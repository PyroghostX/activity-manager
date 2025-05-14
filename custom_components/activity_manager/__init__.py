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
import homeassistant.helpers.config_validation as cv
from homeassistant.util.json import JsonArrayType, load_json_array
from homeassistant import config_entries
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify
from homeassistant.util import dt
from .const import DOMAIN
from .utils import dt_as_local

_LOGGER = logging.getLogger(__name__)

PERSISTENCE = ".activities_list.json"


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

        # If names is provided, use it; otherwise use name as a single-item list
        names_to_use = names if names else ([name] if name else [])

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
                )

    async def add_name_to_activity_service(call: ServiceCall) -> None:
        """Add a name to an activity's name list."""
        data = hass.data[DOMAIN]
        entity_id = call.data.get("entity_id")
        new_name = call.data.get("name")
        
        if entity_id and new_name:
            entity_registry = async_get(hass)
            entity = entity_registry.entities.get(entity_id)
            if entity:
                item = next((itm for itm in data.items if itm["id"] == entity.unique_id), None)
                if item:
                    if "names" not in item:
                        # Migrate from old format
                        item["names"] = [item.get("name", "")]
                        item["current_name_index"] = 0
                    
                    item["names"].append(new_name)
                    await data.update_entities()
                    
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
                item = next((itm for itm in data.items if itm["id"] == entity.unique_id), None)
                if item and "names" in item and 0 <= index < len(item["names"]):
                    # Don't remove the last name
                    if len(item["names"]) <= 1:
                        return
                    
                    # Update current_name_index if necessary
                    if index <= item.get("current_name_index", 0) and item.get("current_name_index", 0) > 0:
                        item["current_name_index"] -= 1
                    
                    # Remove the name
                    item["names"].pop(index)
                    await data.update_entities()
                    
                    # Fire event for UI update
                    hass.bus.async_fire(
                        "activity_manager_updated",
                        {"action": "name_removed", "item": item},
                    )

    hass.services.async_register(DOMAIN, "add_activity", add_item_service)
    hass.services.async_register(DOMAIN, "remove_activity", remove_item_service)
    hass.services.async_register(DOMAIN, "update_activity", update_item_service)
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
        connection.send_message(
            websocket_api.result_message(msg["id"], hass.data[DOMAIN].items)
        )

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "activity_manager/add",
            vol.Required("name"): str,
            vol.Required("category"): str,
            vol.Required("frequency"): dict,
            vol.Optional("last_completed"): int,
            vol.Optional("icon"): str,
        }
    )
    @websocket_api.async_response
    async def websocket_handle_add(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Handle updating activity."""
        id = msg.pop("id")
        name = msg.pop("name")
        category = msg.pop("category")
        frequency = msg.pop("frequency")
        icon = msg.get("icon")
        last_completed = msg.get("last_completed")
        msg.pop("type")

        if last_completed:
            last_completed = dt_as_local(last_completed)
        else:
            last_completed = dt.now().isoformat()

        item = await hass.data[DOMAIN].async_add_activity(
            name,
            category,
            frequency,
            last_completed=last_completed,
            context=connection.context(msg),
        )
        connection.send_message(websocket_api.result_message(id, item))

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "activity_manager/update",
            vol.Required("item_id"): str,
            vol.Optional("last_completed"): str,
            vol.Optional("name"): str,
            vol.Optional("category"): str,
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
            last_completed = dt.as_local(dt.parse_datetime(last_completed)).isoformat()
        else:
            last_completed = dt.now().isoformat()

        item = await hass.data[DOMAIN].async_update_activity(
            item_id, last_completed=last_completed, context=connection.context(msg)
        )
        connection.send_message(websocket_api.result_message(msg_id, item))

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

    websocket_api.async_register_command(hass, websocket_handle_items)
    websocket_api.async_register_command(hass, websocket_handle_add)
    websocket_api.async_register_command(hass, websocket_handle_update)
    websocket_api.async_register_command(hass, websocket_handle_remove)

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when it changed."""
    await hass.config_entries.async_reload(entry.entry_id)
