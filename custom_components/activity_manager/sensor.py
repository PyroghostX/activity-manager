from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
import voluptuous as vol

from . import logic
from .const import DOMAIN
from homeassistant.components import homeassistant
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.entity_registry import async_get
from homeassistant.helpers.json import save_json
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import slugify
from homeassistant.util import dt
from homeassistant.util.json import JsonArrayType, load_json_array
from datetime import datetime, timedelta

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PERSISTENCE = ".activities_list.json"
# Permanent completion log, kept outside the recorder database
HISTORY = ".activities_history.json"

# Phone alerts: one notification per task, replaced/cleared by its tag
NOTIFY_TAG = "activity_manager_"
ACTION_DONE = "AM_DONE_"
ACTION_SNOOZE = "AM_SNOOZE_"
NOTIFY_CHECK = timedelta(minutes=1)


async def async_setup_entry(hass, config_entry, async_add_devices):
    data = hass.data[DOMAIN] = ActivityManager(hass, config_entry, async_add_devices)
    await data.async_load_activities()
    await data.async_load_history()
    activities = []

    for item in data.items:
        activities.append(ActivityEntity(hass, config_entry, item))

    async_add_devices(activities, True)
    data.async_start_notifier()


class ActivityManager:
    """Class to hold activity data."""

    def __init__(self, hass: HomeAssistant, entry, async_add_devices) -> None:
        """Initialize the shopping list."""

        self.hass = hass
        self.async_add_devices = async_add_devices
        self.items: JsonArrayType = []
        self.activities = {}
        self.entry = entry
        self.history = logic.new_history()
        self._history_writable = True
        self._history_lock = asyncio.Lock()

    async def async_add_activity(
        self,
        name,
        category,
        frequency,
        icon=None,
        last_completed=None,
        context=None,
        sharing=None,
    ):
        if last_completed is None:
            last_completed = dt.now().isoformat()

        if icon is None:
            icon = "mdi:checkbox-outline"

        # Handle both string and array formats for name
        names = name if isinstance(name, list) else [name]
        
        item = {
            "name": names[0],  # For backward compatibility
            "names": names,
            "current_name_index": 0,
            "category": category,
            "id": uuid.uuid4().hex,
            "last_completed": last_completed,
            "frequency": frequency,
            "frequency_ms": self._duration_to_ms(frequency),
            "icon": icon,
        }

        # Optional sharing fields (assignees, rotation, ...); raises
        # ValueError before anything is stored
        if sharing:
            logic.apply_edit(item, sharing)

        self.items.append(item)
        self.async_add_devices([ActivityEntity(self.hass, self.entry, item)], True)
        await self.update_entities()

        if logic.upsert_task(self.history, item, dt.now().isoformat()):
            await self.async_save_history()

        _LOGGER.debug("Added activity: %s", item)
        self.hass.bus.async_fire(
            "activity_manager_updated",
            {"action": "add", "item": item},
            context=context,
        )

        return item

    async def async_remove_activity(self, item_id=None, context=None):
        item = self.get_item(item_id)
        if item is None:
            return None

        entity_registry = async_get(self.hass)
        entity = next(
            (
                entry
                for idx, entry in entity_registry.entities.items()
                if entry.unique_id == item_id
            ),
            None,
        )

        self.items.remove(item)
        await self.async_clear_notification(item)
        if entity:
            entity_registry.async_remove(entity.entity_id)
        await self.update_entities()
        _LOGGER.debug("Removed activity: %s", item)

        # The task's completions stay in the history
        if logic.mark_removed(self.history, item_id, dt.now().isoformat()):
            await self.async_save_history()

        self.hass.bus.async_fire(
            "activity_manager_updated",
            {"action": "remove", "item": item},
            context=context,
        )

        return item

    async def async_update_activity(
        self,
        item_id,
        last_completed=None,
        category=None,
        frequency=None,
        context=None,
        icon=None,
        completed_by=None,
    ):
        item = self.get_item(item_id)
        if item is None:
            return None

        completion = None
        if last_completed:
            # A completion: cycles the name, moves the turn, logs it
            completion = logic.apply_completion(item, last_completed, completed_by)

        if category:
            item["category"] = category

        if frequency:
            item["frequency"] = frequency
            item["frequency_ms"] = self._duration_to_ms(frequency)

        if icon:
            item["icon"] = icon

        await self._async_refresh_entity(item["id"])
        await self.update_entities()
        _LOGGER.debug("Updated activity: %s", item)

        history_changed = logic.upsert_task(self.history, item, dt.now().isoformat())
        if completion:
            logic.add_completion(self.history, completion)
            await self.async_clear_notification(item)
        if completion or history_changed:
            await self.async_save_history()

        self.hass.bus.async_fire(
            "activity_manager_updated",
            {"action": "updated", "item": item},
            context=context,
        )

        return item

    async def async_edit_activity(self, item_id, changes, context=None):
        """Edit or correct a task. Not a completion, so nothing is logged.

        Raises ValueError for bad input; returns None for an unknown id.
        """
        item = self.get_item(item_id)
        if item is None:
            return None

        changed = logic.apply_edit(item, changes)
        if "notify" in changed or "last_completed" in changed:
            # Turned off, or a corrected due time: drop the current alert
            item.pop("notified", None)
            await self.async_clear_notification(item)
        if changed:
            await self._async_refresh_entity(item_id)
            await self.update_entities()
            _LOGGER.debug("Edited activity %s: %s", changed, item)

            # Renames and category changes go to the history's task list
            if logic.upsert_task(self.history, item, dt.now().isoformat()):
                await self.async_save_history()

            self.hass.bus.async_fire(
                "activity_manager_updated",
                {"action": "edited", "item": item},
                context=context,
            )

        return item

    async def async_touch_history(self, item) -> None:
        """Pick up a name change made outside the edit/update paths."""
        if logic.upsert_task(self.history, item, dt.now().isoformat()):
            await self.async_save_history()

    # --- Phone alerts ---------------------------------------------------------

    def async_start_notifier(self) -> None:
        """Check due tasks every minute and handle the alerts' buttons."""
        self.entry.async_on_unload(
            async_track_time_interval(self.hass, self._async_check_notifications, NOTIFY_CHECK)
        )
        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                "mobile_app_notification_action", self._async_notification_action
            )
        )

    def _notify_services(self, person) -> list:
        """notify.mobile_app_* services of the phones tracking `person`."""
        state = self.hass.states.get(person)
        if state is None:
            return []
        entities = async_get(self.hass)
        devices = dr.async_get(self.hass)
        services = []
        for tracker in state.attributes.get("device_trackers", []):
            entry = entities.async_get(tracker)
            if entry is None or entry.platform != "mobile_app" or not entry.device_id:
                continue
            device = devices.async_get(entry.device_id)
            if device is None:
                continue
            service = "mobile_app_" + slugify(device.name_by_user or device.name or "")
            if self.hass.services.has_service("notify", service) and service not in services:
                services.append(service)
        return services

    def _people(self) -> list:
        """Everyone with a phone to alert."""
        return sorted(
            state.entity_id
            for state in self.hass.states.async_all("person")
            if self._notify_services(state.entity_id)
        )

    def _display_names(self) -> dict:
        return {
            state.entity_id: state.attributes.get("friendly_name", state.entity_id)
            for state in self.hass.states.async_all("person")
        }

    def _is_home(self, person) -> bool:
        state = self.hass.states.get(person)
        return state is not None and state.state == "home"

    async def _async_check_notifications(self, _now=None) -> None:
        now = dt.now()
        people = self._people()
        names = self._display_names()
        sent = False
        for item in list(self.items):
            for person in logic.notify_targets(item, now, people, self._is_home, now.hour):
                await self._async_send(item, person, names)
                sent = True
        if sent:
            await self.update_entities()

    async def _async_send(self, item, person, names) -> None:
        title, message = logic.notification_text(item, person, names)
        data = {
            "tag": NOTIFY_TAG + item["id"],
            "group": "activity_manager",
            "actions": [
                {"action": ACTION_DONE + item["id"], "title": "Completed"},
                {"action": ACTION_SNOOZE + item["id"], "title": "Snooze 4 hours"},
            ],
        }
        if logic.get_notify(item) == "persistent":
            # Can't be swiped away; tapping it doesn't dismiss it either
            data.update(persistent=True, sticky="true")
        for service in self._notify_services(person):
            try:
                await self.hass.services.async_call(
                    "notify", service, {"title": title, "message": message, "data": data}
                )
            except Exception:  # noqa: BLE001 - one phone failing shouldn't stop the rest
                _LOGGER.exception("Could not send %s to %s", item["id"], service)

    async def async_clear_notification(self, item) -> None:
        """Remove the task's alert from every phone."""
        for person in self._people():
            for service in self._notify_services(person):
                try:
                    await self.hass.services.async_call(
                        "notify",
                        service,
                        {"message": "clear_notification", "data": {"tag": NOTIFY_TAG + item["id"]}},
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Could not clear %s on %s", item["id"], service)

    async def _async_notification_action(self, event) -> None:
        action = event.data.get("action") or ""
        if action.startswith(ACTION_DONE):
            item = self.get_item(action[len(ACTION_DONE):])
            if item is None:
                return
            # Done by whoever pressed Completed
            by = next(
                (
                    state.entity_id
                    for state in self.hass.states.async_all("person")
                    if event.context.user_id
                    and state.attributes.get("user_id") == event.context.user_id
                ),
                None,
            )
            await self.async_update_activity(
                item["id"], last_completed=dt.now().isoformat(), completed_by=by, context=event.context
            )
        elif action.startswith(ACTION_SNOOZE):
            item = self.get_item(action[len(ACTION_SNOOZE):])
            if item is None:
                return
            logic.apply_snooze(item, dt.now())
            await self.async_clear_notification(item)
            await self.update_entities()
            self.hass.bus.async_fire(
                "activity_manager_updated",
                {"action": "snoozed", "item": item},
                context=event.context,
            )

    def get_item(self, item_id):
        return next((itm for itm in self.items if itm["id"] == item_id), None)

    def item_with_status(self, item, now=None):
        """A copy of the item with who-is-responsible fields filled in."""
        return {**item, **logic.compute_status(item, now or dt.now())}

    def items_with_status(self):
        now = dt.now()
        return [self.item_with_status(item, now) for item in self.items]

    async def _async_refresh_entity(self, item_id):
        entity_registry = async_get(self.hass)
        for entity_id, entity_entry in entity_registry.entities.items():
            if entity_entry.unique_id == item_id:  # entity_entry.update()
                await self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": entity_entry.entity_id},
                )

    async def update_entities(self):
        await self.hass.async_add_executor_job(self.save)

    async def async_load_activities(self) -> None:
        """Load items."""

        def load() -> JsonArrayType:
            """Load the items synchronously."""

            items = load_json_array(self.hass.config.path(PERSISTENCE))
            # Fills in frequency_ms, names, icon for older formats
            return logic.normalize_items(items)

        self.items = await self.hass.async_add_executor_job(load)

    async def async_load_history(self) -> None:
        """Load the completion history, seeding it on first run."""
        path = self.hass.config.path(HISTORY)
        items = self.items

        def load():
            if not os.path.exists(path):
                _LOGGER.info("Starting %s from %d activities", HISTORY, len(items))
                return logic.seed_history(items), True, True
            try:
                with open(path, encoding="utf-8") as file:
                    history = logic.normalize_history(json.load(file))
            except ValueError as err:
                # Keep the unreadable file for recovery and start a new one
                backup = f"{path}.bad-{dt.now().strftime('%Y%m%d%H%M%S')}"
                try:
                    os.replace(path, backup)
                except OSError as move_err:
                    _LOGGER.error(
                        "Could not read %s (%s) or move it aside (%s); history is off",
                        path,
                        err,
                        move_err,
                    )
                    return logic.new_history(), False, False
                _LOGGER.error(
                    "Could not read %s (%s); moved it to %s and started a new history",
                    path,
                    err,
                    backup,
                )
                return logic.seed_history(items), True, True
            except OSError as err:
                # Don't overwrite a file we couldn't read
                _LOGGER.error("Could not read %s (%s); history is off", path, err)
                return logic.new_history(), False, False
            return history, logic.ensure_tasks(history, items), True

        self.history, changed, self._history_writable = (
            await self.hass.async_add_executor_job(load)
        )
        if changed:
            await self.async_save_history()

    async def async_save_history(self) -> None:
        if not self._history_writable:
            return
        # The lock keeps saves in order so the newest snapshot lands last
        async with self._history_lock:
            snapshot = copy.deepcopy(self.history)
            try:
                await self.hass.async_add_executor_job(self._save_history, snapshot)
            except Exception:  # noqa: BLE001 - never fail a completion over the log
                _LOGGER.exception("Could not save %s", HISTORY)

    def _save_history(self, history) -> None:
        save_json(self.hass.config.path(HISTORY), history, atomic_writes=True)

    def save(self) -> None:
        """Save the items."""
        items = self.items

        save_json(self.hass.config.path(PERSISTENCE), items)

    def _duration_to_ms(self, frequency) -> int:
        return logic.duration_to_ms(frequency)


class ActivityEntity(SensorEntity):
    """Representation of a sensor."""

    def __init__(self, hass, config, activity) -> None:
        """Initialize the sensor."""
        _attr_has_entity_name = True
        self._hass = hass
        self._config = config
        self._activity = activity
        self._id = self._activity["id"]
        
        # Get the first name for entity_id generation
        first_name = self._activity.get("name", "")
        if not first_name and "names" in self._activity and len(self._activity["names"]) > 0:
            first_name = self._activity["names"][0]
        
        # Use the ID to ensure unique entity_id even if names are duplicated
        self.entity_id = "sensor." + slugify(
            self._activity["category"] + "_" + first_name + "_" + self._id[:8]
        )
        
        self._attributes = {
            "category": self._activity["category"],
            "last_completed": self._activity["last_completed"],
            "frequency_ms": self._activity["frequency_ms"],
            "friendly_name": self.name,
            "id": self._activity["id"],
            "integration": DOMAIN,
            "names": self._activity.get("names", [first_name]),
            "current_name_index": self._activity.get("current_name_index", 0)
        }
        self._attributes.update(self._sharing_attributes(self._activity))

    @property
    def unique_id(self):
        """Return a unique ID to use for this sensor."""
        # return slugify(self._activity["category"] + "_" + self._activity["name"])
        return self._id

    @property
    def entity_id(self):
        return self.entity_id

    def entity_id(self, value):
        self.entity_id = value

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        if "names" in self._activity and len(self._activity["names"]) > 0:
            index = self._activity.get("current_name_index", 0)
            return self._activity["names"][index]
        # Backward compatibility for old format
        return self._activity.get("name", "")

    @property
    def state(self):
        """Return the state of the sensor."""
        return dt.as_local(
            dt.parse_datetime(self._activity["last_completed"])
        ) + timedelta(milliseconds=self._activity["frequency_ms"])

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._attributes

    @property
    def icon(self):
        """Return the state of the sensor."""
        return self._activity["icon"]

    @staticmethod
    def _sharing_attributes(item) -> dict:
        """Turn and escalation attributes, recomputed on every poll."""
        status = logic.compute_status(item, dt.now())
        return {
            key: status[key]
            for key in (
                "assignees",
                "rotation",
                "turn_order",
                "turn_index",
                "turn",
                "assigned_to",
                "escalated",
                "escalate_after",
                "last_completed_by",
            )
        } | {"notify": logic.get_notify(item), "snooze_until": item.get("snooze_until")}

    async def async_update(self) -> None:
        """Fetch new state data for the sensor.

        This is the only method that should fetch new data for Home Assistant.
        Sensors poll, so escalation flips here without a completion.
        """
        for item in self._hass.data[DOMAIN].items:
            if self._id == item["id"]:
                self._attributes["last_completed"] = item["last_completed"]
                self._attributes["category"] = item["category"]
                self._attributes["frequency_ms"] = item["frequency_ms"]
                self._attributes["icon"] = item["icon"]
                self._attributes["names"] = list(item.get("names", []))
                self._attributes["current_name_index"] = item.get("current_name_index", 0)
                # Update name attribute based on current name index
                if "names" in item and len(item["names"]) > 0:
                    index = item.get("current_name_index", 0)
                    self._attributes["friendly_name"] = item["names"][index]

                was_escalated = self._attributes.get("escalated")
                self._attributes.update(self._sharing_attributes(item))
                if self._attributes["escalated"] and not was_escalated:
                    # Lets cards refresh and automations notify everyone
                    self._hass.bus.async_fire(
                        "activity_manager_updated",
                        {"action": "escalated", "item": item},
                    )
