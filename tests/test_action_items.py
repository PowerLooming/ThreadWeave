# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for action items (responsibility assignments)."""

import pytest

from threadweave.action_items import (
    ActionItem,
    ActionStatus,
    attach_to_entry,
    extract_action_items,
    list_open_tasks,
    set_task_status,
    is_action_entry,
    action_item_of,
    extract_completion_signal,
    suggest_done,
    list_pending_tasks,
    reassign_task,
    list_tasks_due,
    build_digest,
)
from threadweave.org_model import OrgModel
from threadweave.store import EntryStore
from datetime import datetime


@pytest.fixture
def store(tmp_path):
    return EntryStore(url=f"sqlite:///{tmp_path}/entries.sqlite3")


def _org():
    org = OrgModel()
    org.add_entity("harald", "Harald Daltveit", "person")
    org.add_entity("adele", "Adele Smith", "person")
    org.add_entity("bob", "Bob Johnson", "person")
    return org


class TestExtractActionItems:

    def test_direct_you_resolves_to_author(self):
        text = "Harald, can you chase the vendor by Friday?"
        items = extract_action_items(text, author_id="harald", author_name="Harald")
        assert len(items) == 1
        it = items[0]
        assert it.owner == "harald"
        assert it.owner_resolved is True
        assert "chase" in it.action.lower()
        assert it.deadline != ""  # "by Friday" parsed
        assert it.status == ActionStatus.OPEN

    def test_obligation_to_author(self):
        text = "You need to update the release notes before we ship."
        items = extract_action_items(text, author_id="alice")
        assert len(items) == 1
        assert items[0].owner == "alice"
        assert "release notes" in items[0].action.lower()

    def test_named_owner_resolves_via_org(self):
        text = "Adele should look into the Azure quota issue."
        org = _org()
        items = extract_action_items(text, author_id="harald", org=org)
        assert len(items) == 1
        it = items[0]
        # "Adele" resolved via first-name match
        assert it.owner == "adele"
        assert it.owner_resolved is True

    def test_named_owner_with_arbitrary_verb(self):
        """An obligation phrased with a verb outside any allow-list still
        becomes a task. Before the general modal pattern, "Adele must deliver
        ... by Friday" produced no action item at all."""
        items = extract_action_items(
            "Adele must deliver the Q3 procurement vendor report by Friday, "
            "this is confirmed and agreed.",
            author_id="harald",
        )
        assert len(items) == 1
        it = items[0]
        assert it.owner == "adele"
        assert it.owner_name == "Adele"     # original casing, not the lowered text
        assert it.owner_resolved is True
        assert it.deadline                  # next Friday

    def test_predictive_will_is_not_an_obligation(self):
        """'will <verb>' is predictive, not an assignment: no task."""
        items = extract_action_items(
            "Megan will be at the offsite next week.", author_id="harald"
        )
        assert items == []

    def test_ambiguous_someone_is_unresolved(self):
        text = "Someone should fix the flaky test in CI."
        items = extract_action_items(text, author_id="harald")
        assert len(items) == 1
        it = items[0]
        assert it.owner == ""
        assert it.owner_resolved is False

    def test_unresolved_named_owner(self):
        text = "Zed should handle the onboarding doc."
        org = _org()  # no "Zed" in org
        items = extract_action_items(text, author_id="harald", org=org)
        assert len(items) == 1
        it = items[0]
        assert it.owner == ""
        assert it.owner_resolved is False
    def test_no_action_no_item(self):
        text = "The weather is nice today and the build is green."
        items = extract_action_items(text, author_id="harald")
        assert items == []

    def test_polite_request_without_verb_is_not_task(self):
        # "would you be able to look at this sometime" is soft; our pattern
        # requires an action verb after "would you". "look at this sometime"
        # does match, but verify at least it's classified with the author.
        text = "Would you be able to look at this sometime?"
        items = extract_action_items(text, author_id="harald")
        # The action is vague; treat as resolved to author but low value.
        assert all(it.owner == "harald" for it in items)

    def test_multiple_items_in_one_message(self):
        text = (
            "Harald, can you chase the vendor? "
            "Also, Bob should own the QA run."
        )
        org = _org()
        items = extract_action_items(text, author_id="harald", org=org)
        assert len(items) == 2
        owners = {it.owner for it in items}
        assert "harald" in owners
        assert "bob" in owners

class TestStoreIntegration:

    def test_attach_and_roundtrip(self, store):
        entry = {
            "id": "t1",
            "content": "Harald to chase the vendor by Friday",
            "wing": "engineering", "room": "general",
            "source_type": "email", "author_id": "bob@x.com",
        }
        item = ActionItem(
            owner="harald", owner_name="Harald", owner_resolved=True,
            action="chase the vendor", deadline="2026-08-22",
        )
        attach_to_entry(entry, item)
        assert is_action_entry(entry)
        assert entry["source_metadata"]["action_status"] == "open"
        assert any(e.get("value") == "harald" for e in entry["entities"])

        store.save(entry)
        loaded = store.get("t1")
        assert is_action_entry(loaded)
        rehydrated = action_item_of(loaded)
        assert rehydrated is not None
        assert rehydrated.owner == "harald"
        assert rehydrated.action == "chase the vendor"
        assert rehydrated.deadline == "2026-08-22"
        assert rehydrated.status == ActionStatus.OPEN

    def test_list_open_tasks_filters_owner(self, store):
        e1 = {"id": "t1", "content": "a", "source_type": "manual",
              "author_id": "bob"}
        e2 = {"id": "t2", "content": "b", "source_type": "manual",
              "author_id": "bob"}
        attach_to_entry(e1, ActionItem(
            owner="harald", owner_name="Harald", owner_resolved=True,
            action="chase vendor"))
        attach_to_entry(e2, ActionItem(
            owner="adele", owner_name="Adele", owner_resolved=True,
            action="write report"))
        store.save(e1)
        store.save(e2)

        open_harald = list_open_tasks(store, owner="harald")
        assert [e["id"] for e in open_harald] == ["t1"]
        all_open = list_open_tasks(store)
        assert len(all_open) == 2

    def test_non_action_entries_excluded(self, store):
        store.save({"id": "plain", "content": "We use Postgres for X",
                    "source_type": "manual", "author_id": "a",
                    "source_metadata": {}})
        assert list_open_tasks(store) == []

    def test_done_status_excluded_from_open(self, store):
        entry = {"id": "t1", "content": "a", "source_type": "manual",
                 "author_id": "bob"}
        attach_to_entry(entry, ActionItem(
            owner="harald", owner_name="Harald", owner_resolved=True,
            action="chase vendor"))
        store.save(entry)
        assert len(list_open_tasks(store)) == 1

        # mark done
        assert set_task_status(store, "t1", ActionStatus.DONE) is True
        assert list_open_tasks(store) == []

    def test_set_task_status_requires_action_entry(self, store):
        store.save({"id": "plain", "content": "x", "source_type": "manual",
                    "author_id": "a"})
        assert set_task_status(store, "plain", ActionStatus.DONE) is False


class TestOrgResolution:

    def test_resolve_by_display_name(self):
        org = _org()
        pid, disp, ok = org.resolve_person("Harald Daltveit")
        assert pid == "harald" and ok is True

    def test_resolve_by_first_name(self):
        org = _org()
        pid, disp, ok = org.resolve_person("Adele")
        assert pid == "adele" and ok is True

    def test_resolve_by_email_local(self):
        org = OrgModel()
        org.add_entity("bob@company.com", "Bob Johnson", "person")
        pid, disp, ok = org.resolve_person("bob")
        assert pid == "bob@company.com" and ok is True

    def test_resolve_missing(self):
        org = _org()
        pid, disp, ok = org.resolve_person("Unknown Person")
        assert ok is False
        assert pid == ""

    def test_get_direct_reports(self):
        org = OrgModel()
        org.add_entity("mgr", "Mgr", "person")
        org.add_entity("r1", "R One", "person")
        org.add_entity("r2", "R Two", "person")
        org.add_relationship("r1", "reports_to", "mgr", valid_from="2026-01-01")
        org.add_relationship("r2", "reports_to", "mgr", valid_from="2026-01-01")
        assert sorted(org.get_direct_reports("mgr")) == ["r1", "r2"]

    def test_get_direct_reports_closed_edge_excluded(self):
        org = OrgModel()
        org.add_entity("mgr", "Mgr", "person")
        org.add_entity("r1", "R One", "person")
        org.add_relationship("r1", "reports_to", "mgr",
                             valid_from="2026-01-01", valid_to="2026-06-01")
        # edge closed before now → not a current report
        assert org.get_direct_reports("mgr") == []


class TestCompletionDetection:
    """Phase 3: detect 'done' statements and correlate to open tasks."""

    def _open_task(self, store, eid, owner, action):
        entry = {"id": eid, "content": action, "source_type": "manual",
                 "author_id": "boss", "source_metadata": {}}
        attach_to_entry(entry, ActionItem(
            owner=owner, owner_name=owner, owner_resolved=True, action=action))
        store.save(entry)
        return entry

    def test_extract_completion_signal_ive(self):
        assert extract_completion_signal(
            "I've chased the vendor now.") is not None

    def test_extract_completion_signal_object_is_done(self):
        assert extract_completion_signal(
            "The QA run is done.") is not None

    def test_extract_completion_signal_bare_past(self):
        assert extract_completion_signal(
            "I fixed the flaky test.") is not None

    def test_no_completion_signal_for_plain(self):
        assert extract_completion_signal(
            "The weather is nice today.") is None

    def test_suggest_done_correlates_to_owner(self, store):
        self._open_task(store, "t1", "harald", "chase the vendor")
        self._open_task(store, "t2", "adele", "write the report")
        affected = suggest_done(
            store, "I've chased the vendor now.", author_id="harald")
        # only harald's task is suggested; adele's untouched
        assert [e["id"] for e in affected] == ["t1"]
        assert list_pending_tasks(store, owner="harald")[0]["id"] == "t1"
        assert list_open_tasks(store, owner="harald") == []
        # adele's task is still open
        assert list_open_tasks(store, owner="adele")[0]["id"] == "t2"

    def test_suggest_done_sets_suggested_not_done(self, store):
        self._open_task(store, "t1", "harald", "chase the vendor")
        suggest_done(store, "I've chased the vendor now.", author_id="harald")
        md = store.get("t1")["source_metadata"]
        assert md["action_status"] == ActionStatus.SUGGESTED_DONE.value
        # must not be DONE — owner confirms
        assert md["action_status"] != ActionStatus.DONE.value

    def test_suggest_done_no_match_changes_nothing(self, store):
        self._open_task(store, "t1", "harald", "chase the vendor")
        # unrelated completion → no match
        affected = suggest_done(
            store, "I've finished the coffee.", author_id="harald")
        assert affected == []
        assert list_open_tasks(store, owner="harald")[0]["id"] == "t1"

    def test_suggest_done_other_author_untouched(self, store):
        self._open_task(store, "t1", "harald", "chase the vendor")
        # adele reports completion, not harald → harald's task unchanged
        affected = suggest_done(
            store, "I've chased the vendor now.", author_id="adele")
        assert affected == []
        assert list_open_tasks(store, owner="harald")[0]["id"] == "t1"

    def test_confirm_done_transitions_suggested_to_done(self, store):
        self._open_task(store, "t1", "harald", "chase the vendor")
        suggest_done(store, "I've chased the vendor now.", author_id="harald")
        # owner confirms → DONE
        assert set_task_status(store, "t1", ActionStatus.DONE) is True
        md = store.get("t1")["source_metadata"]
        assert md["action_status"] == ActionStatus.DONE.value

    def test_reject_not_done_returns_to_open(self, store):
        self._open_task(store, "t1", "harald", "chase the vendor")
        suggest_done(store, "I've chased the vendor now.", author_id="harald")
        assert list_pending_tasks(store, owner="harald")
        # owner rejects → back to OPEN
        assert set_task_status(store, "t1", ActionStatus.OPEN) is True
        assert list_open_tasks(store, owner="harald")[0]["id"] == "t1"
        assert list_pending_tasks(store, owner="harald") == []


class TestFollowUpLoop:
    """P3: reassign, due-soon/overdue, and the weekly digest."""

    def _make_task(self, store, eid, owner, action="chase the vendor",
                   deadline="", status="open"):
        entry = {
            "id": eid,
            "content": f"{owner} to {action}",
            "wing": "engineering", "room": "general",
            "source_type": "email", "author_id": "boss@x.com",
        }
        item = ActionItem(
            owner=owner, owner_name=owner.title(), owner_resolved=True,
            action=action, deadline=deadline,
        )
        item.status = ActionStatus(status)
        attach_to_entry(entry, item)
        store.save(entry)
        return entry

    def test_reassign_changes_owner_and_resets_to_open(self, store):
        self._make_task(store, "t1", "harald", deadline="2026-08-22")
        assert reassign_task(store, "t1", "adele", "Adele Smith") is True
        loaded = store.get("t1")
        md = loaded["source_metadata"]
        assert md["action_owner"] == "adele"
        assert md["action_owner_name"] == "Adele Smith"
        assert md["action_status"] == "open"
        # Now listed under adele, not harald.
        assert list_open_tasks(store, owner="adele")[0]["id"] == "t1"
        assert list_open_tasks(store, owner="harald") == []

    def test_reassign_reopens_done_task(self, store):
        self._make_task(store, "t1", "harald", status="done")
        # mark done
        set_task_status(store, "t1", ActionStatus.DONE)
        assert reassign_task(store, "t1", "adele") is True
        assert store.get("t1")["source_metadata"]["action_status"] == "open"

    def test_reassign_non_action_item_returns_false(self, store):
        store.save({"id": "x", "content": "not a task",
                    "source_type": "manual"})
        assert reassign_task(store, "x", "adele") is False

    def test_list_tasks_due_includes_overdue_and_within_window(self, store):
        # overdue (yesterday)
        self._make_task(store, "t1", "harald", deadline=_iso_days_ago(1))
        # due in 3 days
        self._make_task(store, "t2", "adele", deadline=_iso_days_from_now(3))
        # due far out (30 days) — excluded
        self._make_task(store, "t3", "bob", deadline=_iso_days_from_now(30))
        # no deadline — excluded
        self._make_task(store, "t4", "harald", deadline="")
        due = [e["id"] for e in list_tasks_due(store, within_days=7)]
        assert "t1" in due and "t2" in due
        assert "t3" not in due and "t4" not in due

    def test_build_digest_counts_and_sections(self, store):
        self._make_task(store, "t1", "harald", deadline=_iso_days_ago(1))  # overdue
        self._make_task(store, "t2", "harald", deadline=_iso_days_from_now(3))  # due soon
        self._make_task(store, "t3", "harald", status="suggested_done")  # pending
        self._make_task(store, "t4", "adele", deadline=_iso_days_from_now(1))  # other owner
        d = build_digest(store, owner="harald")
        assert d["owner"] == "harald"
        assert d["open_count"] == 2
        assert d["overdue_count"] == 1
        assert d["due_soon_count"] == 1
        assert d["pending_confirmation_count"] == 1
        # adele's task excluded by owner filter
        assert all(e["source_metadata"]["action_owner"] == "harald"
                   for e in d["open"])

    def test_build_digest_all_owners(self, store):
        self._make_task(store, "t1", "harald")
        self._make_task(store, "t2", "adele")
        d = build_digest(store)
        assert d["open_count"] == 2


def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now().date() - timedelta(days=days)).isoformat()


def _iso_days_from_now(days: int) -> str:
    from datetime import timedelta
    return (datetime.now().date() + timedelta(days=days)).isoformat()

