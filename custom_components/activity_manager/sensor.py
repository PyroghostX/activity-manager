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


async def async_setup_entry(hass, config_entry, async_add_devices):
    data = hass.data[DOMAIN] = ActivityManager(hass, config_entry, async_add_devices)
    await data.async_load_activities()
    await data.async_load_history()
    activities = []

    for item in data.items:
        activities.append(ActivityEntity(hass, config_entry, item))

    async_add_devices(activities, True)


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
        }

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
