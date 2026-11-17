# DSC Alarm Plugin

This plugin interfaces Indigo to a DSC PowerSeries alarm system using one of the interfaces below. It lets Indigo see the status of the entire alarm system. Triggers can be created for any alarm or zone change, actions can be created to Arm, Disarm, or trip the alarm. 

The plugin also maintains timers to keep track of how long any doors and windows have been opened, as well as how long it's been since it's seen activity in certain areas. These timers can be used to turn off the HVAC system if the wife is airing out the house, or turn off lights in areas of the house that aren't occupied.

## Information and Support

**Current version: 2026.05.11.54** (Indigo 2026.1, Python 3). The full change history, release by release, is in [`ROADMAP.md`](DSC%20Alarm.indigoPlugin/ROADMAP.md) inside the plugin bundle.

### Highlights since 2022.0.6

- **Dual interfaces:** IT-100 (serial) and Envisalink (network) can be used together, with automatic failover and reconnection.
- **Keypad label programming** over the IT-100: read, write and broadcast zone labels from Indigo.
- **Accurate zone states:** a zone's state always reflects the live sensor (open/closed), so HomeKit, scenes and triggers never see a "latched" reading.
- **Per-zone alarm memory**, matching the panel's own: `alarmState` is `normal`, `tripped` (in alarm now) or `memory` (was in alarm, since restored), and clears when the partition is next armed or with the **Clear Alarm Memory** action. Each keypad lists its zones in alarm memory.
- **"Unavailable" when states can't be confirmed:** at startup until the panel has reported, while the plugin is shut down, and after a configurable time without a connection. On reconnect the plugin resyncs every zone with the panel. Events: **Panel Connection Lost / Restored**.
- **Consistent zone icons** by zone type and state (flame for fire zones, motion icons, locks, red for tripped).
- **Logging controls:** a global "log all zone changes" override and buttons to set every zone's Log Changes on or off.
- Keypress actions accept installer sequences (`*8`), so the panel can be programmed from Indigo.

### Zone device states (for control pages and triggers)

| State | Values | Use it for |
|---|---|---|
| `onOffState` | live open/closed | HomeKit, triggers, automation |
| `state` | `closed` / `open` / `openLatched` | live state (motion latch shown as `openLatched`) |
| `alarmState` | `normal` / `tripped` / `memory` | which zones set off the alarm |
| `alarmMemorySince` | timestamp | when the zone tripped |
| `displayState` | `closed` / `open` / `tripped` / `memory` / `unavailable` | one combined value for control pages |

> **Upgrading from an older version:** zones no longer use `tripped` as their main `state`. Triggers or control pages that checked for it should use `alarmState` or `displayState`. The old "Latch zones open while alarm is triggered" option has been replaced by these states.

### Earlier history

Version 2022.0.1 with changes from Monstergerm: compatible with Python 2 (Server API 2.0) or Python 3 (Server API 3.0, default), with minor tweaks, a six-level logging system, a Time Sync action, more zone attributes and custom state icons. Version 2.0, also with changes from Monstergerm: see the DSC plugin v2 Release Notes.

For Installation and Information details please see this [forum post](http://forums.indigodomo.com/viewtopic.php?f=56&t=10287).

If you need help, please post a topic in this [user forum](http://forums.indigodomo.com/viewforum.php?f=56).

## Downloading for use

If you are a user and just want to download and install the plugin, click on the "Clone or download" button and then "Download Zip" and it will download the plugin and readme file to a folder named "DSC-Alarm-master" in your Downloads directory. Once it's downloaded just open that folder and double-click on the "DSC Alarm.indigoPlugin" file to have the client install and enable it for you.

## Contributing

If you want to contribute, just clone the repository in your account, make your changes, and issue a pull request. Make sure that you describe the change you're making thoroughly - this will help the repository managers accept your request more quickly.

## Terms

Perceptive Automation is hosting this repository and will do minimal management. Unless a pull request has no description or upon cursory observation has some obvious issue, pull requests will be accepted without any testing by us. We may choose to delegate commit privledges to other users at some point in the future.

Perceptive Automation doesn't guarantee anything about this plugin - that this plugin works or does what the description above states, so use at your own risk. 

## Plugin ID

Here's the plugin ID in case you need to programmatically restart the plugin:

**Plugin ID**: com.frightideas.indigoplugin.dscAlarm
