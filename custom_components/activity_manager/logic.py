"""Task logic for Activity Manager: loading, turns, escalation, edits, history.

Nothing in here imports Home Assistant, so tests/test_logic.py can run it with
plain python. Times are ISO strings in storage; naive values are read as UTC.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

DEFAULT_ICON = "mdi:checkbox-outline"

# Shared tasks
ROTATIONS = ("alternate", "fixed", "anyone")
DEFAULT_ROTATION = "alternate"
DEFAULT_ESCALATE_AFTER = {"hours": 24}
SHARING_KEYS = ("assignees", "rotation", "turn_order", "turn_index", "escalate_after")

# Fields activity_manager/edit and the edit_activity service accept
EDIT_KEYS = (
    "names",
    "category",
    "frequency",
    "icon",
    "last_completed",
    "assignees",
    "rotation",
    "turn_order",
    "turn_index",
    "next_person",
    "escalate_after",
)

HISTORY_VERSION = 1

_DURATION_MS = {
    "days": 24 * 60 * 60 * 1000,
    "hours": 60 * 60 * 1000,
    "minutes": 60 * 1000,
    "seconds": 1000,
}
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# --- Times and durations -------------------------------------------------


def parse_time(value) -> datetime | None:
    """Parse an ISO string (or datetime) to an aware datetime, None if invalid."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def duration_to_ms(frequency) -> int:
    """Duration dict (or a plain number of days, as old versions stored) to ms."""
    # prior versions stored a single int for number of days
    try:
        return int(frequency) * _DURATION_MS["days"]
    except (TypeError, ValueError):
        pass
    if not isinstance(frequency, dict):
        return 0
    total = 0
    for key, factor in _DURATION_MS.items():
        if key in frequency:
            total += frequency[key] * factor
    return int(total)


def validate_duration(value, field="duration") -> dict:
    """Check a duration dict like {"days": 1, "hours": 2}; returns a clean copy."""
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a duration like {{'hours': 24}}")
    clean = {}
    for key, amount in value.items():
        if key not in _DURATION_MS:
            raise ValueError(f"{field}: unknown unit '{key}'")
        if isinstance(amount, bool):
            raise ValueError(f"{field}: {key} must be a number")
        try:
            number = float(amount)
        except (TypeError, ValueError) as err:
            raise ValueError(f"{field}: {key} must be a number") from err
        if number < 0:
            raise ValueError(f"{field}: {key} can't be negative")
        clean[key] = int(number) if number.is_integer() else number
    return clean


def due_time(item) -> datetime | None:
    """When the task is due: last completed plus the frequency."""
    completed = parse_time(item.get("last_completed"))
    if completed is None:
        return None
    frequency_ms = item.get("frequency_ms")
    if not isinstance(frequency_ms, (int, float)):
        frequency_ms = duration_to_ms(item.get("frequency"))
    return completed + timedelta(milliseconds=frequency_ms)


# --- Loading ---------------------------------------------------------------


def normalize_item(item: dict) -> dict:
    """Fill in fields that older versions did not store (load migration)."""
    if "frequency" not in item:
        if "frequency_ms" in item:
            _LOGGER.error("No frequency, using frequency_ms: %s", item)
        else:
            item["frequency_ms"] = duration_to_ms(7)
            _LOGGER.error("Added missing frequency: %s", item)
    else:
        item["frequency_ms"] = duration_to_ms(item["frequency"])

    # Add names array and current_name_index if they don't exist (for migration)
    if not isinstance(item.get("names"), list) or not item["names"]:
        item["names"] = [item.get("name", "")]
        item["current_name_index"] = 0
    index = item.get("current_name_index", 0)
    if not isinstance(index, int) or isinstance(index, bool):
        index = 0
    item["current_name_index"] = index % len(item["names"])

    if "icon" not in item:
        item["icon"] = DEFAULT_ICON

    return item


def normalize_items(items) -> list:
    """Normalize every item loaded from .activities_list.json."""
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            _LOGGER.error("Skipping invalid activity: %s", item)
            continue
        result.append(normalize_item(item))
    return result


def primary_name(item) -> str:
    names = item.get("names")
    if isinstance(names, list) and names:
        return names[0]
    return item.get("name", "")


def current_name(item) -> str:
    names = item.get("names")
    if isinstance(names, list) and names:
        index = item.get("current_name_index", 0)
        if isinstance(index, int) and 0 <= index < len(names):
            return names[index]
        return names[0]
    return item.get("name", "")


# --- Shared tasks: turns and escalation ------------------------------------


def get_assignees(item) -> list:
    raw = item.get("assignees")
    if not isinstance(raw, list):
        return []
    result = []
    for person in raw:
        if isinstance(person, str) and person and person not in result:
            result.append(person)
    return result


def get_rotation(item) -> str:
    rotation = item.get("rotation")
    return rotation if rotation in ROTATIONS else DEFAULT_ROTATION


def get_turn_order(item, assignees=None) -> list:
    """The turn pattern, limited to current assignees; defaults to assignees."""
    if assignees is None:
        assignees = get_assignees(item)
    raw = item.get("turn_order")
    order = []
    if isinstance(raw, list):
        order = [p for p in raw if isinstance(p, str) and p in assignees]
    return order or list(assignees)


def get_turn_index(item, order) -> int:
    if not order:
        return 0
    index = item.get("turn_index", 0)
    if not isinstance(index, int) or isinstance(index, bool):
        index = 0
    return index % len(order)


def get_escalate_after(item, assignees=None):
    """Delay after the due time before everyone gets the task.

    A stored null means never; when nothing is stored, shared tasks with
    2+ people use DEFAULT_ESCALATE_AFTER.
    """
    if assignees is None:
        assignees = get_assignees(item)
    if "escalate_after" in item:
        value = item["escalate_after"]
        return dict(value) if isinstance(value, dict) else None
    if len(assignees) > 1:
        return dict(DEFAULT_ESCALATE_AFTER)
    return None


def compute_status(item, now) -> dict:
    """Who is responsible right now. Computed, never stored on the item."""
    now = parse_time(now)
    assignees = get_assignees(item)
    status = {
        "assignees": assignees,
        "rotation": None,
        "turn_order": [],
        "turn_index": 0,
        "turn": None,
        "assigned_to": [],
        "escalated": False,
        "escalate_after": get_escalate_after(item, assignees),
        "last_completed_by": item.get("last_completed_by"),
    }
    if not assignees:
        return status

    rotation = get_rotation(item)
    order = get_turn_order(item, assignees)
    index = 0 if rotation == "fixed" else get_turn_index(item, order)
    turn = None if rotation == "anyone" else order[index]

    escalated = False
    escalate_after = status["escalate_after"]
    if rotation != "anyone" and len(assignees) > 1 and escalate_after is not None:
        due = due_time(item)
        if due is not None and now is not None:
            delay = timedelta(milliseconds=duration_to_ms(escalate_after))
            escalated = now > due + delay

    status.update(
        rotation=rotation,
        turn_order=order,
        turn_index=index,
        turn=turn,
        assigned_to=list(assignees) if rotation == "anyone" or escalated else [turn],
        escalated=escalated,
    )
    return status


def apply_completion(item, at, by=None) -> dict:
    """Mark the task done at `at` (ISO) by person `by` (or None if unknown).

    Cycles the name, advances the turn when the person whose turn it was (or
    nobody in particular) did it, and returns the history entry to record.
    Someone covering for the turn person leaves the turn where it is.
    """
    by = by or None
    status = compute_status(item, at)
    entry = {
        "task_id": item.get("id"),
        "at": at,
        "by": by,
        "turn": status["turn"],
        "escalated": status["escalated"],
    }
    name = current_name(item)
    if name:
        entry["name"] = name

    if status["assignees"] and status["rotation"] == "alternate":
        if by is None or by == status["turn"]:
            item["turn_index"] = (status["turn_index"] + 1) % len(status["turn_order"])

    # Cycle to the next name when completing the activity
    names = item.get("names")
    if isinstance(names, list) and len(names) > 1:
        index = item.get("current_name_index", 0)
        if not isinstance(index, int):
            index = 0
        item["current_name_index"] = (index + 1) % len(names)

    item["last_completed"] = at
    item["last_completed_by"] = by
    return entry


# --- Edits -----------------------------------------------------------------


def _person_list(value, field, unique) -> list:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list of person entity ids")
    result = []
    for person in value:
        if not isinstance(person, str) or not person.strip().startswith("person."):
            raise ValueError(f"{field}: {person!r} is not a person entity id")
        person = person.strip()
        if unique and person in result:
            continue
        result.append(person)
    return result


def _index_for(order, person, prefer) -> int:
    """Index of `person` in `order`, keeping `prefer` if it already points there."""
    if not order:
        return 0
    if 0 <= prefer < len(order) and order[prefer] == person:
        return prefer
    if person in order:
        return order.index(person)
    return prefer % len(order)


def apply_edit(item, changes) -> list:
    """Edit a task in place. Not a completion: no name cycling, no history,
    and the turn only moves when turn fields are given.

    Returns the changed keys. On bad input raises ValueError and leaves the
    item untouched.
    """
    unknown = set(changes) - set(EDIT_KEYS)
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")

    new = copy.deepcopy(item)

    old_assignees = get_assignees(item)
    old_order = get_turn_order(item, old_assignees)
    old_index = get_turn_index(item, old_order)
    old_person = old_order[old_index] if old_order else None

    if "names" in changes:
        names = changes["names"]
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple)):
            raise ValueError("names must be a list")
        names = [str(n).strip() for n in names if str(n).strip()]
        if not names:
            raise ValueError("A task needs at least one name")
        # Keep pointing at the same next name if it is still there
        old_names = item.get("names") or []
        index = item.get("current_name_index", 0)
        if not isinstance(index, int):
            index = 0
        next_name = old_names[index] if 0 <= index < len(old_names) else None
        new["names"] = names
        new["name"] = names[0]
        if next_name in names:
            new["current_name_index"] = _index_for(names, next_name, index)
        else:
            new["current_name_index"] = min(max(index, 0), len(names) - 1)

    if "category" in changes:
        category = changes["category"]
        if not isinstance(category, str) or not category.strip():
            raise ValueError("Category can't be empty")
        new["category"] = category.strip()

    if "frequency" in changes:
        frequency = validate_duration(changes["frequency"], "frequency")
        new["frequency"] = frequency
        new["frequency_ms"] = duration_to_ms(frequency)

    if "icon" in changes:
        icon = changes["icon"]
        if not isinstance(icon, str) or not icon.strip():
            raise ValueError("Icon can't be empty")
        new["icon"] = icon.strip()

    if "last_completed" in changes:
        # A correction only: the name, turn and history stay as they are
        if parse_time(changes["last_completed"]) is None:
            raise ValueError("last_completed must be an ISO date and time")
        new["last_completed"] = changes["last_completed"]

    if "assignees" in changes:
        assignees = _person_list(changes["assignees"], "assignees", unique=True)
        if not assignees:
            for key in SHARING_KEYS:
                new.pop(key, None)
        else:
            new["assignees"] = assignees
            if isinstance(new.get("turn_order"), list):
                # Keep a custom pattern: drop people who left, add newcomers
                kept = [p for p in new["turn_order"] if p in assignees]
                new["turn_order"] = kept + [p for p in assignees if p not in kept]

    assignees = get_assignees(new)
    if assignees:
        if "rotation" in changes:
            if changes["rotation"] not in ROTATIONS:
                raise ValueError(f"rotation must be one of {', '.join(ROTATIONS)}")
            new["rotation"] = changes["rotation"]

        if "turn_order" in changes:
            order = _person_list(changes["turn_order"], "turn_order", unique=False)
            outsiders = [p for p in order if p not in assignees]
            if outsiders:
                raise ValueError(f"turn_order has people who don't share the task: {', '.join(outsiders)}")
            new["turn_order"] = order

        if "next_person" in changes and changes["next_person"] and get_rotation(new) == "fixed":
            # "Always the same person" is the first one in the pattern
            person = changes["next_person"]
            order = get_turn_order(new, assignees)
            if person not in order:
                raise ValueError(f"next_person {person} doesn't share the task")
            order.remove(person)
            new["turn_order"] = [person] + order

        # A pattern that is just the assignees in order is the default
        if "turn_order" in new and get_turn_order(new, assignees) == assignees:
            new.pop("turn_order")

        order = get_turn_order(new, assignees)
        if "next_person" in changes and changes["next_person"]:
            person = changes["next_person"]
            if person not in order:
                raise ValueError(f"next_person {person} doesn't share the task")
            new["turn_index"] = _index_for(order, person, old_index if order == old_order else 0)
        elif "turn_index" in changes:
            index = changes["turn_index"]
            if isinstance(index, bool) or not isinstance(index, (int, float)) or int(index) != index:
                raise ValueError("turn_index must be a whole number")
            new["turn_index"] = int(index) % len(order)
        elif order != old_order:
            # Same person stays next up when the pattern or people change
            new["turn_index"] = _index_for(order, old_person, old_index)

        if "escalate_after" in changes:
            value = changes["escalate_after"]
            new["escalate_after"] = (
                None if value is None else validate_duration(value, "escalate_after")
            )

    changed = sorted(
        key
        for key in set(item) | set(new)
        if key not in item or key not in new or item[key] != new[key]
    )
    item.clear()
    item.update(new)
    return changed


# --- History -----------------------------------------------------------------


def new_history() -> dict:
    return {"version": HISTORY_VERSION, "tasks": {}, "completions": []}


def normalize_history(data) -> dict:
    """Check the structure read from .activities_history.json."""
    if not isinstance(data, dict):
        raise ValueError("history file is not a JSON object")
    tasks = data.get("tasks")
    completions = data.get("completions")
    if tasks is None:
        tasks = {}
    if completions is None:
        completions = []
    if not isinstance(tasks, dict) or not isinstance(completions, list):
        raise ValueError("history file has the wrong structure")
    data["version"] = data.get("version", HISTORY_VERSION)
    data["tasks"] = tasks
    data["completions"] = [c for c in completions if isinstance(c, dict)]
    return data


def _task_record(item, created) -> dict:
    return {
        "name": primary_name(item),
        "category": item.get("category"),
        "created": created,
        "removed": None,
    }


def _import_completion(item) -> dict:
    return {
        "task_id": item["id"],
        "at": item["last_completed"],
        "by": None,
        "turn": None,
        "escalated": False,
        "source": "import",
    }


def ensure_tasks(history, items) -> bool:
    """Add tasks the history has never seen, with their last completion.

    Used to seed a new history file, and for tasks added while an older
    version of the integration was running. Returns True if anything changed.
    """
    changed = False
    for item in items:
        if item.get("id") in history["tasks"]:
            continue
        # Creation time is unknown for tasks that existed before the history
        history["tasks"][item["id"]] = _task_record(item, None)
        if parse_time(item.get("last_completed")) is not None:
            history["completions"].append(_import_completion(item))
        changed = True
    return changed


def seed_history(items) -> dict:
    """First run: one task record and one imported completion per item."""
    history = new_history()
    ensure_tasks(history, items)
    return history


def upsert_task(history, item, now) -> bool:
    """Record a new task, or a rename / category change. True if changed."""
    tasks = history["tasks"]
    record = tasks.get(item["id"])
    if record is None:
        tasks[item["id"]] = _task_record(item, now)
        return True
    changed = False
    for key, value in (("name", primary_name(item)), ("category", item.get("category"))):
        if record.get(key) != value:
            record[key] = value
            changed = True
    return changed


def mark_removed(history, task_id, now) -> bool:
    """Flag a task as removed; its completions stay."""
    record = history["tasks"].get(task_id)
    if record is None or record.get("removed"):
        return False
    record["removed"] = now
    return True


def add_completion(history, entry) -> None:
    history["completions"].append(entry)


def query_history(history, item_id=None, limit=None) -> list:
    """Completions newest first, each with its task's current name."""
    tasks = history.get("tasks", {})
    rows = []
    for position, completion in enumerate(history.get("completions", [])):
        if item_id is not None and completion.get("task_id") != item_id:
            continue
        task = tasks.get(completion.get("task_id")) or {}
        row = dict(completion)
        row["task_name"] = task.get("name")
        row.setdefault("name", task.get("name"))
        row["category"] = task.get("category")
        row["task_removed"] = task.get("removed")
        rows.append((parse_time(completion.get("at")) or _EPOCH, position, row))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    result = [r[2] for r in rows]
    if limit:
        result = result[:limit]
    return result
