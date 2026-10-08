from __future__ import annotations
from typing import Any
from datetime import datetime, timedelta
import logging
import voluptuous as vol
import uuid
import json
from homeassistant.helpers.json import save_json
from homeassistant.components import websocket_api
from homeassistant.helpers.entity_registry import async_get
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.util.json import JsonArrayType, load_json_array
from homeassistant import config_entries
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify
from homeassistant.util import dt
from . import logic
from .const import DOMAIN
from .utils import dt_as_local

_LOGGER = logging.getLogger(__name__)

PERSISTENCE = ".activities_list.json"

# Sharing fields activity_manager/add accepts on top of the basic ones
ADD_SHARING_KEYS = ("assignees", "rotation", "turn_order", "turn_index", "next_person", "escalate_after")


def _local_iso(value) -> str:
    """ISO string (or datetime) to a local ISO string; ValueError if invalid."""
    parsed = value if isinstance(value, datetime) else dt.parse_datetime(str(value))
    if parsed is None:
        raise ValueError(f"Invalid date and time: {value}")
    return dt.as_local(parsed).isoformat()


def _duration_value(value):
    """Duration dict as is; "HH:MM:SS" or seconds become a dict."""
    if value is None or isinstance(value, dict):
        return value
    try:
        delta = cv.time_period(value)
    except vol.Invalid as err:
        raise ValueError(f"Invalid duration: {value}") from err
    total = int(delta.total_seconds())
    return {
        "days": total // 86400,
        "hours": total % 86400 // 3600,
        "minutes": total % 3600 // 60,
        "seconds": total % 60,
    }


def _string_list(value) -> list:
    """A list, or a comma-separated string, as a list of strings."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    raise ValueError(f"Expected a list, got {value!r}")


def _edit_changes(data, keys=logic.EDIT_KEYS) -> dict:
    """Pick and convert edit fields from service or websocket data."""
    changes = {}
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        if key in ("frequency", "escalate_after"):
            value = _duration_value(value)
        elif key == "last_completed":
            value = _local_iso(value)
        elif key in ("assignees", "turn_order"):
            value = _string_list(value)
        elif key == "names":
            value = [value] if isinstance(value, str) else _string_list(value)
        elif key == "turn_index":
            try:
                value = int(float(value))
            except (TypeError, ValueError) as err:
                raise ValueError("turn_index must be a whole number") from err
        changes[key] = value
    return changes


def _item_ids(hass: HomeAssistant, entity_ids) -> list:
    """Activity ids (entity unique ids) for one entity id or a list of them."""
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    entity_registry = async_get(hass)
    result = []
    for entity_id in entity_ids or []:
        entity = entity_registry.entities.get(entity_id)
        if entity:
            result.append(entity.unique_id)
    return result


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialize the activity."""

    if DOMAIN not in config:
        return True

    # hass.async_create_task(
    #     discovery.async_load_platform(hass, "sensor", DOMAIN, None, hass_config=config)
    # )

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_IMPORT}
        )
    )

    return True


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> bool:
    """Set up Activity Manager from a config entry."""
    # Add sensor
    await hass.config_entries.async_forward_entry_setups(config_entry, ["sensor"])

    async def add_item_service(call: ServiceCall) -> None:
        """Add an item with `name`."""
        data = hass.data[DOMAIN]

        name = call.data.get("name")
        names = call.data.get("names")
        category = call.data.get("category")
        frequency_str = call.data.get("frequency")
        last_completed = call.data.get("last_completed")
        icon = call.data.get("icon")

        # Handle different input formats:
        # 1. If names array is provided, use it
        # 2. If name is a list (from frontend parsing comma-separated), use it
        # 3. If name is a string, wrap it in a list
        if names:
            names_to_use = names
        elif isinstance(name, list):
            names_to_use = name
        elif name:
            names_to_use = [name]
        else:
            names_to_use = ["Unnamed Activity"]

        if last_completed:
            last_completed = dt_as_local(last_completed)
        else:
            last_completed = dt.now().isoformat()

        await data.async_add_activity(
            names_to_use, category, frequency_str, icon=icon, last_completed=last_completed
        )

    async def remove_item_service(call: ServiceCall) -> None:
        data = hass.data[DOMAIN]

        entity_id = call.data.get("entity_id")

        if entity_id:
            entity_registry = async_get(hass)
            entity = entity_registry.entities.get(entity_id)
            if entity:
                await data.async_remove_activity(entity.unique_id)

    async def update_item_service(call: ServiceCall) -> None:
        data = hass.data[DOMAIN]
        entity_id = call.data.get("entity_id")
        last_completed = call.data.get("last_completed")
        category = call.data.get("category")
        now = call.data.get("now")
        frequency = call.data.get("frequency")
        icon = call.data.get("icon")
        # Person entity id of whoever did it; leaving it out (Node-RED) counts
        # as whoever's turn it was
        completed_by = call.data.get("completed_by") or None
        if isinstance(completed_by, (list, tuple)):
            completed_by = completed_by[0] if completed_by else None
        if completed_by is not None and not isinstance(completed_by, str):
            raise ServiceValidationError("completed_by must be a person entity id")

        if last_completed:
            last_completed = dt_as_local(last_completed)

        if now:
            last_completed = dt.now().isoformat()

        if entity_id:
            entity_registry = async_get(hass)
            entity = entity_registry.entities.get(entity_id)
            if entity:
                await data.async_update_activity(
                    entity.unique_id,
                    last_completed=last_completed,
                    category=category,
                    frequency=frequency,
                    icon=icon,
                    completed_by=completed_by,
                )

    async def edit_item_service(call: ServiceCall) -> None:
        """Edit or correct an activity. Never counts as a completion."""
        data = hass.data[DOMAIN]
        item_ids = _item_ids(hass, call.data.get("entity_id"))
        if not item_ids:
            raise ServiceValidationError(
                f"No activity found for {call.data.get('entity_id')}"
            )
        try:
            changes = _edit_changes(call.data)
            for item_id in item_ids:
                await data.async_edit_activity(item_id, changes, context=call.context)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def add_name_to_activity_service(call: ServiceCall) -> None:
        """Add a name to an activity's name list."""
        data = hass.data[DOMAIN]
        entity_id = call.data.get("entity_id")
        new_name = call.data.get("name")
        
        if entity_id and new_name:
            entity_registry = async_get(hass)
            entity = entity_registry.entities.get(entity_id)
            if entity:
                # Use the unique_id which is stable
                item_id = entity.unique_id
                item = next((itm for itm in data.items if itm["id"] == item_id), None)
                if item:
                    if "names" not in item:
                        # Migrate from old format
                        item["names"] = [item.get("name", "")]
                        item["current_name_index"] = 0
                    
                    item["names"].append(new_name)
                    await data.update_entities()
                    await data.async_touch_history(item)
                    
                    # Force entity update
                    await hass.services.async_call(
                        "homeassistant",
                        "update_entity",
                        {"entity_id": entity_id},
                    )
                    
                    # Fire event for UI update
                    hass.bus.async_fire(
                        "activity_manager_updated",
                        {"action": "name_added", "item": item},
                    )

    async def remove_name_service(call: ServiceCall) -> None:
        """Remove a name from an activity's name list."""
        data = hass.data[DOMAIN]
        entity_id = call.data.get("entity_id")
        index = call.data.get("index")
        
        if entity_id is not None and index is not None:
            entity_registry = async_get(hass)
            entity = entity_registry.entities.get(entity_id)
            if entity:
                item_id = entity.unique_id
                item = next((itm for itm in data.items if itm["id"] == item_id), None)
                if item and "names" in item and 0 <= index < len(item["names"]):
                    # Don't remove the last name
                    if len(item["names"]) <= 1:
                        return
                    
                    # Update current_name_index if necessary
                    if index <= item.get("current_name_index", 0) and item.get("current_name_index", 0) > 0:
                        item["current_name_index"] -= 1
                    elif index == item.get("current_name_index", 0) and index == len(item["names"]) - 1:
                        # If removing the current name and it's the last one, go to previous
                        item["current_name_index"] = len(item["names"]) - 2
                    
                    # Remove the name
                    item["names"].pop(index)
                    await data.update_entities()
                    await data.async_touch_history(item)
                    
                    # Force entity update
                    await hass.services.async_call(
                        "homeassistant",
                        "update_entity",
                        {"entity_id": entity_id},
                    )
                    
                    # Fire event for UI update
                    hass.bus.async_fire(
                        "activity_manager_updated",
                        {"action": "name_removed", "item": item},
                    )

    hass.services.async_register(DOMAIN, "add_activity", add_item_service)
    hass.services.async_register(DOMAIN, "remove_activity", remove_item_service)
    hass.services.async_register(DOMAIN, "update_activity", update_item_service)
    hass.services.async_register(DOMAIN, "edit_activity", edit_item_service)
    hass.services.async_register(DOMAIN, "add_name", add_name_to_activity_service)
    hass.services.async_register(DOMAIN, "remove_name", remove_name_service)

    @callback
    @websocket_api.websocket_command(
        {vol.Required("type"): "activity_manager/items", vol.Optional("category"): str}
    )
    def websocket_handle_items(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Handle getting activity_manager items."""
        # Each item also carries turn, assigned_to, escalated, ... (computed now)
        connection.send_message(
            websocket_api.result_message(
                msg["id"], hass.data[DOMAIN].items_with_status()
            )
        )

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "activity_manager/add",
            vol.Optional("name"): vol.Any(str, [str]),
            vol.Optional("names"): [str],
            vol.Required("category"): str,
            vol.Required("frequency"): dict,
            vol.Optional("last_completed"): vol.Any(str, int),
            vol.Optional("icon"): str,
            vol.Optional("assignees"): [str],
            vol.Optional("rotation"): vol.In(logic.ROTATIONS),
            vol.Optional("turn_order"): [str],
            vol.Optional("turn_index"): int,
            vol.Optional("next_person"): str,
            vol.Optional("escalate_after"): vol.Any(dict, None),
        }
    )
    @websocket_api.async_response
    async def websocket_handle_add(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Handle adding an activity."""
        id = msg.pop("id")
        name = msg.pop("name", None)
        names = msg.pop("names", None)
        category = msg.pop("category")
        frequency = msg.pop("frequency")
        icon = msg.get("icon")
        last_completed = msg.get("last_completed")
        msg.pop("type")

        names_to_use = names or (name if isinstance(name, list) else [name] if name else None)
        if not names_to_use:
            connection.send_error(id, "invalid_format", "A task needs a name")
            return

        try:
            if isinstance(last_completed, int):
                # Milliseconds since the epoch, as JavaScript dates give them
                last_completed = dt.as_local(
                    dt.utc_from_timestamp(last_completed / 1000)
                ).isoformat()
            elif last_completed:
                last_completed = _local_iso(last_completed)
            else:
                last_completed = dt.now().isoformat()

            item = await hass.data[DOMAIN].async_add_activity(
                names_to_use,
                category,
                frequency,
                icon=icon,
                last_completed=last_completed,
                context=connection.context(msg),
                sharing=_edit_changes(msg, ADD_SHARING_KEYS),
            )
        except ValueError as err:
            connection.send_error(id, "invalid_format", str(err))
            return
        connection.send_message(
            websocket_api.result_message(id, hass.data[DOMAIN].item_with_status(item))
        )

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "activity_manager/update",
            vol.Required("item_id"): str,
            vol.Optional("last_completed"): str,
            vol.Optional("name"): str,
            vol.Optional("category"): str,
            # Person entity id of whoever did it (None = unknown)
            vol.Optional("completed_by"): vol.Any(str, None),
        }
    )
    @websocket_api.async_response
    async def websocket_handle_update(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Handle updating activity."""
        msg_id = msg.pop("id")
        item_id = msg.pop("item_id")
        msg.pop("type")
        last_completed = msg.get("last_completed")
        data = msg

        if last_completed:
            try:
                last_completed = _local_iso(last_completed)
            except ValueError as err:
                connection.send_error(msg_id, "invalid_format", str(err))
                return
        else:
            last_completed = dt.now().isoformat()

        item = await hass.data[DOMAIN].async_update_activity(
            item_id,
            last_completed=last_completed,
            context=connection.context(msg),
            completed_by=msg.get("completed_by") or None,
        )
        if item is None:
            connection.send_error(msg_id, "not_found", f"No activity {item_id}")
            return
        connection.send_message(
            websocket_api.result_message(msg_id, hass.data[DOMAIN].item_with_status(item))
        )

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "activity_manager/edit",
            vol.Required("item_id"): str,
            vol.Optional("names"): [str],
            vol.Optional("category"): str,
            vol.Optional("frequency"): dict,
            vol.Optional("icon"): str,
            # A correction: no name cycling, no history entry, no turn change
            vol.Optional("last_completed"): str,
            vol.Optional("assignees"): [str],
            vol.Optional("rotation"): vol.In(logic.ROTATIONS),
            vol.Optional("turn_order"): [str],
            vol.Optional("turn_index"): int,
            vol.Optional("next_person"): str,
            # null = never give it to everyone
            vol.Optional("escalate_after"): vol.Any(dict, None),
        }
    )
    @websocket_api.async_response
    async def websocket_handle_edit(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Handle editing an activity."""
        msg_id = msg["id"]
        item_id = msg["item_id"]
        try:
            item = await hass.data[DOMAIN].async_edit_activity(
                item_id, _edit_changes(msg), context=connection.context(msg)
            )
        except ValueError as err:
            connection.send_error(msg_id, "invalid_format", str(err))
            return
        if item is None:
            connection.send_error(msg_id, "not_found", f"No activity {item_id}")
            return
        connection.send_result(msg_id, hass.data[DOMAIN].item_with_status(item))

    @callback
    @websocket_api.websocket_command(
        {
            vol.Required("type"): "activity_manager/history",
            vol.Optional("item_id"): str,
            vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        }
    )
    def websocket_handle_history(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Completions newest first, for one task or all of them."""
        connection.send_result(
            msg["id"],
            logic.query_history(
                hass.data[DOMAIN].history, msg.get("item_id"), msg.get("limit")
            ),
        )

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "activity_manager/remove",
            vol.Required("item_id"): str,
        }
    )
    @websocket_api.async_response
    async def websocket_handle_remove(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Handle removing activity."""
        msg_id = msg.pop("id")
        item_id = msg.pop("item_id")
        msg.pop("type")
        data = msg

        item = await hass.data[DOMAIN].async_remove_activity(
            item_id, connection.context(msg)
        )
        connection.send_message(websocket_api.result_message(msg_id, item))

    # Home Assistant only lets admins subscribe to custom events directly,
    # so forward activity_manager_updated through our own command instead.
    @callback
    @websocket_api.websocket_command(
        {vol.Required("type"): "activity_manager/subscribe"}
    )
    def websocket_handle_subscribe(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Push activity changes to any logged-in user."""

        @callback
        def forward_update(event) -> None:
            connection.send_message(
                websocket_api.event_message(msg["id"], event.data)
            )

        connection.subscriptions[msg["id"]] = hass.bus.async_listen(
            "activity_manager_updated", forward_update
        )
        connection.send_result(msg["id"])

    websocket_api.async_register_command(hass, websocket_handle_items)
    websocket_api.async_register_command(hass, websocket_handle_add)
    websocket_api.async_register_command(hass, websocket_handle_update)
    websocket_api.async_register_command(hass, websocket_handle_edit)
    websocket_api.async_register_command(hass, websocket_handle_history)
    websocket_api.async_register_command(hass, websocket_handle_remove)
    websocket_api.async_register_command(hass, websocket_handle_subscribe)

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when it changed."""
    await hass.config_entries.async_reload(entry.entry_id)
