"""Tests for custom_components/activity_manager/logic.py.

Plain python, no pytest or Home Assistant needed:

    python3 tests/test_logic.py
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load logic.py by path so the package __init__ (which needs Home Assistant)
# is never imported.
LOGIC_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "activity_manager"
    / "logic.py"
)
_spec = importlib.util.spec_from_file_location("am_logic", LOGIC_PATH)
logic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logic)

A = "person.alex"
B = "person.sam"
GUEST = "person.guest"
TZ = timezone(timedelta(hours=-6))
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)

# Real-format item from /config/.activities_list.json
SAMPLE = {
    "name": "Clean cat",
    "names": [
        "Cat water filter: Clean",
        "Cat water filter: Clean2",
        "Cat water filter: Replace",
    ],
    "current_name_index": 1,
    "category": "Home",
    "id": "abc",
    "last_completed": "2026-10-06T21:53:00-06:00",
    "frequency": {"days": 10, "hours": 0, "minutes": 0, "seconds": 0},
    "frequency_ms": 864000000,
    "icon": "mdi:cat",
}


def iso(moment: datetime) -> str:
    return moment.isoformat()


def make_item(item_id="t1", days=1, **extra):
    item = {
        "name": "Dishes",
        "names": ["Dishes"],
        "current_name_index": 0,
        "category": "Home",
        "id": item_id,
        "last_completed": iso(T0),
        "frequency": {"days": days, "hours": 0, "minutes": 0, "seconds": 0},
        "icon": "mdi:silverware",
    }
    item.update(extra)
    return logic.normalize_item(item)


def turn(item, now=T0):
    return logic.compute_status(item, now)["turn"]


def complete(item, by, when=None):
    when = when or (parse(item["last_completed"]) + timedelta(hours=1))
    return logic.apply_completion(item, iso(when), by)


def parse(value):
    return logic.parse_time(value)


# --- Turns -------------------------------------------------------------------


def test_alternate_two_people():
    item = make_item(assignees=[A, B])
    status = logic.compute_status(item, T0)
    assert status["rotation"] == "alternate"
    assert status["turn_order"] == [A, B]
    assert status["turn"] == A and status["assigned_to"] == [A]

    entry = complete(item, A)  # A does their own turn: advances
    assert entry["turn"] == A and entry["by"] == A
    assert turn(item) == B

    entry = complete(item, A)  # A covers for B: B keeps the next turn
    assert entry["turn"] == B and entry["by"] == A
    assert turn(item) == B

    complete(item, B)  # B does it: back to A
    assert turn(item) == A
    assert item["last_completed_by"] == B


def test_pattern_a_a_b():
    item = make_item(assignees=[A, B], turn_order=[A, A, B])
    seen = []
    for _ in range(6):
        current = turn(item)
        seen.append(current)
        complete(item, current)
    assert seen == [A, A, B, A, A, B], seen

    # B covers A's second turn: still A's second turn afterwards
    assert turn(item) == A and item["turn_index"] == 0
    complete(item, A)
    assert item["turn_index"] == 1
    complete(item, B)
    assert item["turn_index"] == 1 and turn(item) == A


def test_fixed():
    item = make_item(assignees=[A, B], rotation="fixed")
    for by in (A, B, None, A):
        status = logic.compute_status(item, T0)
        assert status["turn"] == A and status["assigned_to"] == [A]
        complete(item, by)
    assert item.get("turn_index", 0) == 0
    # fixed = the first in the turn order
    item2 = make_item(assignees=[A, B], rotation="fixed", turn_order=[B, A])
    assert turn(item2) == B


def test_anyone():
    item = make_item(assignees=[A, B], rotation="anyone")
    status = logic.compute_status(item, T0)
    assert status["turn"] is None
    assert status["assigned_to"] == [A, B]
    entry = complete(item, B)
    assert entry["turn"] is None
    assert item.get("turn_index", 0) == 0
    # never "escalated", it already belongs to everyone
    late = T0 + timedelta(days=30)
    assert logic.compute_status(item, late)["escalated"] is False


def test_unknown_completer_advances_and_outsider_does_not():
    item = make_item(assignees=[A, B])
    complete(item, None)  # Node-RED / unknown: counts as the turn person
    assert turn(item) == B
    complete(item, "")  # empty string is unknown too
    assert turn(item) == A
    complete(item, GUEST)  # someone outside the group covered
    assert turn(item) == A


def test_single_assignee():
    item = make_item(assignees=[A])
    status = logic.compute_status(item, T0 + timedelta(days=10))
    assert status["turn"] == A and status["assigned_to"] == [A]
    assert status["escalated"] is False and status["escalate_after"] is None
    complete(item, A)
    assert turn(item) == A


# --- Escalation -------------------------------------------------------------


def test_escalation_before_and_after():
    item = make_item(assignees=[A, B], days=1)  # due T0 + 1 day
    due = T0 + timedelta(days=1)
    status = logic.compute_status(item, due + timedelta(hours=23))
    assert status["escalate_after"] == {"hours": 24}
    assert status["escalated"] is False and status["assigned_to"] == [A]
    status = logic.compute_status(item, due + timedelta(hours=24, minutes=1))
    assert status["escalated"] is True
    assert status["assigned_to"] == [A, B]
    assert status["turn"] == A  # still A's turn underneath

    # B covers while escalated: logged as escalated, A keeps the turn
    entry = logic.apply_completion(item, iso(due + timedelta(hours=30)), B)
    assert entry["escalated"] is True and entry["turn"] == A
    assert turn(item) == A
    # Done now, so no longer escalated
    assert logic.compute_status(item, due + timedelta(hours=31))["escalated"] is False


def test_escalation_custom_and_never():
    item = make_item(assignees=[A, B], escalate_after={"hours": 2})
    due = T0 + timedelta(days=1)
    assert logic.compute_status(item, due + timedelta(hours=1))["escalated"] is False
    assert logic.compute_status(item, due + timedelta(hours=3))["escalated"] is True

    never = make_item(assignees=[A, B], escalate_after=None)
    status = logic.compute_status(never, due + timedelta(days=60))
    assert status["escalated"] is False and status["assigned_to"] == [A]
    assert status["escalate_after"] is None


def test_escalation_fixed_rotation():
    item = make_item(assignees=[A, B], rotation="fixed")
    due = T0 + timedelta(days=1)
    status = logic.compute_status(item, due + timedelta(hours=25))
    assert status["escalated"] is True and status["assigned_to"] == [A, B]


# --- Edits -------------------------------------------------------------------


def test_edit_fields_and_no_history():
    item = make_item(names=["Clean", "Clean2", "Replace"], current_name_index=1)
    history = logic.seed_history([item])
    count = len(history["completions"])

    changed = logic.apply_edit(
        item,
        {
            "names": ["Clean", "Wipe", "Clean2", "Replace"],
            "category": "Sam",
            "frequency": {"days": 2, "hours": 3, "minutes": 0},
            "icon": "mdi:cat",
            "last_completed": "2026-10-05T08:00:00-06:00",
        },
    )
    assert {"names", "category", "frequency", "frequency_ms", "icon", "last_completed"} <= set(changed)
    # Same next name, now at index 2; no cycling for a correction
    assert item["names"][item["current_name_index"]] == "Clean2"
    assert item["frequency_ms"] == (2 * 24 + 3) * 3600 * 1000
    assert item["last_completed"] == "2026-10-05T08:00:00-06:00"
    assert "last_completed_by" not in item

    # The history only learns the new category/name; no completion
    assert logic.upsert_task(history, item, iso(T0)) is True
    assert history["tasks"][item["id"]]["category"] == "Sam"
    assert len(history["completions"]) == count
    assert logic.upsert_task(history, item, iso(T0)) is False


def test_edit_last_completed_keeps_turn():
    item = make_item(assignees=[A, B])
    complete(item, A)
    assert turn(item) == B
    logic.apply_edit(item, {"last_completed": iso(T0 - timedelta(days=3))})
    assert turn(item) == B
    assert item["last_completed_by"] == A


def test_edit_validation_leaves_item_alone():
    item = make_item(assignees=[A, B])
    before = copy.deepcopy(item)
    bad_edits = [
        {"rotation": "sometimes"},
        {"turn_order": [A, GUEST]},
        {"assignees": ["alex"]},
        {"names": []},
        {"category": "  "},
        {"frequency": {"weeks": 1}},
        {"frequency": {"days": -1}},
        {"last_completed": "yesterday"},
        {"next_person": GUEST},
        {"escalate_after": "24h"},
        {"color": "red"},
        # valid part first, invalid later: nothing may stick
        {"category": "Sam", "rotation": "nope"},
    ]
    for edit in bad_edits:
        try:
            logic.apply_edit(item, edit)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted bad edit {edit}")
        assert item == before, edit


def test_edit_sharing_fields():
    item = make_item()
    assert logic.compute_status(item, T0)["assigned_to"] == []

    logic.apply_edit(item, {"assignees": [A, B, A]})
    assert item["assignees"] == [A, B]
    assert turn(item) == A

    logic.apply_edit(item, {"next_person": B})
    assert turn(item) == B

    # A custom pattern keeps B as next up
    logic.apply_edit(item, {"turn_order": [A, A, B]})
    assert item["turn_order"] == [A, A, B] and turn(item) == B

    # next_person already pointing there keeps the position
    logic.apply_edit(item, {"turn_index": 1})
    assert item["turn_index"] == 1 and turn(item) == A
    logic.apply_edit(item, {"next_person": A})
    assert item["turn_index"] == 1

    # Adding a person appends them to the pattern; next up stays
    C = "person.jordan"
    logic.apply_edit(item, {"assignees": [A, B, C]})
    assert item["turn_order"] == [A, A, B, C] and turn(item) == A
    # Removing a person drops them from the pattern
    logic.apply_edit(item, {"assignees": [A, B]})
    assert item["turn_order"] == [A, A, B]

    # A pattern equal to the assignees is not stored
    logic.apply_edit(item, {"turn_order": [A, B]})
    assert "turn_order" not in item

    # fixed + next_person moves that person to the front
    logic.apply_edit(item, {"rotation": "fixed", "next_person": B})
    assert logic.compute_status(item, T0)["turn_order"] == [B, A]
    assert turn(item) == B

    logic.apply_edit(item, {"escalate_after": None})
    assert item["escalate_after"] is None
    logic.apply_edit(item, {"escalate_after": {"days": 1, "hours": 12}})
    assert logic.compute_status(item, T0)["escalate_after"] == {"days": 1, "hours": 12}

    # No assignees: back to an ordinary task
    logic.apply_edit(item, {"assignees": []})
    for key in logic.SHARING_KEYS:
        assert key not in item, key
    status = logic.compute_status(item, T0 + timedelta(days=9))
    assert status["assigned_to"] == [] and status["escalated"] is False


def test_add_with_sharing_via_edit():
    item = make_item()
    logic.apply_edit(
        item,
        {"assignees": [A, B], "rotation": "alternate", "turn_order": [A, B, B], "turn_index": 1},
    )
    assert turn(item) == B
    assert item["turn_order"] == [A, B, B]


# --- History -------------------------------------------------------------------


def test_history_seed_and_removed_task():
    items = [make_item("t1"), make_item("t2", names=["Trash", "Recycling"])]
    items[1]["last_completed"] = iso(T0 + timedelta(hours=5))
    history = logic.seed_history(items)
    assert history["version"] == 1
    assert set(history["tasks"]) == {"t1", "t2"}
    assert history["tasks"]["t2"] == {
        "name": "Trash",
        "category": "Home",
        "created": None,
        "removed": None,
    }
    assert len(history["completions"]) == 2
    for entry in history["completions"]:
        assert entry["by"] is None and entry["source"] == "import"
        assert entry["turn"] is None and entry["escalated"] is False

    # Live completions are added and listed newest first
    entry = logic.apply_completion(items[0], iso(T0 + timedelta(days=1)), A)
    logic.add_completion(history, entry)
    rows = logic.query_history(history)
    assert [r["task_id"] for r in rows] == ["t1", "t2", "t1"]
    assert rows[0]["task_name"] == "Dishes" and rows[0]["by"] == A
    assert logic.query_history(history, limit=1) == rows[:1]

    # Removing keeps everything, flagged
    assert logic.mark_removed(history, "t1", iso(T0 + timedelta(days=2))) is True
    assert logic.mark_removed(history, "t1", iso(T0 + timedelta(days=3))) is False
    rows = logic.query_history(history, item_id="t1")
    assert len(rows) == 2
    assert all(r["task_removed"] == iso(T0 + timedelta(days=2)) for r in rows)
    assert history["tasks"]["t1"]["removed"] == iso(T0 + timedelta(days=2))

    # Survives a JSON round trip
    reloaded = logic.normalize_history(json.loads(json.dumps(history)))
    assert reloaded == history


def test_history_backdated_completion_sorts_by_time():
    item = make_item()
    history = logic.seed_history([item])
    late = logic.apply_completion(item, iso(T0 + timedelta(days=2)), A)
    logic.add_completion(history, late)
    early = logic.apply_completion(item, iso(T0 + timedelta(days=1)), B)
    logic.add_completion(history, early)
    rows = logic.query_history(history, item_id=item["id"])
    assert [r["by"] for r in rows] == [A, B, None]


def test_history_new_task_and_ensure_tasks():
    item = make_item("t1")
    history = logic.seed_history([item])
    added = make_item("t9")
    assert logic.upsert_task(history, added, iso(T0)) is True
    assert history["tasks"]["t9"]["created"] == iso(T0)
    assert len(history["completions"]) == 1  # adding a task is not a completion

    # A task added while an older version ran is picked up on load
    other = make_item("t5")
    assert logic.ensure_tasks(history, [item, added, other]) is True
    assert "t5" in history["tasks"]
    assert logic.ensure_tasks(history, [item, added, other]) is False


def test_history_rejects_bad_file():
    for bad in ([], "x", {"tasks": [], "completions": []}):
        try:
            logic.normalize_history(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert logic.normalize_history({}) == logic.new_history()


# --- Old data ------------------------------------------------------------------


def test_real_sample_loads_and_works():
    items = logic.normalize_items(json.loads(json.dumps([SAMPLE])))
    item = items[0]
    assert item["frequency_ms"] == 864000000
    assert item["names"] == SAMPLE["names"] and item["current_name_index"] == 1
    assert item["icon"] == "mdi:cat"
    # No sharing fields: behaves as before
    for key in logic.SHARING_KEYS:
        assert key not in item
    now = parse("2026-10-08T12:00:00-06:00")
    status = logic.compute_status(item, now)
    assert status == {
        "assignees": [],
        "rotation": None,
        "turn_order": [],
        "turn_index": 0,
        "turn": None,
        "assigned_to": [],
        "escalated": False,
        "escalate_after": None,
        "last_completed_by": None,
    }
    assert logic.due_time(item) == parse("2026-10-16T21:53:00-06:00")

    history = logic.seed_history(items)
    assert history["tasks"]["abc"]["name"] == "Cat water filter: Clean"
    assert history["completions"] == [
        {
            "task_id": "abc",
            "at": "2026-10-06T21:53:00-06:00",
            "by": None,
            "turn": None,
            "escalated": False,
            "source": "import",
        }
    ]

    # Node-RED style completion (no person): name cycles like before
    entry = logic.apply_completion(item, "2026-10-08T12:00:00-06:00", None)
    assert entry["name"] == "Cat water filter: Clean2"
    assert item["current_name_index"] == 2
    assert "turn_index" not in item
    json.dumps(item)  # still plain JSON


def test_legacy_items():
    old = [
        {"name": "Mow", "category": "Home", "id": "m1", "last_completed": iso(T0), "frequency": 7},
        {"name": "Odd", "category": "Home", "id": "o1", "last_completed": iso(T0), "frequency_ms": 1000},
        {"name": "Bad index", "names": ["x", "y"], "current_name_index": 5, "category": "Home",
         "id": "b1", "last_completed": iso(T0), "frequency": {"days": 1}},
        "not an item",
    ]
    items = logic.normalize_items(old)
    assert [i["id"] for i in items] == ["m1", "o1", "b1"]
    assert items[0]["frequency_ms"] == 7 * 86400000
    assert items[0]["names"] == ["Mow"] and items[0]["icon"] == logic.DEFAULT_ICON
    assert items[1]["frequency_ms"] == 1000 and items[1]["names"] == ["Odd"]
    assert items[2]["current_name_index"] == 1


def main() -> int:
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
