# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for ThreadWeave API.
"""

import pytest
from fastapi.testclient import TestClient
from threadweave.api import app

client = TestClient(app)


class TestHealth:
    def test_health(self):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.2.0"
        assert "entries_stored" in data


class TestDetection:
    def test_detect_strong_decision_saved(self):
        response = client.post("/api/v1/detect", json={
            "text": (
                "After evaluating three databases, we chose PostgreSQL for "
                "the new platform because JSONB and full-text search are "
                "critical for our workload, and the decision is documented."
            ),
        })
        assert response.status_code == 200
        data = response.json()
        assert data["content_type"] == "decision"
        assert data["confidence"] >= 0.40
        assert data["should_save"] is True

    def test_detect_chat(self):
        response = client.post("/api/v1/detect", json={
            "text": "ok thanks, sounds good!",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["content_type"] == "chat"
        assert data["should_save"] is False

    def test_detect_decision(self):
        response = client.post("/api/v1/detect", json={
            "text": (
                "Decision: We will use GraphQL for the new API. "
                "We chose this over REST because the frontend needs flexible queries."
            ),
        })
        assert response.status_code == 200
        data = response.json()
        assert data["content_type"] == "decision"
        assert data["should_save"] is True

    def test_detect_empty_text(self):
        response = client.post("/api/v1/detect", json={
            "text": "",
        })
        assert response.status_code == 422  # Validation error


class TestSaveAndRetrieve:
    def test_save_and_get(self):
        # Save
        save_resp = client.post("/api/v1/entries", json={
            "content": "Always check the CI pipeline before deploying. "
                       "If it's red, the deploy will fail.",
            "wing": "engineering",
            "room": "deployment",
            "scope": "team",
            "source_type": "slack",
            "author_id": "harald",
        })
        assert save_resp.status_code == 201
        entry_id = save_resp.json()["id"]

        # Get
        get_resp = client.get(f"/api/v1/entries/{entry_id}")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["wing"] == "engineering"
        assert data["room"] == "deployment"
        assert "CI pipeline" in data["content"]

    def test_get_nonexistent(self):
        response = client.get("/api/v1/entries/nonexistent")
        assert response.status_code == 404


class TestSearch:
    @pytest.fixture(autouse=True)
    def setup_entries(self):
        """Seed the in-memory store with test entries."""
        entries = [
            {
                "content": "We use Postgres because of JSONB support and full-text search.",
                "wing": "engineering",
                "room": "database",
                "scope": "team",
                "source_type": "email",
                "author_id": "alice",
            },
            {
                "content": "The billing service needs to handle 10K TPS. We chose event sourcing.",
                "wing": "billing",
                "room": "architecture",
                "scope": "team",
                "source_type": "slack",
                "author_id": "bob",
            },
            {
                "content": "Deployments always happen Tuesdays at 10am. Never on Fridays.",
                "wing": "engineering",
                "room": "deployment",
                "scope": "department",
                "source_type": "manual",
                "author_id": "charlie",
            },
        ]
        for entry in entries:
            client.post("/api/v1/entries", json=entry)

    def test_search_finds_match(self):
        response = client.post("/api/v1/search", json={
            "query": "Postgres",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        assert any("Postgres" in r["content_preview"] for r in data["results"])

    def test_search_wing_filter(self):
        response = client.post("/api/v1/search", json={
            "query": "TPS",
            "wing": "billing",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        assert all(r["wing"] == "billing" for r in data["results"])

    def test_search_no_match(self):
        response = client.post("/api/v1/search", json={
            "query": "MongoDB",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0


class TestWings:
    @pytest.fixture(autouse=True)
    def setup_entries(self):
        client.post("/api/v1/entries", json={
            "content": "Test content for engineering.",
            "wing": "engineering",
            "room": "test",
            "scope": "team",
            "source_type": "manual",
            "author_id": "test",
        })

    def test_list_wings(self):
        response = client.get("/api/v1/wings")
        assert response.status_code == 200
        data = response.json()
        wings = [w["name"] for w in data]
        assert "engineering" in wings

    def test_list_rooms(self):
        response = client.get("/api/v1/wings/engineering/rooms")
        assert response.status_code == 200
        data = response.json()
        rooms = [r["name"] for r in data]
        assert "test" in rooms


class TestOrgModel:
    def test_add_relationship(self):
        response = client.post("/api/v1/org/relationships", json={
            "source": "harald",
            "relation": "member_of",
            "target": "platform_team",
            "valid_from": "2024-01-01",
        })
        assert response.status_code == 201
        assert response.json()["status"] == "created"

    def test_get_team(self):
        # First add a relationship
        client.post("/api/v1/org/relationships", json={
            "source": "alice",
            "relation": "member_of",
            "target": "engineering",
            "valid_from": "2023-01-01",
        })

        response = client.get("/api/v1/org/people/alice/team")
        assert response.status_code == 200

class TestIngestPipeline:
    """Tests for the central ingestion endpoint POST /api/v1/ingest."""

    def test_ingest_decision_saved(self):
        """Ingesting a clear decision should save and return should_save=True."""
        resp = client.post("/api/v1/ingest", json={
            "content": (
                "After evaluating three databases, we chose PostgreSQL for "
                "the new platform because JSONB and full-text search are "
                "critical for our workload, and the decision is documented."
            ),
            "source": "teams",
            "tenant_id": "acme-corp",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["should_save"] is True
        assert data["content_type"] == "decision"
        assert data["deduplicated"] is False
        assert len(data["id"]) > 0

    def test_ingest_chat_skipped(self):
        """Ingesting chat should not save."""
        resp = client.post("/api/v1/ingest", json={
            "content": "ok thanks, sounds good!",
            "source": "teams",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["should_save"] is False
        assert data["content_type"] == "chat"

    def test_ingest_duplicate_detected(self):
        """Same content + metadata ingested twice → duplicate."""
        content = (
            "We have decided to standardize on Terraform for "
            "infrastructure because it gives us state management and "
            "plan reviews, and the rollout schedule is approved."
        )
        # First ingest
        r1 = client.post("/api/v1/ingest", json={
            "content": content,
            "source": "email",
        })
        assert r1.status_code == 201
        assert r1.json()["deduplicated"] is False

        # Second ingest — same content
        r2 = client.post("/api/v1/ingest", json={
            "content": content,
            "source": "teams",  # Different source, same content
        })
        assert r2.status_code == 201
        assert r2.json()["deduplicated"] is True

    def test_ingest_same_content_different_metadata_not_duplicate(self):
        """Same body with different subject/sender should NOT be a duplicate."""
        content = "Please review the attached document and provide feedback by Friday."
        # First ingest — email from Alice about Q4 report
        r1 = client.post("/api/v1/ingest", json={
            "content": content,
            "source": "email",
            "metadata": {
                "title": "Review: Q4 Budget Report",
                "author_id": "alice@company.com",
            },
        })
        assert r1.status_code == 201
        assert r1.json()["deduplicated"] is False

        # Second ingest — same body but from Bob about a different document
        r2 = client.post("/api/v1/ingest", json={
            "content": content,
            "source": "email",
            "metadata": {
                "title": "Review: Engineering Roadmap 2026",
                "author_id": "bob@company.com",
            },
        })
        assert r2.status_code == 201
        assert r2.json()["deduplicated"] is False  # <-- KEY: NOT a duplicate

    def test_ingest_tenant_isolation(self):
        """Different tenants should get separate entries."""
        r1 = client.post("/api/v1/ingest", json={
            "content": (
                "After evaluating three databases, we chose PostgreSQL for "
                "the new platform because JSONB and full-text search are "
                "critical for our workload, and the decision is documented."
            ),
            "source": "manual",
            "tenant_id": "tenant-a",
        })
        r2 = client.post("/api/v1/ingest", json={
            "content": (
                "We have decided to standardize on Terraform for "
                "infrastructure because it gives us state management and "
                "plan reviews, and the rollout schedule is approved."
            ),
            "source": "manual",
            "tenant_id": "tenant-b",
        })
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["should_save"] is True
        assert r2.json()["should_save"] is True

        # List tenant A entries
        list_a = client.get("/api/v1/tenants/tenant-a/entries")
        assert list_a.status_code == 200
        assert len(list_a.json()) >= 1

    def test_ingest_skipped_content_retryable(self):
        """Content that is not worth saving must NOT be deduped, so it can
        be re-ingested (e.g. after the detector configuration changes)."""
        content = (
            "The reason we use Postgres over MySQL is because we need "
            "JSONB support and full-text search. We evaluated both in 2022."
        )
        r1 = client.post("/api/v1/ingest", json={
            "content": content,
            "source": "teams",
        })
        assert r1.status_code == 201
        assert r1.json()["should_save"] is False
        assert r1.json()["deduplicated"] is False

        # Second ingest must be re-evaluated, not short-circuited as dup
        r2 = client.post("/api/v1/ingest", json={
            "content": content,
            "source": "teams",
        })
        assert r2.status_code == 201
        assert r2.json()["deduplicated"] is False
        assert r2.json()["id"] != "duplicate"

    def test_ingest_empty_content_rejected(self):
        """Empty content should return validation error."""
        resp = client.post("/api/v1/ingest", json={
            "content": "",
            "source": "teams",
        })
        assert resp.status_code == 422

    def test_ingest_with_metadata(self):
        """Metadata should be accepted and stored."""
        resp = client.post("/api/v1/ingest", json={
            "content": "Important decision: we will use gRPC for internal services.",
            "source": "sharepoint",
            "tenant_id": "acme-corp",
            "metadata": {
                "wing": "engineering",
                "room": "architecture",
                "title": "gRPC Decision",
                "author": "alice@acme.com",
                "document_path": "/sites/eng/Shared Documents/ADR-0042.docx",
            },
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["should_save"] is True
        assert data["content_type"] == "decision"


class TestMemPalaceSearch:
    """Tests for hybrid search (MemPalace semantic + keyword fallback)."""

    @pytest.fixture(autouse=True)
    def setup_entries(self):
        """Seed the in-memory store with test entries (also pushes to MemPalace if available)."""
        entries = [
            {
                "content": "We use Postgres because of JSONB support and full-text search.",
                "wing": "engineering",
                "room": "database",
                "scope": "team",
                "source_type": "email",
                "author_id": "alice",
            },
            {
                "content": "The billing service needs to handle 10K TPS. We chose event sourcing.",
                "wing": "billing",
                "room": "architecture",
                "scope": "team",
                "source_type": "slack",
                "author_id": "bob",
            },
            {
                "content": "Deployments always happen Tuesdays at 10am. Never on Fridays.",
                "wing": "engineering",
                "room": "deployment",
                "scope": "department",
                "source_type": "manual",
                "author_id": "charlie",
            },
        ]
        for entry in entries:
            client.post("/api/v1/entries", json=entry)

    def test_search_returns_source_field(self):
        """Search results should include a 'source' field (mempalace or in_memory)."""
        response = client.post("/api/v1/search", json={
            "query": "Postgres",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        for r in data["results"]:
            assert "source" in r, f"Result missing 'source' field: {r}"
            assert r["source"] in ("mempalace", "in_memory")

    def test_search_semantic_match(self):
        """Search works with keyword fallback when MemPalace is unavailable."""
        response = client.post("/api/v1/search", json={
            "query": "Postgres JSONB",
        })
        assert response.status_code == 200
        data = response.json()
        # Keyword fallback finds "Postgres" in the content
        assert data["total"] >= 1

    def test_search_hybrid_results_have_bm25_when_mempalace(self):
        """MemPalace results should include bm25_score."""
        response = client.post("/api/v1/search", json={
            "query": "Postgres",
        })
        assert response.status_code == 200
        data = response.json()
        for r in data["results"]:
            if r["source"] == "mempalace":
                assert "bm25_score" in r

    def test_search_deduplicates_across_sources(self):
        """Same entry should not appear twice (from MemPalace + in-memory)."""
        response = client.post("/api/v1/search", json={
            "query": "Postgres",
        })
        assert response.status_code == 200
        data = response.json()
        ids = [r["id"] for r in data["results"]]
        assert len(ids) == len(set(ids)), f"Duplicate IDs in results: {ids}"


class TestSearchMempalaceMetadata:
    """Search must respect sensitivity + tenant stored in MemPalace metadata,
    and deduplicate across sources via shared entry ids."""

    @staticmethod
    def _use_temp_palace(monkeypatch, tmp_path):
        from threadweave import api as api_module
        from threadweave.mempalace_client import MemPalaceClient
        mp = MemPalaceClient(palace_path=str(tmp_path / "palace"))
        assert mp.available, "MemPalace must be importable for these tests"
        monkeypatch.setattr(api_module, "_mempalace", mp)
        monkeypatch.setattr(api_module, "_mempalace_available", True)

    def test_result_carries_sensitivity_and_dedups(self, monkeypatch, tmp_path):
        self._use_temp_palace(monkeypatch, tmp_path)
        resp = client.post("/api/v1/entries", json={
            "content": (
                "The Acme renewal includes a bespoke penalty clause "
                "negotiated under NDA for our tenant A operations."
            ),
            "wing": "engineering",
            "room": "contracts",
            "tenant_id": "tenant-a",
            "sensitivity": "internal",
        })
        assert resp.status_code == 201
        entry_id = resp.json()["id"]

        r = client.post("/api/v1/search", json={
            "query": "Acme penalty clause", "tenant_id": "tenant-a",
        })
        assert r.status_code == 200
        results = r.json()["results"]
        hit = next((x for x in results if x["id"] == entry_id), None)
        assert hit is not None, f"entry {entry_id} missing: {results}"
        assert hit["sensitivity"] == "internal"

        # Same entry must appear once, not once per search source
        ids = [x["id"] for x in results]
        assert len(ids) == len(set(ids)), f"Duplicate IDs in results: {ids}"

    def test_tenant_scoping_applies_to_mempalace_results(self, monkeypatch, tmp_path):
        self._use_temp_palace(monkeypatch, tmp_path)
        resp = client.post("/api/v1/entries", json={
            "content": (
                "Kongsberg radar calibration schedule for tenant B "
                "operations is finalized."
            ),
            "wing": "engineering",
            "room": "calibration",
            "tenant_id": "tenant-b",
            "sensitivity": "internal",
        })
        assert resp.status_code == 201
        entry_id = resp.json()["id"]

        # tenant-a search must not surface tenant-b entries
        r = client.post("/api/v1/search", json={
            "query": "radar calibration", "tenant_id": "tenant-a",
        })
        assert r.status_code == 200
        results = r.json()["results"]
        assert all(
            x["id"] != entry_id for x in results
        ), f"tenant-b entry leaked into tenant-a search: {results}"

        # tenant-b search still finds it
        r = client.post("/api/v1/search", json={
            "query": "radar calibration", "tenant_id": "tenant-b",
        })
        assert r.status_code == 200
        results = r.json()["results"]
        assert any(x["id"] == entry_id for x in results)

