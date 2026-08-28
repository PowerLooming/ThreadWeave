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
        assert data["version"] == "0.4.6"
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

    def test_private_channel_isolated_end_to_end(self):
        """End-to-end: a private-channel message is stored but only its
        members can retrieve it via direct access AND search.

        This is the KM management-channel requirement. A non-member (even
        with admin clearance) must not see it through any retrieval path.
        """
        ingest = client.post("/api/v1/ingest", json={
            "content": (
                "We have decided to freeze headcount across the department "
                "for FY26 and halt all hiring until the Q2 review, and this "
                "management decision is now finalized and documented."
            ),
            "source": "teams",
            "tenant_id": "acme-corp",
            "metadata": {
                "wing": "dept-team",
                "room": "management",
                "private_channel": True,
                "sensitivity": "restricted",
                "allowed_people": ["mgmt-user-1", "mgmt-user-2"],
            },
        })
        assert ingest.status_code == 201
        entry_id = ingest.json()["id"]

        # Direct access: member sees it, non-member (even admin) is denied.
        member_get = client.get(
            f"/api/v1/entries/{entry_id}",
            params={"person_id": "mgmt-user-1"},
        )
        assert member_get.status_code == 200

        nonmember_get = client.get(
            f"/api/v1/entries/{entry_id}",
            params={"person_id": "sysadmin", "role": "admin"},
        )
        assert nonmember_get.status_code == 403

        # Search: member sees the hit, non-member does not.
        member_search = client.post("/api/v1/search", json={
            "query": "headcount freeze",
            "tenant_id": "acme-corp",
            "requester_team": "mgmt-user-1",
        })
        assert member_search.status_code == 200
        assert any(
            r["id"] == entry_id for r in member_search.json()["results"]
        )

        nonmember_search = client.post("/api/v1/search", json={
            "query": "headcount freeze",
            "tenant_id": "acme-corp",
            "requester_team": "sysadmin",
            "requester_role": "admin",
        })
        assert nonmember_search.status_code == 200
        assert not any(
            r["id"] == entry_id for r in nonmember_search.json()["results"]
        )

    def test_source_acl_group_gated_end_to_end(self):
        """P1: a general per-source ACL (group-based) gates direct access
        and search, with deny-overrides-grant and fast revocation."""
        ingest = client.post("/api/v1/ingest", json={
            "content": (
                "We have decided to move the Q3 vendor contract to the new "
                "procurement platform and this decision is now finalized "
                "and documented."
            ),
            "source": "teams",
            "tenant_id": "acme-corp",
            "metadata": {
                "wing": "procurement",
                "room": "contracts",
                "sensitivity": "confidential",
                # General source ACL: visible to group members only.
                "acl": {
                    "allowed_groups": ["grp-procurement"],
                    "allow_users_demo": None,  # ignored, not a recognized key
                },
            },
        })
        assert ingest.status_code == 201
        entry_id = ingest.json()["id"]

        # Member (in the allowed group) sees it via direct access.
        member_get = client.get(
            f"/api/v1/entries/{entry_id}",
            params={"person_id": "proc-user", "role": "readwrite",
                    "groups": "grp-procurement"},
        )
        assert member_get.status_code == 200

        # Non-member (no group) is denied, even as admin.
        nonmember_get = client.get(
            f"/api/v1/entries/{entry_id}",
            params={"person_id": "sysadmin", "role": "admin"},
        )
        assert nonmember_get.status_code == 403

        # Search: group member finds it; non-member does not.
        member_search = client.post("/api/v1/search", json={
            "query": "procurement platform contract",
            "tenant_id": "acme-corp",
            "requester_team": "proc-user",
            "requester_groups": ["grp-procurement"],
        })
        assert any(
            r["id"] == entry_id for r in member_search.json()["results"]
        )

        nonmember_search = client.post("/api/v1/search", json={
            "query": "procurement platform contract",
            "tenant_id": "acme-corp",
            "requester_team": "sysadmin",
            "requester_role": "admin",
            "requester_groups": [],
        })
        assert not any(
            r["id"] == entry_id for r in nonmember_search.json()["results"]
        )

    def test_source_acl_revoke_end_to_end(self):
        """P1: the revoke endpoint denies a previously-allowed member
        immediately, without re-ingest."""
        ingest = client.post("/api/v1/ingest", json={
            "content": (
                "We have decided to onboard the Nordic distribution partner "
                "in Q4 and this decision is now finalized and documented."
            ),
            "source": "teams",
            "tenant_id": "acme-corp",
            "metadata": {
                "wing": "sales",
                "room": "partners",
                "sensitivity": "confidential",
                "acl": {"allowed_users": ["partner-mgr", "sales-dir"]},
            },
        })
        assert ingest.status_code == 201
        entry_id = ingest.json()["id"]

        # Allowed member can see it before revocation.
        before = client.get(
            f"/api/v1/entries/{entry_id}",
            params={"person_id": "partner-mgr"},
        )
        assert before.status_code == 200

        # Admin revokes the ACL grant.
        revoke = client.post(
            f"/api/v1/entries/{entry_id}/revoke",
            params={"role": "admin"},
        )
        assert revoke.status_code == 200

        # Now the member is denied, even with admin role.
        after = client.get(
            f"/api/v1/entries/{entry_id}",
            params={"person_id": "partner-mgr", "role": "admin"},
        )
        assert after.status_code == 403

        # Non-admin cannot revoke.
        nonadmin_revoke = client.post(
            f"/api/v1/entries/{entry_id}/revoke",
            params={"role": "readwrite"},
        )
        assert nonadmin_revoke.status_code == 403


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
                "Nordvik radar calibration schedule for tenant B "
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


class TestActionItemEndpoints:
    """Action-item (task) endpoints: list, done, undone."""

    @pytest.fixture(autouse=True)
    def _isolate_notify_store(self, tmp_path, monkeypatch):
        """Give each test a fresh notification DB so action-item task
        notifications don't leak into other tests (the shared notify
        singleton would otherwise pollute test_notifications.py)."""
        import threadweave.notify as notify_mod

        monkeypatch.setenv(
            "THREADWEAVE_NOTIFY_DB",
            str(tmp_path / "notifications.sqlite3"),
        )
        notify_mod._store = None
        yield
        notify_mod._store = None

    def _ingest_assignment(self, content, author="boss@x.com"):
        resp = client.post("/api/v1/ingest", json={
            "content": content,
            "source": "teams",
            "metadata": {"author_id": author},
        })
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_list_tasks_by_owner(self):
        eid = self._ingest_assignment(
            "Adele should look into the Azure quota issue."
        )
        # owner matching by name works without org resolution on the bot side
        r = client.get("/api/v1/tasks", params={"owner": "Adele"})
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()["tasks"]]
        assert eid in ids

    def test_list_tasks_empty(self):
        r = client.get("/api/v1/tasks", params={"owner": "nobody@x.com"})
        assert r.status_code == 200
        assert r.json()["tasks"] == []

    def test_task_done_and_undone(self):
        eid = self._ingest_assignment(
            "Harald should chase the vendor by Friday."
        )
        # open by default
        r = client.get("/api/v1/tasks", params={"owner": "harald"})
        assert any(t["id"] == eid and t["status"] == "open"
                   for t in r.json()["tasks"])

        # mark done → no longer open
        r = client.post(f"/api/v1/tasks/{eid}/done")
        assert r.status_code == 200
        r = client.get("/api/v1/tasks", params={"owner": "harald"})
        assert all(t["id"] != eid for t in r.json()["tasks"])

        # done filter shows it
        r = client.get("/api/v1/tasks", params={"owner": "harald",
                                                "status": "done"})
        assert any(t["id"] == eid for t in r.json()["tasks"])

        # undone → open again
        r = client.post(f"/api/v1/tasks/{eid}/undone")
        assert r.status_code == 200
        r = client.get("/api/v1/tasks", params={"owner": "harald"})
        assert any(t["id"] == eid for t in r.json()["tasks"])

    def test_task_done_missing_404(self):
        r = client.post("/api/v1/tasks/does-not-exist/done")
        assert r.status_code == 404

    def test_ingest_queues_task_notification_to_assignee(self):
        """A high-confidence resolved assignment notifies the assignee."""
        from threadweave.notify import get_notification_store

        # Ensure a clean store snapshot of pending before this ingest
        before = {n["id"] for n in
                  get_notification_store().pending(limit=100)}
        eid = self._ingest_assignment(
            "Harald should follow up on the vendor invoice.",
            author="boss@x.com",
        )
        pending = get_notification_store().pending(limit=100)
        new = [n for n in pending if n["id"] not in before]
        task_notifs = [n for n in new if n.get("kind") == "task"]
        assert task_notifs, "expected a task notification for the assignee"
        # Notification is addressed to the assignee (Harald), not the author
        assert task_notifs[0]["author_id"] == "harald"
        assert task_notifs[0]["entry_id"] == eid

    def test_ingest_completion_sets_suggested_done(self):
        """A 'done' statement from the assignee suggests the task is done."""
        from threadweave.notify import get_notification_store

        before = {n["id"] for n in
                  get_notification_store().pending(limit=100)}
        # 1. assign harald a task (unique content to avoid dedup with other tests)
        task_id = self._ingest_assignment(
            "Harald should chase the Phase3 vendor by Friday.",
            author="boss@x.com",
        )
        # open
        r = client.get("/api/v1/tasks", params={"owner": "harald"})
        assert any(t["id"] == task_id and t["status"] == "open"
                   for t in r.json()["tasks"])

        # 2. harald reports completion
        c = client.post("/api/v1/ingest", json={
            "content": "I've chased the vendor now.",
            "source": "teams",
            "metadata": {"author_id": "harald"},
        })
        assert c.status_code == 201

        # task now suggested_done, not open
        r = client.get("/api/v1/tasks", params={"owner": "harald"})
        assert all(t["id"] != task_id for t in r.json()["tasks"])
        r = client.get("/api/v1/tasks", params={"owner": "harald",
                                                "status": "suggested_done"})
        assert any(t["id"] == task_id for t in r.json()["tasks"])

        # a task_suggest confirmation notification was queued to harald
        # for the suggested task(s) (the shared entry store may hold other
        # matching tasks from earlier tests, so assert the suggestion
        # notification is addressed to harald and refers to a suggested task)
        pending = get_notification_store().pending(limit=100)
        new = [n for n in pending if n["id"] not in before]
        suggests = [n for n in new if n.get("kind") == "task_suggest"]
        assert suggests, "expected a task_suggest confirmation notification"
        assert suggests[0]["author_id"] == "harald"

        # 3. confirm → done
        r = client.post(f"/api/v1/tasks/{task_id}/done")
        assert r.status_code == 200
        r = client.get("/api/v1/tasks", params={"owner": "harald",
                                                "status": "done"})
        assert any(t["id"] == task_id for t in r.json()["tasks"])

    def test_ingest_completion_no_match_changes_nothing(self):
        """An unrelated completion from the assignee leaves open tasks open."""
        task_id = self._ingest_assignment(
            "Harald should chase the vendor.",
            author="boss@x.com",
        )
        c = client.post("/api/v1/ingest", json={
            "content": "I've finished the coffee now.",
            "source": "teams",
            "metadata": {"author_id": "harald"},
        })
        assert c.status_code == 201
        r = client.get("/api/v1/tasks", params={"owner": "harald"})
        assert any(t["id"] == task_id and t["status"] == "open"
                   for t in r.json()["tasks"])


class TestP6CrossLanguageCapture:
    """P6: non-English content is translated at ingest and stored as content_en.

    The translation call is mocked (translate_async) so the test runs without
    a live Ollama. The assertions verify the entry gains content_en and that
    the English translation is searchable.
    """

    def test_non_english_ingest_stores_content_en(self, monkeypatch):
        from threadweave.detector import DetectionResult, ContentType
        import threadweave.api as api_mod

        # Make detection report Chinese content.
        async def fake_detect(text, threshold=0.40):
            return True, DetectionResult(
                content_type=ContentType.DECISION,
                confidence=0.95,
                suggested_scope="team",
                suggested_title="Decision to migrate vendor contract",
                language="zh",
            )

        async def fake_translate(text, target="en"):
            return "We decided to migrate the vendor contract to the new procurement platform."

        monkeypatch.setattr(api_mod, "is_worth_saving_async", fake_detect)
        monkeypatch.setattr(api_mod, "translate_async", fake_translate)

        resp = client.post("/api/v1/ingest", json={
            "content": (
                "我们已经决定将供应商合同迁移到新的采购平台，这个决定已经最终确定并记录在案，"
                "包括具体的迁移时间表和负责人安排。"
            ),
            "source": "teams",
            "tenant_id": "acme-corp",
            "metadata": {"wing": "procurement", "room": "contracts"},
        })
        assert resp.status_code == 201
        entry_id = resp.json()["id"]

        # The stored entry must carry the English translation.
        from threadweave.api import _memory_store
        stored = _memory_store.get(entry_id)
        assert stored is not None
        assert stored["content_en"] == (
            "We decided to migrate the vendor contract to the new procurement platform."
        )
        # Original preserved.
        assert stored["content"].startswith("我们已经决定")

        # Search by English terms finds it (in-memory keyword path).
        r = client.post("/api/v1/search", json={
            "query": "migrate vendor contract",
            "tenant_id": "acme-corp",
            "requester_team": "someone",
        })
        assert any(x["id"] == entry_id for x in r.json()["results"])

    def test_english_ingest_skips_translation(self, monkeypatch):
        from threadweave.detector import DetectionResult, ContentType
        import threadweave.api as api_mod

        called = {"translate": False}

        async def fake_detect(text, threshold=0.40):
            return True, DetectionResult(
                content_type=ContentType.DECISION,
                confidence=0.95,
                language="en",
            )

        async def fake_translate(text, target="en"):
            called["translate"] = True
            return "should not be called"

        monkeypatch.setattr(api_mod, "is_worth_saving_async", fake_detect)
        monkeypatch.setattr(api_mod, "translate_async", fake_translate)

        resp = client.post("/api/v1/ingest", json={
            "content": (
                "We decided to use PostgreSQL for the new analytics platform because "
                "it offers JSONB support and full-text search that our workload requires."
            ),
            "source": "email",
            "tenant_id": "acme-corp",
            "metadata": {"wing": "engineering", "room": "database"},
        })
        assert resp.status_code == 201
        assert called["translate"] is False, "translation must not run for English"

        from threadweave.api import _memory_store
        stored = _memory_store.get(resp.json()["id"])
        assert stored["content_en"] == ""

    def test_translation_failure_never_blocks_capture(self, monkeypatch):
        from threadweave.detector import DetectionResult, ContentType
        import threadweave.api as api_mod

        async def fake_detect(text, threshold=0.40):
            return True, DetectionResult(
                content_type=ContentType.DECISION,
                confidence=0.95,
                language="no",
            )

        async def fake_translate(text, target="en"):
            return None  # translation fails

        monkeypatch.setattr(api_mod, "is_worth_saving_async", fake_detect)
        monkeypatch.setattr(api_mod, "translate_async", fake_translate)

        resp = client.post("/api/v1/ingest", json={
            "content": (
                "Vi har besluttet å migrere leverandørkontrakten til den nye "
                "innkjøpsplattformen, og denne beslutningen er endelig og dokumentert."
            ),
            "source": "teams",
            "tenant_id": "acme-corp",
            "metadata": {"wing": "procurement", "room": "contracts"},
        })
        assert resp.status_code == 201, "capture must not fail when translation fails"

        from threadweave.api import _memory_store
        stored = _memory_store.get(resp.json()["id"])
        assert stored is not None
        # Original preserved even though translation returned None.
        assert stored["content"].startswith("Vi har besluttet")
        assert stored["content_en"] == ""


class TestP2Citation:
    """P2: every search result carries a source_url citation back to the
    exact captured source (Teams message deep link, file, etc.)."""

    def test_search_result_includes_source_url(self):
        resp = client.post("/api/v1/ingest", json={
            "content": (
                "We decided to adopt Kubernetes for the platform rollout and "
                "this decision is now finalized and documented in full."
            ),
            "source": "teams",
            "tenant_id": "acme-corp",
            "metadata": {
                "wing": "platform",
                "room": "deploy",
                "message_url": "https://teams.microsoft.com/l/message/team/123",
            },
        })
        assert resp.status_code == 201
        eid = resp.json()["id"]

        r = client.post("/api/v1/search", json={
            "query": "Kubernetes platform rollout",
            "tenant_id": "acme-corp",
            "requester_team": "someone",
        })
        hits = [x for x in r.json()["results"] if x["id"] == eid]
        assert hits, "entry should be searchable"
        assert hits[0].get("source_url") == (
            "https://teams.microsoft.com/l/message/team/123"
        )

    def test_source_url_empty_for_manual_entry(self):
        """An entry captured with no source link has an empty source_url."""
        resp = client.post("/api/v1/ingest", json={
            "content": (
                "We decided to standardize on a quarterly security review and "
                "this decision is now finalized and documented in full."
            ),
            "source": "manual",
            "tenant_id": "acme-corp",
            "metadata": {"wing": "security", "room": "process"},
        })
        assert resp.status_code == 201
        eid = resp.json()["id"]

        r = client.post("/api/v1/search", json={
            "query": "quarterly security review",
            "tenant_id": "acme-corp",
            "requester_team": "someone",
        })
        hits = [x for x in r.json()["results"] if x["id"] == eid]
        assert hits
        assert hits[0].get("source_url") == ""

    def test_citation_url_helper_prefers_message_url(self):
        import threadweave.api as api_mod
        md = {
            "source_file": "/path/to/doc.pdf",
            "url": "https://example.com/doc.pdf",
            "message_url": "https://teams.microsoft.com/l/message/42",
        }
        assert api_mod._citation_url(md) == "https://teams.microsoft.com/l/message/42"

    def test_citation_url_falls_back_when_no_message_url(self):
        import threadweave.api as api_mod
        md = {"url": "https://example.com/doc.pdf"}
        assert api_mod._citation_url(md) == "https://example.com/doc.pdf"

    def test_citation_url_empty_for_no_link(self):
        import threadweave.api as api_mod
        assert api_mod._citation_url({}) == ""
        assert api_mod._citation_url({"wing": "x"}) == ""



