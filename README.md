# activity-manager

Manager recurring tasks from within Home Assistant

Use the companion [Activity Manager Card](https://github.com/pathofleastresistor/activity-manager-card) for the best experience.

The core idea is that an activity happens on a recurring basis, which is stored in the `frequency` field when adding an activity. By default, the activity is last completed when you first add the activity and then the timer can be reset.

<p align="center">
  <img width="600" src="images/activitymanager.gif">
</p>

## Installation

### Manually

Clone or download this repository and copy the "activity_manager" directory to your "custom_components" directory in your config directory

`<config directory>/custom_components/activity-manager/...`

### HACS

1. Open the HACS section of Home Assistant.
2. Click the "..." button in the top right corner and select "Custom Repositories."
3. In the window that opens paste this Github URL.
4. Select "Integration"
5. In the window that opens when you select it click om "Install This Repository in HACS"

## Usage

Once installed, you can use the link below to add the integration from the UI.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=activity_manager)

If you're using the [Activity Manager Card](https://github.com/pathofleastresistor/activity-manager-card), then you all you need to do is add the Activity Manager Card to your dashboard. When you're creating the card, you'll have to supply a `category` attribute to the card.

### Notifications

Because entities are exposed for each activity, you can build custom notifications. The example below runs an automation at sunrise to remind the user if they are past due on workout activities:

```
service: notify.mobile_android_phone
data:
  title: >-
    Workout reminder{% if (states.sensor | selectattr('attributes.integration', 'eq', 'activity_manager') |
    selectattr('attributes.category', 'equalto', 'Workout') |
    map(attribute='state') | map('as_datetime') | reject(">", now()) | list |
    count > 1)%}s{% endif %}
  message: >-
    {{ "Remember to stay healthy and go do: " }}
    {%- set new_line = joiner("<br />") %}
    <br />
    {%- for activity in states.sensor | selectattr('attributes.integration', 'eq', 'activity_manager') -%}
    {%- if activity.state|as_datetime < now() and activity.attributes.category=="Workout"  -%}
    {{ new_line() }}{{ " - "}}{{  activity.name }}
    {%- endif -%}
    {%- endfor %}
  data:
    priority: high
    ttl: 0
    importance: high
    notification_icon: "mdi:dumbbell"
```

### More information

-   Activities are stored in .activities_list.json in your `<config>` folder
-   An entity is created for each activity (e.g. `sensor.<category>_<activity>`). The state of the activity is the datetime of when the activity is due. You can use this entity to build notifications or your own custom cards.
-   Three services are exposed: `activity_manager.add_activity`, `activity_manager.update_activity`, `activity_manager.remove_activity`. The update activity can be used to reset the timer.
-   `activity_manager.update_activity` takes an optional `completed_by` (a `person.*` entity). Without it the completion counts as done by whoever's turn it was.
-   `activity_manager.edit_activity` edits or corrects an activity (names, category, frequency, icon, last completed, and the sharing fields below). It is never a completion: names don't cycle and nothing is added to the history.

### Shared tasks

An activity can be shared by several people (`assignees`, person entity ids). `rotation` is `alternate` (take turns following `turn_order`, which may repeat people), `fixed` (always the first in `turn_order`) or `anyone`. When the person whose turn it is does it, the turn moves on; when someone else covers, the turn stays, so that person gets the next one. If a shared task is still not done `escalate_after` (default 24 hours) after it was due, it goes to everyone until done. Each sensor has the attributes `assignees`, `rotation`, `turn_order`, `turn_index`, `turn`, `assigned_to`, `escalated`, `escalate_after` and `last_completed_by`, and an `activity_manager_updated` event with `action: escalated` fires when a task escalates.

### History

Every completion (card, services, Node-RED) is kept in `.activities_history.json` in your `<config>` folder, outside the recorder database, with who did it, whose turn it was and whether it had escalated. Removed tasks keep their history. On first start the file is seeded with each activity's last completion. The card shows it in the edit dialog; the websocket command `activity_manager/history` (`item_id` and `limit` optional) returns it newest first.
