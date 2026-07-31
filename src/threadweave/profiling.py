# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Zero-dependency profiling and metrics for the ThreadWeave ingestion pipeline.

Tracks latency percentiles, throughput, memory, and pipeline-stage counters.
Exposes both JSON (human/dashboard) and Prometheus text format.

Usage:
    from threadweave.profiling import metrics, track_latency

    # Decorate pipeline stages
    @track_latency(metrics.detect_latency)
    async def classify(text): ...

    # Read metrics
    print(metrics.to_dict())
    print(metrics.to_prometheus())
"""

from __future__ import annotations

import functools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional


# ── Rolling bucket (percentile-friendly ring buffer) ──────────


@dataclass
class LatencyBucket:
    """Tracks count, min/max, and recent samples for percentile computation."""

    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=2000))

    def record(self, duration_ms: float) -> None:
        self.count += 1
        self.total_ms += duration_ms
        if duration_ms < self.min_ms:
            self.min_ms = duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        self._recent.append(duration_ms)

    def _compute_percentiles(self) -> None:
        if not self._recent:
            return
        s = sorted(self._recent)
        n = len(s)
        self.p50_ms = s[int(n * 0.50)]
        self.p95_ms = s[min(int(n * 0.95), n - 1)]
        self.p99_ms = s[min(int(n * 0.99), n - 1)]

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    @property
    def is_empty(self) -> bool:
        return self.count == 0


# ── Throughput tracker ─────────────────────────────────────────


@dataclass
class ThroughputWindow:
    """Rolling 60-second window for per-minute throughput."""

    _start: float = field(default_factory=time.monotonic)
    _count: int = 0
    _lock: object = field(default_factory=object)  # sentinel, not a real lock

    def tick(self) -> None:
        self._count += 1

    @property
    def per_minute(self) -> float:
        elapsed = time.monotonic() - self._start
        if elapsed <= 0:
            return 0.0
        # Reset window if stale (no activity for 2 min)
        if elapsed > 120:
            self._start = time.monotonic()
            self._count = 0
            return 0.0
        return (self._count / elapsed) * 60.0


# ── Central metrics registry ───────────────────────────────────


@dataclass
class PipelineMetrics:
    """All ThreadWeave pipeline metrics in one place.

    Thread-safe enough for single-worker FastAPI (no locks needed since
    Python's GIL protects dataclass field mutations).
    """

    # -- latency buckets --
    ingest_latency: LatencyBucket = field(default_factory=LatencyBucket)
    detect_latency: LatencyBucket = field(default_factory=LatencyBucket)
    mempalace_write_latency: LatencyBucket = field(default_factory=LatencyBucket)
    dedup_latency: LatencyBucket = field(default_factory=LatencyBucket)

    # -- counters --
    ingest_total: int = 0
    ingest_saved: int = 0
    ingest_skipped: int = 0
    ingest_rejected_pii: int = 0
    detect_llm_hits: int = 0
    detect_llm_misses: int = 0
    detect_regex_fallback: int = 0
    dedup_hits: int = 0

    # -- throughput --
    throughput: ThroughputWindow = field(default_factory=ThroughputWindow)

    # -- uptime --
    _start_time: float = field(default_factory=time.monotonic)

    # -- memory (lazy import psutil) --
    _proc: object = None  # psutil.Process or None

    # ── record helpers ─────────────────────────────────────────

    def record_ingest(self, *, saved: bool = False, skipped: bool = False,
                      rejected_pii: bool = False) -> None:
        self.ingest_total += 1
        if saved:
            self.ingest_saved += 1
        if skipped:
            self.ingest_skipped += 1
        if rejected_pii:
            self.ingest_rejected_pii += 1
        self.throughput.tick()

    def record_detect(self, *, llm_hit: bool = False, llm_miss: bool = False,
                      regex_fallback: bool = False) -> None:
        if llm_hit:
            self.detect_llm_hits += 1
        if llm_miss:
            self.detect_llm_misses += 1
        if regex_fallback:
            self.detect_regex_fallback += 1

    # ── memory ─────────────────────────────────────────────────

    @property
    def memory_mb(self) -> dict[str, float]:
        """Resident + virtual memory in MB. Returns -1 if psutil not installed."""
        try:
            import psutil
        except ImportError:
            return {"rss_mb": -1.0, "vms_mb": -1.0}
        if self._proc is None:
            self._proc = psutil.Process()
        mem = self._proc.memory_info()  # type: ignore[union-attr]
        return {
            "rss_mb": round(mem.rss / (1024 * 1024), 2),
            "vms_mb": round(mem.vms / (1024 * 1024), 2),
        }

    # ── export ─────────────────────────────────────────────────

    def _compute_all_percentiles(self) -> None:
        for bucket in [
            self.ingest_latency, self.detect_latency,
            self.mempalace_write_latency, self.dedup_latency,
        ]:
            bucket._compute_percentiles()

    def to_dict(self) -> dict:
        """JSON-friendly snapshot (for /api/v1/metrics)."""
        self._compute_all_percentiles()
        mem = self.memory_mb
        return {
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            "throughput_per_minute": round(self.throughput.per_minute, 2),
            "latency": {
                "ingest":      _bucket_dict(self.ingest_latency),
                "detect":      _bucket_dict(self.detect_latency),
                "mempalace":   _bucket_dict(self.mempalace_write_latency),
                "dedup":       _bucket_dict(self.dedup_latency),
            },
            "counters": {
                "ingest_total":          self.ingest_total,
                "ingest_saved":          self.ingest_saved,
                "ingest_skipped":        self.ingest_skipped,
                "ingest_rejected_pii":   self.ingest_rejected_pii,
                "detect_llm_hits":       self.detect_llm_hits,
                "detect_llm_misses":     self.detect_llm_misses,
                "detect_regex_fallback": self.detect_regex_fallback,
                "dedup_hits":            self.dedup_hits,
            },
            "memory": mem,
        }

    def to_prometheus(self) -> str:
        """Prometheus text format (for /api/v1/metrics/prometheus)."""
        self._compute_all_percentiles()
        out: list[str] = []

        def _g(name: str, value: float, help_: str) -> None:
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} gauge")
            out.append(f"{name} {value}")

        def _c(name: str, value: int, help_: str) -> None:
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} counter")
            out.append(f"{name} {value}")

        # Latency gauges
        for key, bucket in [
            ("ingest", self.ingest_latency),
            ("detect", self.detect_latency),
            ("mempalace_write", self.mempalace_write_latency),
            ("dedup", self.dedup_latency),
        ]:
            pfx = f"threadweave_{key}_latency_ms"
            _g(f"{pfx}_avg", round(bucket.avg_ms, 2), f"Average {key} latency ms")
            _g(f"{pfx}_min", round(bucket.min_ms, 2), f"Min {key} latency ms")
            _g(f"{pfx}_max", round(bucket.max_ms, 2), f"Max {key} latency ms")
            _g(f"{pfx}_p50", round(bucket.p50_ms, 2), f"P50 {key} latency ms")
            _g(f"{pfx}_p95", round(bucket.p95_ms, 2), f"P95 {key} latency ms")
            _g(f"{pfx}_p99", round(bucket.p99_ms, 2), f"P99 {key} latency ms")
            _c(f"{pfx}_count", bucket.count, f"Total {key} calls")

        # Throughput
        _g("threadweave_throughput_per_minute",
           round(self.throughput.per_minute, 2),
           "Ingest throughput (per minute, rolling window)")

        # Counters
        _c("threadweave_ingest_total", self.ingest_total, "Total ingest calls")
        _c("threadweave_ingest_saved", self.ingest_saved, "Entries saved")
        _c("threadweave_ingest_skipped", self.ingest_skipped, "Entries skipped (not worth saving)")
        _c("threadweave_ingest_rejected_pii", self.ingest_rejected_pii, "Entries rejected (PII)")
        _c("threadweave_detect_llm_hits", self.detect_llm_hits, "LLM detection successes")
        _c("threadweave_detect_llm_misses", self.detect_llm_misses, "LLM detection failures (regex fallback)")
        _c("threadweave_detect_regex_fallback", self.detect_regex_fallback, "Regex used (no key / short text)")
        _c("threadweave_dedup_hits", self.dedup_hits, "Duplicate content detected")

        # Memory
        mem = self.memory_mb
        _g("threadweave_memory_rss_mb", mem["rss_mb"], "Resident memory MB")
        _g("threadweave_memory_vms_mb", mem["vms_mb"], "Virtual memory MB")
        _g("threadweave_uptime_seconds",
           round(time.monotonic() - self._start_time, 0),
           "Process uptime seconds")

        return "\n".join(out) + "\n"

    def reset(self) -> None:
        """Reset all counters and buckets (for testing)."""
        self.ingest_latency = LatencyBucket()
        self.detect_latency = LatencyBucket()
        self.mempalace_write_latency = LatencyBucket()
        self.dedup_latency = LatencyBucket()
        self.ingest_total = 0
        self.ingest_saved = 0
        self.ingest_skipped = 0
        self.ingest_rejected_pii = 0
        self.detect_llm_hits = 0
        self.detect_llm_misses = 0
        self.detect_regex_fallback = 0
        self.dedup_hits = 0
        self.throughput = ThroughputWindow()
        self._start_time = time.monotonic()


def _bucket_dict(b: LatencyBucket) -> dict:
    return {
        "avg_ms":  round(b.avg_ms, 2),
        "min_ms":  round(b.min_ms, 2) if b.min_ms != float("inf") else 0,
        "max_ms":  round(b.max_ms, 2),
        "p50_ms":  round(b.p50_ms, 2),
        "p95_ms":  round(b.p95_ms, 2),
        "p99_ms":  round(b.p99_ms, 2),
        "count":   b.count,
    }


# ── Decorator ──────────────────────────────────────────────────


def track_latency(bucket: LatencyBucket) -> Callable:
    """Decorator: record function wall-clock time into *bucket* (milliseconds).

    Works on both async and sync functions.
    """
    import asyncio

    def deco(fn: Callable) -> Callable:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                t0 = time.monotonic()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    bucket.record((time.monotonic() - t0) * 1000)
            return wrapper
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                t0 = time.monotonic()
                try:
                    return fn(*args, **kwargs)
                finally:
                    bucket.record((time.monotonic() - t0) * 1000)
            return wrapper
    return deco


# ── Module-level singleton ─────────────────────────────────────

metrics = PipelineMetrics()
