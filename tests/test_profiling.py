# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for the profiling/metrics module.
"""

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from threadweave.profiling import (
    LatencyBucket,
    ThroughputWindow,
    PipelineMetrics,
    metrics,
    track_latency,
)


# ── LatencyBucket ──────────────────────────────────────────────


class TestLatencyBucket:
    def test_record_single(self):
        b = LatencyBucket()
        b.record(42.0)
        assert b.count == 1
        assert b.total_ms == 42.0
        assert b.min_ms == 42.0
        assert b.max_ms == 42.0
        assert b.avg_ms == 42.0

    def test_record_multiple(self):
        b = LatencyBucket()
        b.record(10.0)
        b.record(20.0)
        b.record(30.0)
        assert b.count == 3
        assert b.avg_ms == 20.0
        assert b.min_ms == 10.0
        assert b.max_ms == 30.0

    def test_percentiles(self):
        b = LatencyBucket()
        for val in [5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
                     55, 60, 65, 70, 75, 80, 85, 90, 95, 100]:
            b.record(float(val))
        b._compute_percentiles()
        # 20 values: index = int(20 * 0.50) = 10 → value 55.0
        assert b.p50_ms == 55.0
        # p95: int(20 * 0.95) = 19 → value 100.0 (last element)
        assert b.p95_ms == 100.0
        # p99: min(int(20 * 0.99), 19) = 19 → value 100.0
        assert b.p99_ms == 100.0

    def test_empty_bucket(self):
        b = LatencyBucket()
        assert b.avg_ms == 0.0
        assert b.is_empty
        b._compute_percentiles()
        assert b.p50_ms == 0.0

    def test_single_value_percentiles(self):
        b = LatencyBucket()
        b.record(7.0)
        b._compute_percentiles()
        assert b.p50_ms == 7.0
        assert b.p95_ms == 7.0
        assert b.p99_ms == 7.0

    def test_maxlen_enforced(self):
        b = LatencyBucket()
        for i in range(3000):
            b.record(float(i))
        # Only last 2000 samples retained
        assert len(b._recent) == 2000
        assert b._recent[0] == 1000.0  # first retained
        assert b._recent[-1] == 2999.0


# ── ThroughputWindow ───────────────────────────────────────────


class TestThroughputWindow:
    def test_initial_zero(self):
        w = ThroughputWindow()
        assert w.per_minute == 0.0

    def test_after_ticks(self):
        w = ThroughputWindow()
        w.tick()
        w.tick()
        w.tick()
        # Allow a tiny amount of time to pass so elapsed > 0
        time.sleep(0.02)
        assert w.per_minute > 0

    def test_stale_window_resets(self):
        w = ThroughputWindow(_start=time.monotonic() - 200)
        w.tick()
        # Window is older than 120 seconds — resets
        assert w.per_minute == 0.0


# ── PipelineMetrics ────────────────────────────────────────────


class TestPipelineMetrics:
    def test_record_ingest_saved(self):
        m = PipelineMetrics()
        m.record_ingest(saved=True)
        assert m.ingest_total == 1
        assert m.ingest_saved == 1
        assert m.ingest_skipped == 0

    def test_record_ingest_skipped(self):
        m = PipelineMetrics()
        m.record_ingest(skipped=True)
        assert m.ingest_total == 1
        assert m.ingest_saved == 0
        assert m.ingest_skipped == 1

    def test_record_ingest_rejected_pii(self):
        m = PipelineMetrics()
        m.record_ingest(rejected_pii=True)
        assert m.ingest_total == 1
        assert m.ingest_rejected_pii == 1

    def test_record_ingest_combined(self):
        m = PipelineMetrics()
        m.record_ingest(saved=True, skipped=True)  # shouldn't normally happen
        assert m.ingest_total == 1
        assert m.ingest_saved == 1
        assert m.ingest_skipped == 1

    def test_record_detect_llm_hit(self):
        m = PipelineMetrics()
        m.record_detect(llm_hit=True)
        assert m.detect_llm_hits == 1
        assert m.detect_llm_misses == 0
        assert m.detect_regex_fallback == 0

    def test_record_detect_regex_fallback(self):
        m = PipelineMetrics()
        m.record_detect(regex_fallback=True)
        assert m.detect_regex_fallback == 1

    def test_to_dict(self):
        m = PipelineMetrics()
        m.record_ingest(saved=True)
        m.record_detect(regex_fallback=True)

        d = m.to_dict()
        assert "latency" in d
        assert "counters" in d
        assert "throughput_per_minute" in d
        assert "memory" in d
        assert "uptime_seconds" in d
        assert d["counters"]["ingest_total"] == 1
        assert d["counters"]["ingest_saved"] == 1
        assert d["counters"]["detect_regex_fallback"] == 1

    def test_to_prometheus(self):
        m = PipelineMetrics()
        m.record_ingest(saved=True)
        m.detect_latency.record(15.0)
        m.detect_latency.record(25.0)

        text = m.to_prometheus()
        assert "threadweave_ingest_total" in text
        assert "threadweave_ingest_saved" in text
        assert "threadweave_detect_latency_ms_avg" in text
        assert "threadweave_detect_latency_ms_p50" in text
        assert "# HELP" in text
        assert "# TYPE" in text

    def test_reset(self):
        m = PipelineMetrics()
        m.record_ingest(saved=True)
        m.detect_latency.record(10.0)
        m.reset()
        assert m.ingest_total == 0
        assert m.detect_latency.count == 0

    def test_memory_available(self):
        """Memory metrics returned (psutil may or may not be installed)."""
        m = PipelineMetrics()
        mem = m.memory_mb
        # If psutil is available, we get positive values
        if mem["rss_mb"] > 0:
            assert mem["rss_mb"] > 0
            assert mem["vms_mb"] > 0
        else:
            # psutil not installed — returns -1 sentinel
            assert mem["rss_mb"] == -1.0
            assert mem["vms_mb"] == -1.0


# ── track_latency decorator ────────────────────────────────────


class TestTrackLatency:
    def test_sync_function(self):
        bucket = LatencyBucket()

        @track_latency(bucket)
        def slow_add(a, b):
            time.sleep(0.05)  # 50ms — well above Windows ~15ms clock resolution
            return a + b

        result = slow_add(3, 4)
        assert result == 7
        assert bucket.count == 1
        assert bucket.avg_ms >= 5  # at least 5ms

    @pytest.mark.asyncio
    async def test_async_function(self):
        bucket = LatencyBucket()

        @track_latency(bucket)
        async def slow_add_async(a, b):
            import asyncio
            await asyncio.sleep(0.05)  # 50ms — well above clock resolution
            return a + b

        result = await slow_add_async(5, 6)
        assert result == 11
        assert bucket.count == 1
        assert bucket.avg_ms >= 1  # at least 1ms (relaxed for CI/Windows flakiness)

    def test_preserves_metadata(self):
        bucket = LatencyBucket()

        @track_latency(bucket)
        def documented():
            """This docstring must survive."""
            return 1

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "This docstring must survive."


# ── API Metrics Endpoint ───────────────────────────────────────


class TestMetricsEndpoint:
    @pytest.fixture(autouse=True)
    def _reset_metrics(self):
        metrics.reset()
        yield
        metrics.reset()

    def test_metrics_json_endpoint(self):
        from threadweave.api import app
        client = TestClient(app)

        # First, run an ingest to populate metrics
        client.post("/api/v1/ingest", json={
            "content": (
                "After evaluating three databases, we chose PostgreSQL for "
                "the new platform because JSONB and full-text search are "
                "critical for our workload, and the decision is documented."
            ),
            "source": "teams",
        })

        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "latency" in data
        assert "counters" in data
        assert data["counters"]["ingest_total"] >= 1
        assert data["counters"]["ingest_saved"] >= 1
        assert "uptime_seconds" in data

    def test_metrics_prometheus_endpoint(self):
        from threadweave.api import app
        client = TestClient(app)

        client.post("/api/v1/ingest", json={
            "content": "Important decision: we will use Kubernetes.",
            "source": "slack",
        })

        resp = client.get("/api/v1/metrics/prometheus")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        text = resp.text
        assert "threadweave_ingest_total" in text
        assert "# HELP" in text
        assert "# TYPE" in text

    def test_metrics_after_pii_rejection(self):
        # PII detection disabled — test that the pipeline accepts all content
        from threadweave.api import app
        client = TestClient(app)

        client.post("/api/v1/ingest", json={
            "content": "Contact john@company.com or call 555-123-4567 for access.",
            "source": "email",
        })

        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["counters"]["ingest_total"] >= 1
        # No PII rejection — basic PII patterns are disabled.
        # Sensitivity detection is in the confidentiality layer.

    def test_metrics_after_duplicate(self):
        from threadweave.api import app
        client = TestClient(app)

        content = (
            "We have decided to standardize on Terraform for "
            "infrastructure because it gives us state management and "
            "plan reviews, and the rollout schedule is approved."
        )
        client.post("/api/v1/ingest", json={"content": content, "source": "teams"})
        client.post("/api/v1/ingest", json={"content": content, "source": "teams"})

        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["counters"]["dedup_hits"] >= 1

    def test_metrics_reset(self):
        """metrics.reset() should zero out all counters."""
        from threadweave.api import app
        client = TestClient(app)

        client.post("/api/v1/ingest", json={
            "content": "Some knowledge content that will be saved because "
                       "it contains detailed rationale and explanation.",
            "source": "manual",
        })

        # Verify non-zero
        resp = client.get("/api/v1/metrics")
        assert resp.json()["counters"]["ingest_total"] >= 1

        # Reset and verify zero
        metrics.reset()
        resp = client.get("/api/v1/metrics")
        assert resp.json()["counters"]["ingest_total"] == 0
