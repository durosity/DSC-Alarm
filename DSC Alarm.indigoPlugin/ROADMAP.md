# DSC Alarm Plugin - Development Roadmap

## v2026.05.11.54 (CURRENT)
**Status:** Zones keep their live state at all times; alarm involvement moves to its own states; devices show "unavailable" whenever states can't be confirmed

### Background

Two older mechanisms bent a zone's main state to show alarm involvement: the panel's zone-alarm message (cmd 601) set the zone's `state` to `tripped`, and the optional v47 latching deferred zone closes during an alarm. Both made the zone's state disagree with the real sensor, which misleads HomeKit, scenes and triggers. The plugin also already kept a plugin-wide alarm memory (`trippedZoneList`, the `DSC_Alarm_Memory` variable, cleared on arming). This release replaces all three with per-zone states that mirror the panel's own alarm memory.

### New zone states

| State | Values | Purpose |
|---|---|---|
| `onOffState` (value) | live open/closed | Never altered by alarm handling; what HomeKit, triggers and scripts should use |
| `alarmState` | `normal` / `tripped` / `memory` | `tripped` on cmd 601 (zone in alarm), `memory` on cmd 602 (zone alarm restored), `normal` when the zone's partition is next armed (cmd 652) |
| `alarmMemorySince` | timestamp | When the zone tripped |
| `displayState` | `closed` / `open` / `tripped` / `memory` / `unavailable` | One combined value for control pages |

`onOffState`'s display text (the State column) shows the combined picture, e.g. `closed – alarm memory` or `open – ALARM`, while its value stays live. All display updates go through a single helper, `_refreshZoneDisplay()`.

New keypad state `alarmMemoryZones`: comma-separated names of that partition's zones that are tripped or in memory.

New action **Clear Alarm Memory (all zones)** (also clears `DSC_Alarm_Memory` and the tripped-zone list).

### Retired

- **Latch zones open while alarm is triggered** (v47) is removed: zone closes always apply immediately (`closeTheseZonesList` is no longer filled). The config dialog's "Zone Behaviour During Alarm" section now explains the new states.
- Zones' `state` is never set to `tripped` any more. Zones found at `tripped` on startup are converted to `open` + `alarmState = memory`; the startup resync then corrects open/closed.

### Unavailable on startup and shutdown

Builds on v53:
- **Shutdown** marks every plugin device "unavailable", so stale states aren't shown while the plugin isn't running.
- **Startup** marks every device "unavailable" immediately (not after the grace period) and clears it once the first status report has been processed — this also covers crashes and power cuts, where shutdown never ran. Logged as "states confirmed"; the Connection Restored event is not fired for startup.

### Resync fix

The Envisalink login sends the status request (cmd 001) before the connection is reported successful, so zone-open reports could arrive before v53 started collecting them, and the resync would then wrongly close those zones. Collection now starts at the beginning of the connection attempt (`_beginResyncCollection`); the 15-second timer starts on success (`_startResync`). The legacy single-interface path does the same around its own cmd 001.

### Zone groups

A group is `tripped` while any member's `alarmState` is not `normal` (previously: while a member's `state` was literally `tripped`). Groups refresh whenever a member's alarm state changes.

### Log buttons

Under **Log all zone state changes**, two buttons — **Set all to log changes** / **Set all to not log changes** — set the "Log Changes" checkbox on every zone device and log how many changed. They apply immediately (Cancel on the dialog doesn't undo them).

### Migration notes

- Triggers, control pages or dashboards that test a zone's `state` for `tripped` should use `alarmState` (or `displayState`) instead.
- Per-zone variables (`var`) now only receive live states.
- Expect devices to show "unavailable" for roughly 15–20 seconds after every plugin start.

---

## v2026.05.11.53
**Status:** Devices are marked "unavailable" when the panel can't be reached

### Behaviour

- If no interface (IT-100 or Envisalink) has been connected for the grace period (default 30 s, setting **Mark devices unavailable after (seconds)**, minimum 5), every zone, keypad and zone group is put into Indigo's error state "unavailable". Last known states are kept underneath, so no triggers fire for things that didn't happen. Shorter blips are ignored. With both interfaces enabled, one working interface keeps everything live.
- On reconnect the plugin already requests a full status report (cmd 001). It now waits 15 s for the report, sets any zone still shown open that the panel did not report open (cmd 609) to closed — it closed while we weren't listening — and clears the marking.
- New events: **Panel Connection Lost** and **Panel Connection Restored**. Each fires once per outage; "restored" only after a successful resync.

### Implementation

`_checkConnectionHealth()` runs every main-loop pass. The once-a-minute `LastChanged` timer updates now pass `clearErrorState=False`; without that they would have silently cleared the marking within a minute (Indigo clears a device's error state on any state update by default).

---

## v2026.05.11.52
**Status:** Tripped zones get their own icon

Zone icons now have three columns: clear, active and tripped (red `SensorTripped`). Fire zones stay an orange flame when tripped. Because icons are set from the live state on every change, the tripped icon clears when the state changes (it can't stick like the pre-v50 red icon).

---

## v2026.05.11.51
**Status:** Zone icons set explicitly from a fixed table

Indigo's automatic icon (`kStateImageSel.Auto`) shows plain dots for most sensor subtypes and has no fire icon, so zone icons are now set explicitly on every state change and at startup from `ZONE_ICONS` (zone type → clear / active / tripped), using only selectors Indigo 2026.1 provides:

| Zone type | Clear | Active |
|---|---|---|
| Fire | `HvacHeatMode` (grey flame) | `HvacHeating` (orange flame) |
| Motion | `MotionSensor` | `MotionSensorTripped` |
| Door | `DoorSensorClosed` | `DoorSensorOpened` |
| Window | `WindowSensorClosed` | `WindowSensorOpened` |
| Lock | `Locked` | `Unlocked` |
| Water, gas, CO, glass, shock | `SensorOff` | `SensorTripped` |
| Other | `SensorOff` | `SensorOn` |

Indigo currently draws the door and window selectors as plain grey/green dots (per its documentation). They're used deliberately so proper artwork appears automatically if Indigo adds it. `Custom` images aren't implemented by Indigo yet.

Resolves parking-lot item 2 from v48 ("Device icons not working as expected").

---

## v2026.05.11.50
**Status:** Stale zone icons fixed

The cause of the inconsistent icons: with "custom icons" on, cmd 601 forced a zone's icon to red and nothing ever reset it, and the alarm-restore routines forced motion/grey icons that also stuck. Indigo keeps a forced icon until it is explicitly changed, so icons reflected each device's history rather than its state. All five sites stopped forcing zone icons, and startup reset zones to automatic (superseded by the explicit table in v51). Keypad icons unchanged.

---

## v2026.05.11.49
**Status:** Keypress actions accept installer programming sequences

- **Send Single Keypress (IT-100):** the dialog now validates with the same parser the action uses at run time (`_parseKeypressString`), so bracketed tokens (`[STAR]`, `[F2]`…) and whitespace — both documented in the dialog — are accepted, and invalid input gets the parser's specific error. The old regex rejected them.
- **`*8` is allowed** in all three keypress actions (single keypress, and the two Envisalink multi-keypress actions) so the panel can be programmed from Indigo. Note that an action containing `*8` plus the installer code stores that code in plain text in Indigo.

---

## v2026.05.11.48
**Status:** New global override for per-zone Log Changes

### New config option

In **Plugin Config → Logging**:

- **Log all zone state changes (override per-device setting)** (checkbox, default off)

When ticked, every zone open/close is logged to the event log regardless of each zone device's individual "Log Changes" checkbox. Equivalent to ticking Log Changes on every zone device, without actually having to do so. Useful for diagnostic sessions where you need a complete picture of zone activity without manually editing 30+ devices.

When unticked (default), the per-device "Log Changes" checkbox is honoured as before.

### Implementation

New helper `_shouldLogZoneChanges(props)` wraps the per-device `_propIsTrue(props, 'zoneLogChanges')` check with a global-override branch. Three call sites updated:
- Main `updateZoneState` open/close logging
- Temp sensor temperature-change logging  
- Zone bypass / bypass-cancel logging

Scope is logging only — trigger events (eventZoneTripped etc) and other per-device gates are unaffected. If you want per-device trigger behaviour, you still get it; the override just opens the log spigot.

### Parking lot (not addressed this release)

Tracked for a future session:
1. Reset-after-alarm-trigger still has issues despite v45 fixes — needs investigation using the v45 diagnostic log lines next time it occurs
2. Device icons not working as expected — needs investigation with specifics on which device type and what's wrong
3. General codebase tidy-up — orphaned code, dead branches, leftover comments from earlier iterations

---

## v2026.05.11.47
**Status:** Zone-latching during alarm is now opt-in; default behaviour gives real-time zone state

### Background

The original plugin behaviour was: during a trip (while `repeatAlarmTripped` is True), zone-close events from the panel get deferred to `closeTheseZonesList` and only replayed when the panel disarms. The intent was to preserve a snapshot of every zone that opened during the break-in, useful for someone looking at a control page after the fact.

But this breaks downstream automation that expects accurate real-time sensor state — HomeKit views motion sensors as still active long after they've physically closed, scenes don't trigger correctly, etc. For most users the real-time behaviour is what they want; the snapshot is a niche use case.

### v47 changes

**New config option** under Plugin Config → "Zone Behaviour During Alarm":

- **Latch zones open while alarm is triggered** (checkbox, default OFF)

When unchecked (default): cmd 610 (zone closed) applies the zone-close immediately, even while `repeatAlarmTripped` is True. Downstream automation sees accurate state at all times.

When checked: original snapshot behaviour — closes deferred to `closeTheseZonesList`, replayed on the next disarm cmd.

### Migration

Default flips from on-to-off. Existing users who relied on the snapshot need to tick the new box to keep that behaviour. The plugin config dialog explains the trade-off in plain English.

### Toggle-while-running cleanup

If the user toggles the setting from ON to OFF while there are zones already deferred in `closeTheseZonesList`, the list is drained immediately on config save — those zones reflect their real current state right away instead of staying latched until the next disarm.

The reverse toggle (OFF to ON) needs no migration — the list just starts collecting on the next cmd 610.

### Log message updated

The "Zone N close DEFERRED" log line now explicitly notes that latching is enabled in config, so when the line appears it's clear it's intentional behaviour rather than the v45 stuck-flag bug.

---

## v2026.05.11.46
**Status:** Detects alarm trips that happened while the plugin was offline

### Background

If Indigo is offline (or the plugin is being restarted) when the alarm trips, the plugin misses the cmd 654 broadcast. The panel keeps re-broadcasting cmd 654 from alarm memory for several minutes after the bell cutoff, so when the plugin reconnects there's a window where it COULD pick up the trip — but the existing handler would treat the replay as a fresh live alarm, firing a false-positive "ALARM TRIPPED!" notification for an event that's already over.

Worse: if Indigo comes back AFTER the panel has stopped replaying (case 4 — alarm fully resolved while we were offline), there's no signal at all and the trip event is silently lost.

### v46 changes

#### Auto status-pull on interface connect

When the IT-100 or Envisalink interface comes up, the plugin now automatically sends cmd 001 to request a full state report from the panel. The response includes current zone states, partition states, and crucially LED states (including `LEDMemory` which indicates alarm-memory data is present).

Reset-on-disconnect logic means a transient interface drop will trigger a fresh status pull on reconnect — covers cases like network blips or a panel power-cycle.

#### Historical-alarm classification

When cmd 654 arrives, the handler now has a new branch BEFORE the live-trip handling:

```
1. Partition in alarmAcknowledgedPartitions → suppress (existing)
2. Within startup grace AND partition currently ready/disarmed → HISTORICAL ALARM (new)
3. Otherwise → fresh live trip (existing)
```

The discriminator for case 2 isn't just time-since-startup — it's also the partition's current ready/disarmed state. A real active trip leaves the partition in notready/tripped, so if we see ready/disarmed it means the alarm cycle is already over and this is a memory replay.

#### Historical-alarm handling

When a historical alarm is detected:
- **Always** logs loudly to the event log with a clear "HISTORICAL ALARM DETECTED" message
- **Always** adds the partition to `alarmAcknowledgedPartitions` so subsequent replays are silently suppressed
- **Always** sets `LEDMemory='on'` to reflect that alarm memory has a recorded trip
- **Conditionally** (config-controlled) fires the new `eventHistoricalAlarmDetected` trigger AND sends a clearly-distinct email to the urgent recipient

The notification uses subject `"POSSIBLE HISTORICAL ALARM detected on startup"` and a body explaining that the alarm is not currently active and pointing the user to the keypad's alarm memory for details.

Importantly, does NOT set `repeatAlarmTripped` or update the partition state to tripped — the alarm is already over.

#### New config options

In **Plugin Config → Startup / Missed Alarm Handling**:

- **Alert on detected historical alarm** (checkbox, default ON) — controls whether the trigger fires and the email goes out. Either way, the event is logged.
- **Startup grace period (seconds)** (textfield int, default 120, range 0-600) — the window during which alarm-memory-replay detection is active. Set to 0 to disable historical-alarm detection entirely.

#### New trigger

`eventHistoricalAlarmDetected` (Events.xml) — separate from the regular `eventAlarmTripped` so the user can wire up different actions for the two cases (e.g. immediate alert vs delayed/quieter notification).

### Things this doesn't solve

If a trip happens while offline AND the panel finishes replaying alarm memory before the plugin reconnects, no cmd 654 arrives at all. The plugin can still see `LEDMemory='on'` in the cmd 001 status response — but the current code doesn't trigger anything from a bare `LEDMemory` state without a corresponding cmd 654. This is a smaller hole than what v46 fixes and can be addressed in a future version if it shows up in practice.

---

## v2026.05.11.45
**Status:** Fixes the "stuck on tripped" bug where zone updates stop working for hours after a real alarm trip

### The bug

A real alarm trip (cmd 654, with bell sounding) leaves the plugin permanently stuck reporting `state='tripped'` and refusing to apply any zone-close updates. Restart fixes it. Reproduced reliably in the field.

Root cause: cmd 610 (zone closed) defers all zone-close events into `self.closeTheseZonesList` whenever `self.repeatAlarmTripped` is True, on the assumption that a disarm cmd (750/655/751) will follow and replay them. But if the panel's bell-cutoff timer expires WITHOUT the user disarming via the keypad, no user-disarm cmd is sent. The panel just goes quiet and returns to ready on its own. The flag stays True forever, the deferred-list grows with every zone close, and motion sensors etc. show as 'open' until the next plugin restart.

The state was wholly in-memory which is why restart fixed it.

### Fixes

**Primary fix — cmd 650 (Partition Ready) now clears the flag if stuck.** When the panel reports a partition back to Ready state and we still think we're tripped, the alarm cycle is over and the flag must be cleared (otherwise zone-close deferral runs forever). The fix also adds the partition to `alarmAcknowledgedPartitions` so any subsequent cmd 654 re-broadcasts from panel alarm memory don't revert the clear, and applies all pending deferred zone closes immediately so the zone states catch up.

This covers both reported scenarios: bell-cutoff timeout AND any disarm path the plugin doesn't recognise as a user-disarm cmd.

**Defensive fix — watchdog in runConcurrentThread.** `_watchdogRepeatAlarmTripped()` checks once per second: if the flag has been True for more than 30 minutes, force-clear it, log loudly, and replay any deferred zone closes. This catches any other scenario we haven't accounted for. 30 minutes is well past the longest legitimate alarm-memory-replay window (typically 4-15 minutes depending on panel programming).

**Diagnostic — centralised setter for `repeatAlarmTripped`.** All 6 direct assignments replaced with `self._setRepeatAlarmTripped(value, reason)`, which logs every flag transition with the cmd that triggered it. New tracking field `self.repeatAlarmTrippedSetAt` records when the flag went True so durations can be logged.

**Diagnostic — cmd 610 now logs every deferred close.** Each deferred zone-close logs the size of the deferred list and how long the flag has been held. Makes the stuck-state visible in the log instead of silently losing updates.

### What to look for in the log next time this happens

- `repeatAlarmTripped: False -> True (cmd 654 ...)` — trip detected
- A series of `Zone N close DEFERRED ...` lines while the alarm is active — expected
- `cmd 650 (partition N Ready): panel reports Ready while we still thought the alarm was tripped` — primary fix firing
- `repeatAlarmTripped: True -> False (cmd 650 ...)` — flag cleared cleanly
- If the cmd-650 path doesn't fire, the watchdog will fire after 30 minutes: `repeatAlarmTripped watchdog: flag has been True for ... force-clearing the flag`

### Things this doesn't fix

If the panel really does send a user-disarm cmd (750) that doesn't parse cleanly OR there's a different scenario causing the bug, the watchdog will still catch it after 30 minutes. The diagnostic logging will give us the trail to fix the real cause properly in v46.

---

## v2026.05.11.44
**Status:** Multi-round pre-flight backout, immediate cancel, and cancel-aware LCD waits

### Pre-flight backout — multi-round

The pre-flight `####` that runs before every per-zone read/write occasionally failed to clear the panel from a previous run's installer-menu state. The old code did ONE round of `####`, snapshotted the LCD, and if any partition still showed menu-state words ('section', 'enter lcd', 'program', 'enter data'), it aborted with the message *"Press # on a physical keypad until the display shows the date/time, then try again."* — which is impossible because **other keypads are locked out while the IT-100 holds installer mode**.

Replaced with `_preFlightBackoutToIdle(opLabel, maxRounds=3)`:
- Sends `####`, waits, snapshots — repeats up to 3 rounds if still in a menu state
- A second round is usually all that's needed; a busy keybus can drop keystrokes on the first attempt
- If 3 rounds still don't clear, the abort message now gives realistic recovery options instead of impossible ones: (1) wait ~30 min for the panel's own installer-mode timeout, (2) power-cycle the panel, (3) disable+re-enable the IT-100 device in Indigo

Both the read path and the write path now use this helper, replacing two near-identical hand-rolled backout blocks.

### Cancel — immediate panel recovery

The old cancel behaviour set a flag that was checked between zones, meaning the in-progress zone had to finish first (~60-90s for a write). The new cancel action:

1. **Flushes the IT-100 keystroke queue immediately** — drops any half-typed label characters that haven't gone on the wire yet
2. **Sends `####` twice from the cancel-action's own thread** — pushes the panel back to idle right now, doesn't wait for the worker
3. **Sets the cancel flag** — signals the worker to stop

The result: the panel is back at its home screen within ~1 second of pressing cancel, regardless of where the worker was in its state machine.

### `_waitForLCDPrompt` — cancel-aware

The plugin's LCD-wait helper is used at every stage transition (~20 call sites). It previously polled until success/failure/timeout, blocking the worker for the full timeout duration (up to 180s for a broadcast wait). It now also returns `'cancelled'` if `self.bulkOperationCancelRequested` becomes True during the wait, with at most one poll interval (~200ms) of delay.

Existing call sites treat `'cancelled'` like any other non-`'success'` return — they go down their error path, call `_emergencyBackoutIT100`, and return. The per-zone outcome will be recorded as a FAIL with a slightly misleading "panel didn't show X" detail, but the bulk run's cancellation summary at the end will say `CANCELLED` and the count is reported correctly.

Set `check_cancel=False` to disable cancel-interrupt for callers where it isn't safe (e.g. mid-pre-flight backout). All existing callers default to `check_cancel=True`.

### Per-char loop — cancel check

Added a cancel check at the top of the per-character verification loop inside `_runZoneLabelWrite`. A 14-character label can spend 30+ seconds in this loop, so checking cancel only at zone boundaries left a long window. Now cancel is honoured between each character.

### Between-zone cancel — immediate

Both bulk pass workers (`_runZoneReadPass`, `_runZoneWritePass`) now check the cancel flag immediately after each zone's outcome is determined, BEFORE the inter-zone delay. Previously cancel would still cause a 5-second wait before the pass terminated.

### Cancel responsiveness — summary

End-to-end: cancel → panel back at idle within ~1s; worker thread fully exited within (mid-zone time + inter-zone delay) → typically 1-3 seconds for a read, 1-5 seconds for a write depending on what character it was on.

---

## v2026.05.11.43
**Status:** New Read Specific Zone Labels action for picking up individual zones without re-reading the panel

### New action: Read Specific Zone Labels from Panel

Symmetrical with the existing `Write Specific Zone Labels` — takes a comma-separated zone-list spec (`'1,3,5-10,15'`) and reads only those zones. Useful when you only need to pick up a handful of zones rather than blanket-reading the whole panel.

Same retry/delay config fields as the other bulk actions (retry passes, inter-pass delay, inter-zone delay). The zone-list spec is parsed via the shared `_parseZoneRangeSpec` helper that the Write Specific action already uses.

### Refactor: `_kickOffBulkLabelRead` zoneFilter

Replaced the `emptyOnly` boolean with a `zoneFilter` parameter taking `'all'` / `'empty'` / `'specific'`, matching the shape of `_kickOffBulkLabelWrite` for symmetry. The 'specific' branch parses `zoneSpec` and intersects with configured zones, with proper error reporting for malformed input (same patterns as the write side).

The skip-summary line now consistently shows what got filtered out:

```
Read Specific Zone Labels: starting bulk read of 3 configured zone(s) (skipped: 2 not configured in Indigo). Estimated total runtime ~1 minute(s). ...
```

---

## v2026.05.11.42
**Status:** Broadcast Labels state machine rewrite + new diagnostic menu item

### Broadcast Labels — correct state machine

The broadcast state machine had the panel flow wrong. The real PK5500 sequence is:

1. `*8 + installer code` → LCD shows `Enter LCD` / `Section ---` (LCD programming menu, not generic section entry)
2. `998` → prompt CHANGES to `Broadcast Labels` / `Yes    No` (this is a confirmation prompt; the broadcast hasn't started yet)
3. `*` → confirms Yes; LCD shows `In Progress`; ~60s on a typical bus
4. When complete, LCD returns to `Enter LCD` / `Section ---`
5. `#` to exit installer mode

The old state machine sent `998` and immediately waited for completion — no confirmation step. Any keystrokes the plugin sent after the 998 would actually have been interpreted by the Yes/No prompt, which is most of why broadcasts didn't reliably do anything. The wait condition was also wrong (`'enter section'`) — that prompt never appears here.

v42 rewrite uses `_waitForLCDPrompt` (same helper the read/write paths use) instead of hand-rolled polling, with stage-by-stage expected/failure substrings, and includes the missing `*` confirmation between sending `998` and waiting for completion. Stage 4 timeout bumped to 180s (was 120s) since ~60s is typical and larger buses can take longer.

### New menu item: Print Stored Zone Labels to Log

Read-only diagnostic. Doesn't touch the panel. Dumps the `labelText` and `lastSyncedLabelText` pluginProps for every configured `alarmZone` device, including disabled ones, to the event log. Format matches the bulk per-zone results block:

```
---- Configured Zone Labels (37 zone(s)) ----
  zone 001 (Front Door Sensor)             : labelText='Front Door'      synced='Front Door'
  zone 020 (Utility Front Door Sensor)     : labelText='Utility Frnt'    synced=<empty>             <never synced>
  zone 035 (Mike's Study Door Sensor)      : labelText='Mike Door Edit'  synced='Mike Door'         (modified)
  zone 039 (Loft Hatch Sensor)             : labelText='Loft Hatch'      synced='Loft Hatch'        [disabled]
---- End Configured Zone Labels (37 total, 1 modified (would be written by 'Write Changed'), 1 never synced, 1 disabled) ----
```

Markers: `(modified)` for zones whose labelText differs from synced (would be picked up by `Write Changed Zone Labels`), `<never synced>` for zones with no `lastSyncedLabelText` yet (never read or written via the plugin), `[disabled]` for devices disabled in Indigo.

Enumerates `indigo.devices.iter("self.alarmZone")` directly rather than `self.zoneList` so disabled devices are included (`zoneList` is only populated via `deviceStartComm`, which is only called for enabled devices).

---

## v2026.05.11.41
**Status:** Configurable multi-pass retry for bulk read/write, plus fixes for success-count and missing-outcome bugs surfaced by v40 production run

### Background

The first real-world v40 bulk-write run (29 zones, 20-48) exposed three issues:

1. **Intermittent stage 1 timeouts.** 7 zones failed on the first attempt with `per-char mismatch on char 02` (case-toggle case-mode race) or `stage 1 timeout` (the panel briefly in 'System is Unavailable' recovery state after the previous zone's exit). Manually re-running just the failed zones — twice — eventually got every zone through with no other changes. This is exactly the kind of failure pattern a retry pass should hide automatically.

2. **Success count wrong.** Final log line said `27/27 zone(s) written successfully` while the per-zone summary block listed 7 failures inside. The bulk worker was incrementing `successes` whenever `_runZoneLabelWrite` returned without raising — but the per-zone state machine catches its own errors and returns normally via `_emergencyBackout`. So a recorded FAIL outcome could still bump the success tally.

3. **Summary entry count mismatch.** The todo list had 29 zones but the summary block listed only 26 — three zones (one normalised-empty skip, two unexpected-exception catches) never recorded an outcome at all, so they were silently absent from the per-zone results.

### v41 changes

#### Multi-pass retry for bulk operations

Both `_runBulkZoneLabelRead` and `_runBulkZoneLabelWrite` now do an initial pass then up to N retry passes over only the zones that failed. Each retry pass:

- Honours cancel before each zone AND during the inter-pass delay (sleeps in 1s chunks).
- Determines per-zone success by inspecting the outcomes list — specifically the LAST outcome recorded for that devId during the pass. If it's not a `Read SUCCESS` / `Write SUCCESS`, the zone goes to the next pass's todo list.
- Logs a `starting retry pass N — X zone(s) failed previous pass` line at the start of each retry pass.
- Waits the configured inter-pass delay between passes, so the panel can fully recover from installer mode before the next attempt.

The summary at the end shows the FINAL outcome per zone (collapsed by devId), with an `(after N attempts)` suffix when a zone needed retries:

```
---- Write Specific Zone Labels: per-zone results (3 zone(s)) ----
  zone 021 (Utility Back Door Sensor)     : Write SUCCESS - 'Utility Backdo' (after 2 attempts)
  zone 035 (Mike's Study Door Sensor)     : Write SUCCESS - 'Mike Door'
  zone 037 (Claire's Study Window Sensor) : Write SUCCESS - 'Claire Window' (after 3 attempts)
---- End Write Specific Zone Labels per-zone results ----
```

The `COMPLETE` summary line now also notes `(across N pass(es))` when more than one pass ran.

#### Three new configurable fields on all 5 bulk actions

(Read All, Read Empty, Write All, Write Specific, Write Changed)

| Field | Default | Range | Notes |
|---|---|---|---|
| `retryPasses` | 2 | 0-5 | Extra passes after the initial. 0 = no retry. 2 retry passes = 3 total attempts per zone. |
| `interPassDelaySeconds` | 15 | 0-120 | Recommended 15-30s. Lets the panel fully recover from installer mode before retry. |
| `interZoneDelaySeconds` | 5 | 0-30 | Recommended 3-10s. Was hardcoded to 2s (read) / 3s (write) before. Bumped to give the panel more settling time per zone. |

All three are textfield ints. Out-of-range values are clamped with a warning in the log; non-numeric values fall back to the default with a warning. Parsing centralised in `_parseRetryConfig(action)`.

#### Per-pass extraction

Each worker's per-zone loop is now in a separate helper:
- `_runZoneReadPass(todo, installerCode, displayLabel, passLabel, interZoneDelay)` → `(failedTodo, cancelled)`
- `_runZoneWritePass(todo, installerCode, displayLabel, passLabel, interZoneDelay)` → `(failedTodo, cancelled)`

These don't touch the `bulkLabelReadInProgress` flag or the outcomes lifecycle — the outer worker owns those. Each pass snapshots `len(self.bulkOperationOutcomes)` before each zone, so it can find what outcome the per-zone state machine recorded and decide whether to retry.

#### Bug fixes

- **Success counting**: counts are now derived from the FINAL outcome per devId at the end of the run, not from per-call function returns. A zone is a success iff its last outcome string starts with `Read SUCCESS` / `Write SUCCESS`. This fixes the `27/27 successfully` lie when 7 failed.
- **Missing outcomes**: both the `normalised-empty` branch and the `unexpected-exception` catch in the write worker now call `_recordZoneOutcome` with a FAIL outcome. The read worker's exception catch does the same. There's also a defensive `_recordZoneOutcome` at the end of each per-zone pass step that fires only if NOTHING got recorded — so the outcomes list always has at least one entry per attempted zone.

#### `_logZoneOutcomes` collapse-by-devId

The summary formatter now deduplicates the outcomes list by devId, keeping the LAST entry per device and tracking the total attempt count. Single-zone and multi-zone formats both gain the `(after N attempts)` suffix when applicable. This is essential for multi-pass runs where each retried zone would otherwise show multiple rows.

### Known issue parked: case-toggle retry case-mode

The per-character verification retry path still re-sends the case-toggle key sequence (`*>*`), which toggles the panel's stateful case mode. The first attempt at a given character may be in the right case; the retry then sends it in the wrong case. v36 documented this; v41 doesn't fix it at the per-char level, but the multi-pass retry should now mask its impact — failed zones get a fresh start from the section-entry stage on the next pass.

---

## v2026.05.11.40
**Status:** Replaced device-Notes audit trail with end-of-operation log summary

User reaction to v39's device-Notes-as-audit-trail: they already use the Notes field for something else. Reasonable — overloading a free-text field that the user owns is the kind of choice that bites later.

**v40 changes:**

- **Removed `_appendZoneNote`** (which wrote per-zone outcomes into `dev.description` via `dev.replaceOnServer()`). No more device Notes writes.
- **New `_recordZoneOutcome(devId, outcome, panelLabel, details)`** — same call sites, same arguments — but it appends to an in-memory list `self.bulkOperationOutcomes` instead of writing anywhere persistent.
- **New `_beginZoneOutcomes()`** — resets the list at the start of a single-zone OR bulk operation.
- **New `_logZoneOutcomes(displayLabel)`** — dumps the list to the log when the operation finishes, then clears it.

### Summary format

**Single-zone (one entry):** compact one-liner appended to the log.
```
Zone Label Write (Loft Hatch Sensor) summary: zone 048 (Loft Hatch Sensor): Write SUCCESS - 'Loft Hatch'
```

**Multi-zone (bulk operations):** header + aligned per-zone block + footer.
```
---- Read All Zone Labels: per-zone results (3 zone(s)) ----
  zone 001 (Front Door)        : Read SUCCESS - 'Front Door'
  zone 002 (Hallway Motion)    : Read SUCCESS - 'Hallway Motion'
  zone 048 (Loft Hatch Sensor) : Write FAIL - 'LOFTMO' - stage 4 - per-char mismatch on char 02 (expected 'o')
---- End Read All Zone Labels per-zone results ----
```

Zones sorted by zone number. Device-name column width auto-computed so the SUCCESS/FAIL part lines up.

### Lifecycle plumbing

- `_runZoneLabelRead` and `_runZoneLabelWrite` both gained a `manageOutcomes` keyword arg (defaults True). When True (per-zone button path), the method opens its own outcomes list at the top and dumps the summary in its finally clause. When False (bulk worker path), the bulk worker owns the lifecycle — one outcomes list spans every zone in the bulk run.
- `_runBulkZoneLabelRead` and `_runBulkZoneLabelWrite` now call `_beginZoneOutcomes()` at the top and `_logZoneOutcomes(displayLabel)` in their finally.
- Broadcast doesn't touch outcomes (it's not per-zone — it's a single panel-side operation).

The `_recordZoneOutcome` call sites from v39 are otherwise unchanged — same arguments, same content. The 18 call sites across read/write success/failure paths all still fire; they just now record to RAM instead of writing to device descriptions.

## v2026.05.11.39 (PREVIOUS)
**Status:** Cancel action + per-zone success/failure history in device Notes + latent NameError fix

User asks during v38 testing:
1. A way to stop a bulk operation mid-run (don't want to sit through 40 zones if you spot something going wrong on zone 3).
2. Per-zone audit trail of read/write outcomes so it's easy to see what happened to a specific zone without digging through the event log.

### Latent bug found and fixed

While instrumenting `_runZoneLabelWrite` to update the device Notes, discovered that v38's pluginProps-update block was referencing `devId` from outside its scope — the variable was never passed into `_runZoneLabelWrite`. Every successful write would have hit `NameError`. Fixed by adding `devId` to the function signature and updating both call sites (per-zone button + bulk worker). The user hadn't hit this because their last successful test was on v35, before v38 introduced the offending code.

### Cancel mechanism

- **New action 'Cancel Bulk Label Operation'** (id: `actionCancelBulkOperation`) sets a flag that bulk workers check between zones.
- **Two new instance flags:** `self.bulkOperationCancelRequested` (the user's request) and `self.bulkOperationKind` (a short label of what's running, so the cancel action can tell the user precisely what they're cancelling — and refuse to even try when 'Broadcast Labels' is in progress).
- **Cancel honoured between zones, not mid-zone.** Worker checks the flag at the top of each loop iteration. The currently-in-progress zone is allowed to finish first — that can take up to ~90 seconds for a write. Cleaner than aborting mid-state-machine.
- **Cancellation summary log** tells the user how many zones completed before stopping and how many were not attempted. Mentions a suggested next step (e.g. 'Read Only Empty Zone Labels' or 'Write Changed Zone Labels') to resume the work.
- **Broadcast cannot be cancelled.** Once section 998 is sent, the panel is broadcasting autonomously on the keybus and doesn't accept interruption — the action's cancel callback detects 'Broadcast Labels' is running and explains this clearly to the user.

### Per-zone Notes audit trail

- **New helper `_appendZoneNote(devId, outcome, panelLabel, details)`** prepends a timestamped line to the Indigo device's Notes field (newest at the top so you don't have to scroll).
- **Format:** `YYYY-MM-DD HH:MM <Outcome> - Label currently: '<text>' [- <details>]`
- **Examples:**
  - `2026-05-19 19:42 Write SUCCESS - Label currently: 'Loft Hatch'`
  - `2026-05-19 19:42 Write FAIL - Label currently: 'LOFTMO' - stage 4 - per-char mismatch on char 02 (expected 'o'); panel label unchanged (write aborted before save)`
  - `2026-05-19 19:42 Read SUCCESS - Label currently: 'Front Door'`
  - `2026-05-19 19:42 Read FAIL - stage 1 - wrong installer code`
- **Wired into every meaningful outcome path** in both read and write workers: read success, read stage 1/2/3 failures, read WARNING (no LCD content), read unexpected error, write success, write stage 1/2/3 failures, write per-char ABORT (with detail of which character and the panel's last-known-good label), write unexpected error.
- **Uses `dev.description` via `dev.replaceOnServer()`** (the Indigo API for the Notes field). Best-effort: any failure is caught and logged so a notes failure can never break the worker.

### Plumbing changes

- `_runZoneLabelWrite` signature now takes `devId` as its first arg.
- Both call sites updated to pass `dev.id`.
- `syncedLabel` calculation moved outside its try block so it's available for the notes call on success.

## v2026.05.11.38 (PREVIOUS)
**Status:** Three new bulk-write actions + `lastSyncedLabelText` tracking

Follow-up to v37's broadcast action. With the per-character verification proven reliable, bulk writes are now practical. v38 adds the three write counterparts to the existing read actions, plus a new tracking prop that enables "write only changed labels" without needing to re-read from the panel.

**New tracked prop:** `lastSyncedLabelText`. Stored per zone in pluginProps (no UI field — internal use only). Updated automatically:
- When a successful **read** completes → set to the captured label
- When a successful **write** completes → set to the just-written label (normalised, trailing-spaces stripped)

Comparing the current `labelText` to `lastSyncedLabelText` tells us whether the user has manually edited the label locally since the last sync.

**v38 additions:**

### Three new actions in the action group editor

- **Write All Zone Labels to Panel** (`actionWriteAllZoneLabels` / `methodWriteAllZoneLabels`). Iterates every configured zone, writes each. ~60-90s per zone (per-char verification is slow), so a 40-zone system is ~40-60 minutes. Long-running.
- **Write Specific Zone Labels to Panel** (`actionWriteSpecificZoneLabels` / `methodWriteSpecificZoneLabels`). Accepts a text field `zoneList` containing zone numbers/ranges: `5`, `1,3,5`, `1-10`, `1,3,5-10,15`. Whitespace tolerated. Out-of-range zones (>64) are clamped where possible; unparseable parts produce per-part error messages and the action proceeds with the valid subset.
- **Write Changed Zone Labels to Panel** (`actionWriteChangedZoneLabels` / `methodWriteChangedZoneLabels`). Writes only zones where `labelText != lastSyncedLabelText`. Zones with no `lastSyncedLabelText` (never read or written) are SKIPPED with a friendly log message telling the user to run 'Read All' or 'Write All' first to establish a baseline. Safe to run repeatedly.

### Shared implementation

- **`_kickOffBulkLabelWrite(zoneFilter, zoneSpec, displayLabel)`** is the shared kickoff. `zoneFilter` is `'all'`, `'specific'`, or `'changed'`. Same validation shape as the bulk reads (IT-100 enabled+connected, partitions disarmed, installer code, concurrency lock).
- **`_runBulkZoneLabelWrite(todo, installerCode, displayLabel)`** is the background worker. Calls the existing `_runZoneLabelWrite` per zone, synchronously, with 3s between zones to let the panel settle (avoids 'System is Unavailable' interfering with the next stage 1).
- **`_parseZoneRangeSpec(spec)`** parses zone-range strings — covered by 14 unit tests.
- **Per-zone failures don't abort the bulk run** — each zone is tried independently; a single failure just increments the failure counter and continues.

### Skip-summary in the start log

The kickoff log line now shows what was filtered out, e.g.:
```
Write Changed Zone Labels: starting bulk write of 5 zone(s) (skipped: 35 unchanged since last sync, 2 never synced (no lastSyncedLabelText)). Estimated total runtime ~6 minute(s)...
```

### Concurrency

Reuses `bulkLabelReadInProgress` for the lock (despite the name, it gates ALL long-running IT-100 installer-mode operations: bulk reads, bulk writes, and broadcast). Per-zone read button, per-zone write button, broadcast action, and the three new write actions all check it and refuse to start if it's set.

### Migration

Existing devices have no `lastSyncedLabelText` prop. The first successful read or write per zone establishes it. Until then, 'Write Changed' will skip those zones with a clear message. No backfill needed.

### Workflow recommendations

The "settling-in" flow for a system with stale panel labels:
1. **Read All Zone Labels** — populates `labelText` and `lastSyncedLabelText` from the panel
2. Edit labels manually in device configs where needed
3. **Write Changed Zone Labels** — pushes your edits to the panel; unchanged zones are skipped
4. **Broadcast Labels to All Keypads** — pushes the new labels to all other keypads on the bus (wait ~30s after step 3 for installer-mode recovery)

## v2026.05.11.37 (PREVIOUS)
**Status:** New 'Broadcast Labels to All Keypads' action — DSC section 998 trigger

After v35/v36 made the per-character label write work reliably (the `Loft Hatch` test wrote cleanly with proper case-toggle settling), the natural next step is to push the freshly-written labels from the programming keypad out to all the other PK5500 keypads on the bus. DSC provides section 998 for exactly this purpose — it broadcasts all stored labels to every other keypad on the keybus.

**v37 additions:**

- **New action 'Broadcast Labels to All Keypads (Section 998)'** (id: `actionBroadcastLabels`) — visible alongside the bulk-read actions in the action group editor. No device filter, system-wide operation.
- **State-machine worker** `_runBroadcastLabels` follows the same pattern as the per-zone read/write:
  - Stage 0: pre-flight `####` to back out of any latent installer mode
  - Stage 1: send `*8` + installer code, wait for `Enter Section` prompt (up to 4 retries on dropped keystrokes)
  - Stage 2: send `998` to trigger the broadcast
  - Stage 3: wait up to 120s for the broadcast to complete (LCD returns to `Enter Section` prompt). Logs LCD transitions for visibility.
  - Stage 4: send `#` to exit installer mode
  - Stage 5: wait up to 10s for LCD to confirm idle state
- **Same validation as bulk reads:** IT-100 enabled and connected, all partitions disarmed, installer code configured.
- **Same concurrency lock:** uses `bulkLabelReadInProgress` so a broadcast can't run concurrently with bulk reads (or vice versa), and per-zone read/write are blocked while broadcasting.
- **Honest UI hint in action config:** explicitly warns that if running this immediately after a label write, wait ~30 seconds for the panel to recover from its post-installer "System is Unavailable" transient state.

**Why action-only (no menu item):** lets you chain it into action groups after a bulk write or schedule it routinely. Less prominent in UI than the menu, which is good for an installer-mode operation that shouldn't be casual.

**Estimated runtime:** 30-90 seconds typical, capped at 120s. The broadcast itself is panel-driven once 998 is triggered — we just wait for it to finish.

## v2026.05.11.36 (PREVIOUS)
**Status:** Fix premature exit from `_waitForLabelEditScreen` — wait for menu to enter FIRST

**Diagnosis from the v35 write log:** v35's wait helper was exiting prematurely. The case-toggle keystrokes (`*>*`) are queued via `_sendKeystrokeToIT100` which adds them to the IT-100 TX queue. They take time to drain (~0.2s/key + ~100-300ms for panel to process). The v35 wait helper started polling 0.3s after queueing the keystrokes — sometimes catching the LCD BEFORE the first `*` had reached the panel. The wait would see "Loft O" (label edit content) and conclude "menu cleared, settled" — exiting immediately before the case toggle had even started.

Then we'd start sending digit presses while the case-toggle keystrokes were still draining. The queue ended up interleaving: `*`, `>`, `*`, `3`, `3` — but the panel saw them in some order that produced unexpected results, and on top of that ONE of the two `3`s seems to have gotten dropped on every attempt.

The v35 log makes this clear: the "case-toggle settled" line fires AFTER only 1-2 keystrokes have been TXed, not after the full `*>*` sequence and the panel transitions.

**v36 changes:**

- **`_waitForLabelEditScreen` is now a two-phase wait:**
  1. Phase 1: poll until the LCD ENTERS a menu screen (proves the case-toggle `*` has reached the panel)
  2. Phase 2: poll until the LCD LEAVES that menu screen (proves the toggle has been committed)
- **Three possible outcomes logged:**
  - "case-toggle settled" — both phases completed (the success path)
  - "case-toggle entered menu but did NOT settle within Xs" — Phase 1 succeeded but Phase 2 timed out (panel stuck in menu)
  - "case-toggle did NOT cause any menu screen to appear within Xs" — Phase 1 timed out (keystrokes likely dropped)

The third outcome message is also a diagnostic for the underlying drop issue — if it fires we know the case-toggle keystrokes are vanishing entirely.

**Known remaining issue:** the per-character retry mechanism re-sends `*>*` on retry, which re-toggles the panel's case mode (since case toggle is stateful). If a case-toggling character needs 2 attempts to succeed, the panel ends up in the WRONG case mode for subsequent characters that share the same case. We should clear retry-toggles by tracking the panel's true case mode based on attempt count, but that's a v37 problem — let's first see if v36's primary fix makes retries unnecessary in practice.

## v2026.05.11.35 (PREVIOUS)
**Status:** Superseded by 2026.05.11.36

**Diagnosis from the v34 write log:** the per-char verification consistently caught a real bug. When writing `Loft Hatch` over the existing label `LOFTMO`:
- Char 1 (position 0) `L` typed and verified correctly
- Char 2 (position 1) `o` failed identically 3 times — LCD always ended up showing `LmFTMO` (one letter short of `o`)

The detailed LCD trace during char 2 revealed the actual sequence:
1. We send `*` → panel shows `Select Option / Save`
2. We send `>` → panel shows `Select Option / Change Case`
3. We send `*` → panel shows `Lower Case Now / Selected` (confirmation screen)
4. We send `5` → DISMISSES the confirmation screen (no character typed!)
5. We send `5` → finally types `m` at position 1
6. We send `5` → ...but the cursor cycling never reaches `n`/`o`

**Root cause:** the `Lower Case Now / Selected` confirmation screen displays for ~0.5-1.5s after the case toggle. Any keystroke pressed while that screen is up just dismisses it and is otherwise consumed by the panel. Our throttled digit presses (0.2s/key) all arrived during this transient screen, with the first being eaten and only later ones cycling the character.

**v35 changes:**

- **`_waitForLabelEditScreen` helper added.** Polls LCDLine1 every 0.3s, up to 4s, waiting for it to NOT contain any of the menu/confirmation transient words: `select option`, `save changes`, `save?`, `change case`, `cycle case`, `lower case now`, `upper case now`, `selected`.
- **Per-char write loop now splits case-toggle from digit-press.** When a character needs a case toggle:
  1. Send the case-toggle keystrokes (`*>*`)
  2. Call `_waitForLabelEditScreen` — blocks until the menu confirmation screens have cleared
  3. THEN send the digit presses with the panel cleanly accepting character input
- **No change for characters that don't need a case toggle** — the wait is only invoked when `caseToggle` is non-empty, so the common case (typing in a single case mode) has no added latency.

**Excluded from the transient-screen list:** `system is`, `ready to arm`, `before arming`, and similar normal-mode status strings. These can appear as transient broadcast overlays at any time but are NOT menus and don't consume keystrokes. Including them would cause spurious waits.

If the wait times out (4s without the LCD settling), we proceed anyway and let the per-char verification catch any mismatch. Worst case: same behaviour as v34.

## v2026.05.11.34 (PREVIOUS)
**Status:** Superseded by 2026.05.11.35

User feedback after v33's bulk read worked well: needed a way to re-run the bulk read after manually editing some labels, without losing those edits. Added a second action that skips zones whose `labelText` pluginProp is already populated.

**v34 additions:**

- **New action 'Read Only Empty Zone Labels from Panel'** (id: `actionReadEmptyZoneLabels`) — visible alongside the existing 'Read All Zone Labels from Panel' in the action group editor.
- **Shared implementation:** both action callbacks (`methodReadAllZoneLabels` and `methodReadEmptyZoneLabels`) are now thin wrappers around `_kickOffBulkLabelRead(emptyOnly, displayLabel)`. The empty-only variant just filters the todo list to skip zones whose `labelText` is non-blank after strip.
- **Skipped count in start log** when running empty-only (e.g. `starting bulk read of 12 zone(s) (28 already have labels, skipped)`).
- **Friendlier no-op message** when running empty-only and all zones already have labels — explains that to force a re-read, the user should use the 'Read All Zone Labels' variant instead.
- **All log messages now use a per-mode `displayLabel`** (`Read All Zone Labels` vs `Read Empty Zone Labels`) so the event log makes it obvious which mode ran.

The `bulkLabelReadInProgress` lock and concurrency guards from v33 apply to both modes equally.

## v2026.05.11.33 (PREVIOUS)
**Status:** Superseded by 2026.05.11.34

**Diagnosis confirmed:** the v32 diagnostic logging proved `replacePluginPropsOnServer` IS correctly writing labelText to pluginProps when Read fires. The actual bug is dialog-side: clicking Save/Cancel on the device config dialog while it has an old in-memory valuesDict overwrites the freshly-written labelText with the stale value. Even Cancel exhibits this in some Indigo client versions — the in-memory state seems to persist somehow.

**Workaround approach:** offer a way to do the read with NO config dialog open. New action `actionReadAllZoneLabels` triggers a bulk read of every configured zone. Run it from the action group editor or as a one-off action, with all device config dialogs closed. Each zone's labelText is written cleanly to its pluginProps and stays written. Open each device's config dialog AFTER the bulk read completes to see the captured labels in the Label Text field.

**v33 additions:**

- **New action 'Read All Zone Labels from Panel'** (id: `actionReadAllZoneLabels`) — visible in the action group editor under the DSC Alarm plugin. No device filter — it's a system-wide bulk operation.
- **Same validation as the per-zone read:** IT-100 enabled and connected, all partitions disarmed, installer code configured.
- **Iterates `self.zoneList` in sorted zone-number order** — only configured zones get read. Zones with no Indigo device are skipped naturally.
- **Sequential reads through `_runZoneLabelRead`** — identical per-zone behaviour to the existing per-zone button, including all the staged installer-mode entry, snapshot, three-stage exit, and write-back to pluginProps. Each zone's read is independent; a failure on one doesn't stop the others.
- **2s pause between zones** so the panel can settle back to idle before the next read's stage 1 starts.
- **Progress logging** — `Bulk read [N/total]: starting zone NNN (devName)...` per zone, plus a final summary `COMPLETE. M/total zone(s) read successfully...`.

**Concurrency guards:**

- `self.bulkLabelReadInProgress` flag set while the bulk worker runs. The per-zone Read button and the Write button both refuse to start if the flag is set — two simultaneous IT-100 reads would collide on installer-mode entry.
- The bulk action refuses to start a second bulk read if one's already running.

**Runtime expectations:** ~15-20s per zone, so a 40-zone system is ~10-15 minutes total. The log will tell you what's happening.

**Known limitation:** if a user happens to have a zone config dialog OPEN during the bulk read, that specific zone's labelText may still get overwritten when the user later closes the dialog (same root bug). Final completion log explicitly reminds about this.

## v2026.05.11.32 (PREVIOUS)
**Status:** Superseded by 2026.05.11.33

**Reported:** even clicking Cancel after Read doesn't result in the captured label appearing in the Label Text field on reopen. The log says SUCCESS but the field stays empty.

This contradicts the previous theory (which was: dialog Save overwrites the prop with its OLD valuesDict). If Cancel doesn't work either, then either:
- `replacePluginPropsOnServer` is silently failing to persist labelText
- Or it IS persisting, but the dialog isn't reading from pluginProps the way I assumed

**v32 adds diagnostic logging** to the read path's prop-save block:
- Logs `pluginProps BEFORE write has N keys: [list]`
- Calls `replacePluginPropsOnServer` with the modified dict
- **Re-fetches the device** and logs `pluginProps AFTER write has N keys: [list]`
- Explicitly checks whether `labelText` is in the post-write props and logs its value
- Errors out loud if labelText is missing after the write

After installing v32 and running Read once, the log will tell us which of the two failure modes is happening. From there we can fix the right thing.

Also switched from `dev.pluginProps` (which returns a copy each call) to `dict(dev.pluginProps)` — being explicit that we're making a mutable local copy, to rule out a copy-vs-reference subtlety as a contributing factor.

## v2026.05.11.31 (PREVIOUS)
**Status:** Superseded by 2026.05.11.32

**Reported:** zone device showing `onState = false` and `offState = false` in the Custom States panel for a door that's been open for 78 days. Mike noticed in the UI that both were false simultaneously, which can't be right.

**Diagnosis:** Devices.xml declares two custom states `onState` and `offState` (Boolean, with TriggerLabels "Sensor Activated" / "Sensor Deactivated"), but no code path was ever writing to them. They sat at their default `False` forever, regardless of actual zone state. The comment in `updateZoneState` claimed "Indigo handles onState/offState automatically" — but that's only true for Indigo's built-in `state.on`/`state.off` derivations, NOT for custom states declared by a plugin with those names.

**v31 changes:**

- **onState/offState now mirror onOffState at all five sites that update zone state:**
  - deviceStartComm initial sync
  - updateZoneState: closed + latch (sensor stays "on" during latch)
  - updateZoneState: closed, no latch
  - updateZoneState: open/tripped
  - latch timer expiry
- **Updated the misleading comment** that claimed Indigo handles these automatically.

These states aren't used internally anywhere else in the plugin — they exist as triggerable states for users who built triggers/control pages against them. If you never used them, the only visible change is that the Custom States panel now shows accurate values.

## v2026.05.11.30 (PREVIOUS)
**Status:** Superseded by 2026.05.11.31

**Reported:** intermittent error stack trace ending in `type: 3` at line 4372, which is in the cmd 672 (Failed to Arm) handler. The handler did `indigo.devices[self.keypadList[partition]]` without first checking that `partition` exists in `keypadList`. If the panel ever broadcasts cmd 672 for a partition that doesn't have a configured keypad device (e.g. partition 3+ on a typical 1-2 partition system), `keypadList[partition]` raises `KeyError` which bubbles all the way up out of `runConcurrentThread`, taking down the whole concurrent loop. Indigo then auto-restarts it 10 seconds later. (The "type: 3" wording in the error is how Indigo formats the exception summary — the underlying error is `KeyError: 3`.)

**Why this is intermittent:** the panel only emits cmd 672 when arming fails (e.g. door open during arm). Most arms succeed silently, but occasionally one fails for an unconfigured partition.

**v30 changes:**

- **`_pollInterface` now wraps `_dispatchMessage` in try/except.** Any exception in a packet handler is logged with the cmd, data, exception type and message — but doesn't take down the concurrent thread. The drain loop continues with the next packet. This is the broader safety net for all handlers, not just 672.
- **cmd 672 handler explicitly checks `if partition not in self.keypadList`** before lookup. If the partition isn't configured, logs at debug level and returns without raising. The 'fail to arm' event/trigger still wouldn't have made sense for an unconfigured partition anyway.

There are several other cmd handlers (663, 664, 700, 701, 750, etc.) with the same structural risk — they directly access `self.keypadList[partition]` without checking. The v30 try/except safety net catches those too if they fire on an unconfigured partition. If specific ones become frequent offenders we'll add explicit defensive checks like we did for 672. 

## v2026.05.11.29 (PREVIOUS)
**Status:** Superseded by 2026.05.11.30

**Two issues addressed:**

### 1. Read result wasn't appearing in the dialog (UX bug)

User reported that after running Read Current Label, closing the dialog, and reopening it, the captured label wasn't showing in the Label Text field. Root cause: when the user clicks Read, the config dialog stays open with its OLD `valuesDict` in memory. The background read writes to `pluginProps` via `replacePluginPropsOnServer`, but when the user later closes the dialog by clicking OK/Save, Indigo writes the dialog's IN-MEMORY (old) valuesDict back to pluginProps, overwriting what the read just wrote.

This is a fundamental Indigo dialog-API limitation — there's no supported way to push values into an open modal dialog from a background thread.

**Fix:** updated the success log message, the device config dialog hint text, and the startup info message to make it explicit: **after Read completes, click CANCEL on the dialog (not OK/Save), then reopen it.** The captured label will then appear correctly.

### 2. Char 5 retries all failed identically — added diagnostic logging

In the v28 test, char 5 mismatched three times in a row with identical "got 'g'" results. Three consecutive identical failures across attempts strongly suggests the issue isn't random keystroke drops — the keystrokes either aren't reaching the panel or aren't being interpreted.

To dig further, v29 adds verbose diagnostic logging during the per-char write:
- **Pre-attempt snapshot**: log the LCD content right before typing each char (to see the starting state)
- **In-wait snapshots**: take 4 LCD snapshots during the 2.5s wait window and log the deduplicated sequence. If the LCD ever changes during the wait (e.g. briefly shows a menu), we'll see it. If all 4 are identical to the pre-attempt LCD, the keystrokes had no visible effect at all.

This is noisier in the log but will give us evidence to diagnose what's happening with the case-toggle keystrokes specifically.

## v2026.05.11.28 (PREVIOUS)
**Status:** Superseded by 2026.05.11.29

**Diagnosis from the v27 test:** the verification system worked exactly as designed. Char 5 mismatch was caught (expected `o`, LCD still showed `g` from the previous label). But it also produced a much clearer picture of what's actually going wrong: **the existing label `05 Lgunge iti` was already on the panel from the v26 failed write, and our 6 keystrokes for char 5 (`*>*555`) all silently vanished, leaving the panel unchanged.**

This is significant. It means keystroke drops aren't "one in N" — they're "a whole burst of N consecutive keystrokes goes missing at once." Likely cause: brief TX-path stall (OS serial buffer block, IT-100 momentarily not processing, or similar). The exact cause is still unknown but the pattern is clearer.

The good news: when the burst is dropped wholesale, the cursor stays put and the case mode is unchanged. So **re-sending the exact same keystrokes is the correct recovery action.**

**v28 changes:**

- **Per-character verification now retries up to 3 times** (1 initial + 2 retries) before aborting. Each retry re-sends the same `caseToggle + digitPresses` keystrokes and re-verifies the LCD.
- **Pre-retry LCD check:** if the LCD shows menu-style content (`save`, `cycle case`, `change case`) before retrying, we send `#` first to back out of any partial menu state we might be stuck in. Belt-and-braces — unlikely but possible if only part of the case-toggle burst landed.
- **Cursor is NOT advanced on a failed attempt.** Since the cursor stays at position `i` when keystrokes drop wholesale, retry sends naturally land on the right position.
- **Logging shows attempt number on retries**, so you can see how often retries are firing.

If retries are commonly needed, the log will reveal the actual drop rate. If max retries are commonly hit (operation aborts), we'd know that retry-same-char isn't enough and we'd need a different recovery approach.

**What this doesn't fix:** the underlying cause of bursts going missing. Same investigative path as before — threaddebug TX logging during a failure would show whether all 6 packets actually got written to the wire or whether some never left the plugin.

## v2026.05.11.27 (PREVIOUS)
**Status:** Superseded by 2026.05.11.28

**Diagnosis from the v26 write test:** the label `05 Lounge Moti` came out as `05 Lgunge iti` on the panel — multiple character drops including a missing `M` that shifted the whole tail by one position. The write itself "succeeded" (all stages completed, save fired, panel went back to idle), so the plugin had no way of knowing the label was wrong until a follow-up read confirmed it.

The intermittent keystroke drops we've been chasing all the way through v22-v26 affect the label-typing segment too, just with worse visibility since there were no LCD-state milestones to fail on mid-typing.

**v27 changes:**

- **Stage 4 (typing the label) now operates one character at a time** with explicit LCD verification before advancing the cursor.
- For each of the 14 characters: emit the case-toggle keystrokes (if needed), emit the digit presses, wait ~2.5s for the LCD to settle, then read LCDLine1 and check position `i` against the expected character.
- On match: send `>` to advance, continue to the next character.
- On mismatch: **abort the whole write**. Log clearly which character was wrong and what the LCD actually showed. Send `####` to back out of installer mode without saving, so the partial label is discarded.

**Design choices:**

- **Abort on mismatch rather than auto-retry mid-write.** Recovering character-by-character (cycle the digit, back up with `<`, etc.) is fiddly and could leave the panel in a worse state. Cleaner to abort, let the user investigate / retry the whole write by clicking the button again.
- **Don't retry automatically.** A user-initiated retry is one button click. Auto-retry would add complexity and could mask other problems (e.g. if EVERY attempt fails on the same char, it's probably a config bug not a transient drop).
- **No save if any character fails.** The panel's section 005 keeps whatever it had before — the failed write doesn't overwrite the original label with a partial mess.

**New helper:** `_labelStringToPerCharSequences(label)` returns the keystrokes broken down per character (case toggle + digit presses) instead of as a flat list. The existing `_labelStringToKeystrokes` flat-list version is kept for the dry-run preview.

**Estimated runtime change:** ~30-60s for a typical label, up from ~25-40s. The trade-off is reliability: previously a failed write would happen silently, now they're caught immediately and the user gets a clear message about which character was wrong.

## v2026.05.11.26 (PREVIOUS)
**Status:** Superseded by 2026.05.11.27

**Final position on installer-mode entry reliability:**

After extensive investigation (v22 retry logic, v23 throttle tuning, v24 three-stage exit, v25 paired-send revert, v26 multi-packet drain), the picture is:

- **TX scheduling is now solid** — v26's drain-multiple-per-tick fix gets 6 code digits sent in ~1.2 seconds, exactly matching the configured 0.2s throttle. Verified by attempt-2 timings in real-world testing.
- **Failures still occur intermittently on attempt 1** — specifically when the panel starts in `Secure System / Before Arming` rather than `Date Time` (i.e. there's an open zone like a motion sensor when we start). Root cause is unknown but is NOT in our TX path. Likely panel-side fragility around the *8+code sequence transitioning from one of those starting states, or possibly wire-level issues we can't see from the plugin.
- **The retry mechanism handles this gracefully.** Attempt 2 typically starts from `Date Time` (after the pre-flight has settled) and succeeds. Total time when retries fire: ~15-20 seconds. Acceptable for an occasional operation.

**If anyone investigates further in future:** enabling threaddebug logging would show every `IT-100 TX: 070X` line written to the wire during a failing attempt. That would distinguish "our TX path dropped a packet" from "we sent everything correctly but the panel didn't see it." All the layers above (queue handling, throttle, drain rate) have been thoroughly debugged at this point.

**v26 changes:**

- **`processIT100CommandQueue` now drains up to 4 packets per call**, sleeping briefly to respect the 0.2s throttle between presses (breaks bypass the throttle as before). This decouples TX rate from main-loop tick rate. Total max block time per call: 4 × 0.2s = 0.8s, roughly one slow-loop-tick — small enough that inbound packets won't pile up during the block.
- **Same change to `processEnvisalinkCommandQueue`** for symmetry, though the Envisalink doesn't typically have the same TX volume.

**Trade-offs:**
- During a long TX sequence (e.g. ~50 keystroke label write), the main loop will be blocked in TX more aggressively. Inbound LCD updates may be delayed by up to ~0.8s. That's acceptable — the alternative is the entire TX failing.
- The previous "TX only at the throttle rate" behaviour is gone — we now actively race the throttle to send as many packets per call as fit in 0.8s.

## v2026.05.11.25 (PREVIOUS)
**Status:** Superseded by 2026.05.11.26

**The smoking gun:** in your v24 log, attempt 1 saw the panel show `Press for Zone Bypass` (from the `*` keypress) at t=3.1s, then `Date Time` (reverted) at t=4.1s — the `8` keystroke that should have followed `*` was never registered by the panel. The `*` landed but the `8` didn't. Attempt 2 saw `*` then `8` register but then a code digit got dropped. Attempt 3 same shape. Attempt 4 worked.

This isn't TX timing pressure — it's specific keystrokes being silently lost. Which led me back to a v13 change I'd made that was based on a guess rather than evidence.

**Root cause (most likely):** v13 introduced a "paired send" optimisation where the IT-100 break packet (`070^`) was written to the serial port in the SAME `write()` call as its preceding press, microseconds apart. The reasoning was "real keypads have ~tens of milliseconds between press and release, so let's match that." But the IT-100 manual specifically notes "The IT-100 does not support handshaking and does not buffer events" — and arriving press+break essentially simultaneously may have caused the IT-100 to process them as a single net non-event, never registering the press on the keybus. Hence intermittently dropped keystrokes.

**v25 changes:**
- **Reverted the paired send.** Press and break now go through the queue separately, each sent on its own main-loop tick (~100ms apart). The break still bypasses the throttle so it follows quickly, but with enough separation that the IT-100 reliably processes the press first. Each keystroke now appears to the panel as a ~100ms hold-and-release — well within "short keypress" territory (<<1.5s threshold per the manual), but with enough separation for the IT-100 to register both.

If the dropped-keystroke issue persists after this change, it's not the paired-send theory and we'd need verbose threaddebug logging to dig further. But this explanation is genuinely the best fit for the symptoms we've been seeing — intermittent, affects different keys at different times, persists across throttle adjustments.

## v2026.05.11.24 (PREVIOUS)
**Status:** Superseded by 2026.05.11.25

**v23 results:** the throttle change helped but didn't eliminate the intermittent failure — attempt 1 still failed in your test, attempt 2 succeeded. Critically, the failure shape changed:
- Old (0.4s throttle): code digits landed close to the panel's prompt-timeout, plausibly dropped by tight timing
- New (0.2s throttle): code digits should have all landed by t=3.1s with 2.3s to spare, panel still didn't advance

So the underlying issue isn't TX pacing — **a keystroke really is occasionally being dropped or misinterpreted somewhere** (TX path, IT-100 hardware, or panel input parsing). The retry handles it gracefully but doesn't fix the root cause. Capturing verbose threaddebug TX/RX logging during a failing run would be the next investigative step if you want to chase it.

**v24 changes:**
- **Read path now uses a three-stage exit** (`#`, `#`, `#`) instead of two-stage. Diagnosis from your v23 log: after capturing the label we're three menu levels deep (inside section → LCD label sub-menu → outer installer → idle). The previous two-`#` exit relied on the panel's installer-mode idle timeout to drop us out the third level — which timed out our verification wait. Now we explicitly send the third `#` and check for idle in stage 6.
- New `exit_lcd_menu` segment between `exit_section` and `exit_installer` to make the menu navigation explicit.

**Not changed:**
- Write path still uses two `#` exit because Save (`**`) drops us back to the LCD label sub-menu (level 2), so we only need two `#`s from there. Logically correct, no change needed.

## v2026.05.11.23 (PREVIOUS)
**Status:** Superseded by 2026.05.11.24

**From the v22 logs:** the failing attempts always showed the same shape — panel reaches `Enter Your Installers Code` at ~t=2.5s, then reverts to idle at ~t=6s. That's a ~3-second window during which all 6 code digits need to land. At the v22 throttle of 0.4s per keystroke, 6 digits take ~2.4s — leaving only ~0.5s of margin. Any timing variance (slow throttle gap, IT-100 latency, panel processing) and we miss the window.

**v23 changes:**
- **`outboundPacketIntervalSecs` reduced from 0.4s to 0.2s.** 6 digits now drain in ~1.2s instead of ~2.4s — comfortably inside the panel's window with margin to spare. PowerSeries panels handle this rate fine (a fast typist on a real keypad easily exceeds 5 keys/sec). If this causes any IT-100 buffer issues we'll dial it back.
- **`_enterInstallerModeWithRetry` default `maxAttempts` raised from 2 to 4.** Belt-and-braces: even if the throttle change doesn't eliminate the issue, statistical odds of all 4 attempts failing are much lower.

If you see the panel still reverting at this throttle, the issue isn't TX timing — it's actually dropped keystrokes between us and the IT-100, and we'd need verbose threaddebug TX logging to dig further.

## v2026.05.11.22 (PREVIOUS)
**Status:** Superseded by 2026.05.11.23

**Symptom:** intermittent failure of the new Read Current Label feature in v21. Sometimes the panel revert to its idle date/time display partway through the `*8`+code sequence, and stage 1 just timed out 9 seconds later with a generic "panel didn't show installer menu" error.

**Likely root cause:** a keystroke gets dropped somewhere between the plugin and the IT-100 (still unconfirmed), so the panel receives an incomplete code and its prompt-timeout fires, sending it back to idle. The previous "manual keystroke entry" bug (where individual Send Keypress actions sometimes dropped a digit) is probably the same underlying issue, and we now know it affects batched sequences too — just less often.

**v22 changes:**

- **New `_enterInstallerModeWithRetry` helper** handles the stage-1 (`*8` + installer code) sequence with up to 2 attempts. If the first attempt times out AND the LCD looks like it's reverted to idle (date/time display), the helper does a fresh pre-flight backout and retries. This converts most intermittent dropped-keystroke failures from a hard fail to a silent recovery.
- **`_lcdLooksLikeIdle` helper** centralises the "is the panel at idle?" check.
- **`_emergencyBackoutIT100` helper** is now a real class method (was previously a closure redefined in each operation).
- **Zone label read and write now both use the retry helper** — same intermittent failures will retry automatically.
- **Test installer code now reports "panel reverted to idle"** as a distinct diagnostic message when its stage 2 times out at idle — easier to interpret than the generic "didn't show installer menu" timeout.

**What's NOT fixed:** the underlying cause of dropped keystrokes between the plugin and the IT-100. That investigation needs verbose TX/RX logging during a known-bad run, which we haven't captured yet. Retry papers over the symptom for now.

## v2026.05.11.21 (PREVIOUS)
**Status:** Superseded by 2026.05.11.22

Adds a new **Read Current Label from Panel** button to each zone device's config dialog, above the existing Label Text field. Briefly enters installer mode via the IT-100, navigates to the LCD-label-programming section for this zone, snapshots the LCD (which displays the current label), exits cleanly, and writes the captured label to the device's `labelText` pluginProp.

Same staged state machine as the write path (pre-flight `####`, then `*8` + code, then `*`, then section number), with the same critical `Enter Data` abort guard at stage 3 — if the panel somehow lands in a data section instead of the label section, the read refuses to read and backs out cleanly.

After the read completes (~15-20 seconds), the user has to **close and reopen** the device config dialog to see the populated value — Indigo's modal-dialog API doesn't allow background threads to push values into an open dialog. This is logged clearly when the operation completes.

The read is overwriting behaviour: anything currently in the Label Text field is replaced. By design — the typical workflow is "read the current label, then optionally edit, then write back."

**Use case:** before bulk-relabelling a system, run Read on each zone to populate the field with the existing panel value. Then you can review/edit each before doing the corresponding Write, and have a clear before/after trail in the event log.

## v2026.05.11.20 (PREVIOUS)
**Status:** Superseded by 2026.05.11.21

Reproduction of the stuck-zone issue confirms it pre-dates all the recent changes (also happens on v2026.05.11.4). So my drain-loop fix in v19 was a real bug but probably not THE bug we're chasing. This release adds three menu items to help diagnose and recover from stuck-zone situations without needing to wait for a fresh motion cycle:

- **Plugins → DSC Alarm → Print Zone States to Log** — dumps every zone's current Indigo state to the event log in a neat table (zone number, state, bypass, type, name). Pure snapshot of what Indigo currently believes. No side effects.

- **Plugins → DSC Alarm → Request Status from Panel** — sends TPI cmd 001 (Status Report). The panel replies with a flood of state packets covering every zone and partition. The plugin processes these through the normal dispatch path, so any drifted-out-of-sync zones get corrected. Useful as a recovery action when you know a zone is stuck.

- **Plugins → DSC Alarm → Refresh Zone States from Panel** — combines the above two: sends the status request, waits 5 seconds for the response to flow through, then dumps the resulting zone states. One-click answer to "what does Indigo think after asking the panel?". Runs in a background thread so it doesn't block Indigo's UI.

The Refresh option also serves as a workaround for the stuck-zone bug: any zone that has drifted out of sync should be corrected by the response to cmd 001.

## v2026.05.11.19 (PREVIOUS)
**Status:** Superseded by 2026.05.11.20

**Reported symptom:** A handful of motion sensor zones got stuck showing as 'open' (yellow icon) even though the physical sensors had cleared. Manually triggering fresh motion and letting it clear restored the correct state. Not related to a recent alarm event.

**Root cause: a real bug from v12 that's been quietly active ever since.** When I introduced the drain loop in `maintainIT100Connection` and `maintainEnvisalinkConnection` to consume backlogs of inbound packets quickly, I made `_pollInterface` return `''` in two distinct situations:

1. No data was available (kernel buffer empty)
2. Data was read and dispatched successfully

Both returned the same value, and the drain loop was coded to stop on `''`. So the "drain loop" actually drained at most ONE packet per main-loop iteration — defeating the entire point. Same as the pre-v12 behaviour but with a misleading name.

For most operation this wasn't catastrophic (the loop runs ~10 times a second), but **during bursts** — like when several zones change state in quick succession, or right after an alarm event when the panel emits a flood of status updates — packets fell behind. With the Envisalink especially, network buffering plus kernel buffer can absorb several packets before pressure builds up, but eventually packets get dropped if we never catch up. A missed cmd 610 (Zone Closed) leaves the zone state at 'open' indefinitely until the next physical cycle.

**Fixes in v19:**

- **`_pollInterface` now returns four distinct values**: `'-'` (port closed), `''` (no data), `'dispatched'` (packet processed), `'skipped'` (packet read but ignored as duplicate/garbage).
- **Both drain loops keep draining on `'dispatched'` or `'skipped'`**, only stopping on `''` or `'-'`. This is what the drain loop was supposed to do all along.
- **Diagnostic debug log added to `updateZoneState`**: if a state-change packet arrives for a zone but the state is already at the desired value, we log "update no-op" at debug level. If you ever see stuck zones again, enabling debug logging will tell us whether the packet is arriving (no-op log fires) or being dropped entirely (no log line).

**Honest caveat:** I can't 100% prove the drain-loop bug caused your stuck zones — it could also be a wire-level packet drop, an interface routing oddity, or something else. But the bug is a real bug regardless, and it's the most likely candidate. If after v19 you see another stuck zone with debug logging enabled, the new diagnostic should help us narrow it down further.

## v2026.05.11.18 (PREVIOUS)
**Status:** Superseded by 2026.05.11.19

The **Show Stored Credentials in Log** menu item added in v17 is commented out in `MenuItems.xml`. The callback method (`menuShowStoredCredentials` in `plugin.py`) is still in place — uncommenting the XML entry brings it back without any code changes. Hidden by default because it's a footgun for users who don't know what they're doing.

## v2026.05.11.17 (PREVIOUS)
**Status:** Superseded by 2026.05.11.18

Adds **Plugins → DSC Alarm → Show Stored Credentials in Log**, which dumps the currently-stored Envisalink TPI password, address/port, IT-100 serial port, and installer code to the event log. Useful when you've forgotten the Envisalink web-UI password (which is the same as the TPI password) and want to recover it before doing a hardware reset.

The credentials were always there — Indigo stores plugin prefs in plain text on disk — this just surfaces them to the log on demand.

## v2026.05.11.16 (PREVIOUS)
**Status:** Superseded by 2026.05.11.17

**Reported symptom:** After the alarm trips and the user enters the disarm code on a real keypad, the Indigo keypad device's `state` field stays at `tripped` until the plugin is reloaded.

**Likely root cause (theory):** PowerSeries panels re-broadcast cmd 654 (Partition In Alarm) periodically from their *alarm memory* state for several minutes after a bell cutoff — even AFTER the user has disarmed. The existing cmd 654 handler dutifully re-applied `state='tripped'` on every such re-broadcast, undoing the disarm that just happened. The dedup window of 0.5 seconds isn't long enough to catch re-broadcasts that come a minute apart.

**Fixes in v16:**

- **`alarmAcknowledgedPartitions` set tracks which partitions have had their alarm event acknowledged via user disarm.** When cmd 750/655/751 fires while `repeatAlarmTripped=True`, the partition is added to this set.
- **Cmd 654 now suppresses re-broadcasts for already-acknowledged partitions.** The first 654 for a trip still trips. Subsequent 654s from alarm memory are logged as info-level "re-broadcast suppressed" but don't revert state.
- **The set is cleared at the start of any new arming cycle** (cmd 656 Exit Delay or cmd 652 Armed), so a legitimate next trip is honoured normally.
- **Cmd 750 regex is now more defensive.** If the panel sends a non-standard `dat` format (anything other than the documented 5-char `partition + 4-char-user`), we still extract the partition number from the first character and process the disarm. Previously a malformed dat would cause 750 to silently do nothing, leaving state stuck.

**What this doesn't fix yet:** If the keypad device's `state` is stuck for some OTHER reason (e.g. cmd 750 not being sent at all, or stuck zone devices, stuck custom icons), the user reported they couldn't test these to confirm. The new "re-broadcast suppressed" info log will make it obvious whether the 654 re-broadcast theory is correct. If after v16 the state still gets stuck and you see no "re-broadcast suppressed" messages in the log, the cause is something else and we'll need another pass.

## v2026.05.11.15 (PREVIOUS)
**Status:** Superseded by 2026.05.11.16

**The critical bug from the v14 label-write test:** The PK5500 manual specifies the opening sequence as `[*][8][Installer Code][*]` — with a **trailing `*`** after the installer code. That second `*` drops you into the LCD-label-programming sub-menu. The existing plugin omitted this `*`, so after entering the installer code we were still in the top-level installer programming menu. The 3-digit section number that followed (`001` for zone 1) was then interpreted as **section 001 = Zone Definitions / data programming**, not LCD label section 001. The subsequent label-cycling keystrokes were typed as `Enter Data` byte values, which is why zone 1's definition got overwritten and the alarm tripped.

**Fixes in v15:**

- **The trailing `*` is now in the sequence**, per the PK5500 Installation Manual.
- **Zone label write is now a staged state machine** (same pattern as the test installer code button). Each stage sends a segment of keystrokes, then verifies the panel reached the expected LCD prompt before sending the next segment. Stages:
  1. Pre-flight `####` to clear any latent installer state
  2. `*8` + installer code → wait for `Enter Section`
  3. `*` → wait for `Enter LCD`
  4. Section number → **abort hard if LCD shows `Enter Data`** (means we somehow landed in a data section and any more keystrokes would corrupt programming)
  5. Label keystrokes (drain-and-wait, no per-key verification)
  6. `**` save → wait for return to `Enter LCD / Section ---`
  7. `#` exit section → wait for `Enter Section`
  8. `#` exit installer → wait for idle
- **`Invalid Access Code` detection** in stage 2 — wrong installer code is reported clearly rather than waiting for timeout.
- **Pre-write LCD logging** — after entering the section number, the plugin logs what was already on the LCD (i.e. the existing zone label) so you can see what's being overwritten.
- **Dry-run output now shows segment breakdown** — each named stage with its keystrokes, so you can see what the sequence is going to do before sending it.

**Reminder:** If you've been testing on a working panel and any zone definition got overwritten, those need restoring before the panel can usefully arm again. The plugin only programs labels — it never writes to data sections like Zone Definitions on purpose.

## v2026.05.11.14 (PREVIOUS)
**Status:** Superseded by 2026.05.11.15

**The smoking gun from the v13 test log:**

```
stage 1 t=0.0s: 'Date Time' / 'MAY 11/26 8:44p'        ← looked idle
stage 1 (*8 → wait for installer code prompt): queueing ['*', '8']
stage 1 t=1.6s: 'Enter LCD' / 'Section ---'             ← section programming?!
stage 1 t=3.3s: 'Enter LCD' / 'Section 8--'             ← entering '8' as the section
```

**Diagnosis:** The panel was already in installer mode at the start of the test — but its LCD was showing the idle date/time display (a quirk of some PowerSeries firmware: the idle screen can be drawn even while the panel is technically inside installer mode). So when we sent `*8`, it was interpreted as "open section programming menu, choose section 8" (the LCD-label-programming section), NOT as "enter installer programming with code". The first `8` of the code then got appended as the section-number digit, giving `Section 8--`.

This is a state leak from a previous test run's error-path that exited via `##` but didn't fully back out of all menu levels.

**Fixes in v14:**

- **Pre-flight backout phase added.** The test now ALWAYS sends `####` (four hash keystrokes) before starting, regardless of what the LCD currently shows. This is harmless on an idle panel (`#` does nothing on a disarmed Ready panel) and exits any depth of installer-mode menu otherwise. After the backout, a 1.5-second wait lets the LCD update, then we snapshot it.
- **Post-flight LCD verification.** If the LCD still shows installer-mode patterns (`Section`, `Enter LCD`, `Program`, `Enter Data`) after the pre-flight cleanup, the test aborts with a clear message asking the user to manually back out on a physical keypad. This is the "we tried, but the panel is stuck in a state we don't recognise" escape hatch.
- **Error-path cleanups upgraded from `##` to `####`.** All five places in the test code that previously sent `##` on failure now send `####` via a new `_emergencyBackout()` helper. The hierarchical nature of PowerSeries installer menus (section → data → ...) means two #s isn't always enough to reach idle if we got deeper than expected.

**Known issue, deferred:** Sending code digits one at a time via individual Send Keypress actions still has a missing-keystroke problem. Batching them into an action group works correctly. Parked.

## v2026.05.11.13 (PREVIOUS)
**Status:** Superseded by 2026.05.11.14

**Real-world signatures captured from manual testing:**
- `Enter Section` / `---` is the LCD signature of successful entry into installer programming mode (matched by existing `'section'` substring).
- `Invalid Access` / `Code` is the wrong-code rejection signature (NEW failure substring).

**Fixes in v13:**

- **`_waitForLCDPrompt` now supports explicit failure detection.** New optional `failure_substrings` parameter lets a single call resolve as `'success'`, `'failure'`, or `'timeout'`. Failure check runs first on each poll cycle so a wrong-code rejection isn't masked by stale "installer" text from an earlier stage.
- **Test installer code now reports wrong codes immediately**, rather than waiting for the success-match to time out. A wrong code shows in the log within ~1 second of the panel rejecting it (panel response time), not 10 seconds (previous timeout).
- **Stage 1 also gets failure detection** as belt-and-braces — if the panel somehow rejects `*8` (unexpected but possible from an odd starting state), the test logs that explicitly rather than waiting for timeout.

- **Press and break keys are now sent back-to-back in the same tick.** The 0.4s outbound throttle previously applied between every consecutive packet — including the break key that immediately follows each press. That made each emulated keypress ~0.4 seconds of "hold time," which the panel could potentially interpret as edging toward a long-press. The new behaviour:
  - Press packets are throttled (0.4s minimum gap between consecutive presses)
  - Break packets (`070^`) bypass the throttle entirely and are sent in the same `processIT100CommandQueue` call as the press that preceded them, so the panel sees a quick press+release matching real keypad behaviour
  - Break packets don't update `_it100LastTxTime` — so the next real press is throttled relative to the previous press, not relative to the break in between

**Known issue, deferred:** Sending code digits one at a time via individual Indigo Send Keypress actions still has a missing-keystroke problem (panel acts on what should be the 7th key after 8 digits). Batching them into a single action group works correctly. Root cause not yet identified; this is parked while we get the labelling functionality working.

## v2026.05.11.12 (PREVIOUS)
**Status:** Superseded by 2026.05.11.13

**Problem from the keypad test:** LCDLine1/LCDLine2 device states updated noticeably slower than a real keybus-connected keypad would refresh. During installer mode in particular, the LCD could stay "stuck" on an old screen for several seconds before catching up. This made interactive operations (installer code test, label programming, manual virtual keypad use) feel sluggish.

**Root cause:** The plugin's main loop pacing was *implicit* — it relied on the serial/socket `readline()` call blocking for up to 1 full second when no data was available. That same 1-second timeout also meant that when the panel transmitted multiple cmd 901 LCD updates in quick succession, we only consumed one packet per main-loop iteration. The kernel TTY buffer would accumulate several packets, and the device states would lag the panel by N iterations.

**Fixes in v12:**

- **Read timeout reduced from 1.0s to 0.1s** on both the IT-100 serial port and the Envisalink socket. Idle reads now return in 100ms instead of 1s.
- **`maintainIT100Connection` and `maintainEnvisalinkConnection` now drain the port in a loop**, calling `_pollInterface` repeatedly until the read returns empty. This consumes any backlog of inbound packets in a single main-loop iteration, so when the panel sends a burst of LCD updates we see them all immediately.
- **Outbound TX rate is now decoupled from main-loop tick rate.** A new `outboundPacketIntervalSecs` (default 0.4s) puts an explicit minimum gap between consecutive outbound packets, replacing the previous behaviour where TX rate was implicitly tied to main-loop tick rate. This means we can read at high rate without flooding the keybus with outbound keystrokes.
- **Explicit `self.sleep(0.1)` at the bottom of the main loop** prevents CPU thrash now that the implicit `readline()` pacing is gone.

**Net effect on interactive operations:**
- Manual virtual keypad use feels responsive — LCD updates arrive ~100ms after the panel sends them rather than ~1s.
- Keystroke entry is faster: a 6-digit code drains in ~2.4s instead of ~12s.
- CPU usage should be slightly higher when idle (now ~10 ticks/sec vs ~1) but still negligible.

**What this won't fix:** If the panel itself simply doesn't broadcast LCD updates during installer mode (some DSC firmware behaves this way), there's nothing the plugin can do at the protocol level — the IT-100 cmd 901 is push-only from the panel. We see whatever the panel sends, faster, but we can't make it send more.

## v2026.05.11.11 (PREVIOUS)
**Status:** Superseded by 2026.05.11.12

**Problem from the screenshot:** The Send Single Keypress action's help text showed arrow glyphs (`←`, `→`, `←→`) that users couldn't actually type into the input field. The field actually wants ASCII `<`, `>`, `=` for left/right/both arrows — but the help made it look like you should type the Unicode arrow itself. Worse: there was no input validation, so typing a Unicode arrow would silently send a malformed `070←` packet that the IT-100 couldn't make sense of.

**Fixes in v11:**

- New `_parseKeypressString()` helper translates user input into the IT-100 070 keypress alphabet, supporting **two styles which can be mixed**:
  - Raw chars: `*2` for trouble check, `b236>` to press F2 + 2/3/6 + right arrow (existing syntax, unchanged for backward compatibility)
  - Bracketed tokens: `[STAR]2`, `[F2]236[RIGHT]`, `[FIRE]`, `[PANIC]`, `[WAIT]` (new — more discoverable)
- Input validation rejects anything outside the IT-100 alphabet (`0-9 * # < > = a b c d e F A P` plus the `L` / `[WAIT]` pause marker). Invalid input produces a clear log error listing every problem found, and refuses to send anything (no partial sequences).
- Whitespace between keys is ignored, so `* 2` and `*2` are equivalent.
- Action help text rewritten to show a clear two-column table of "raw char ↔ bracketed token" mappings, with worked examples in both styles. No more misleading Unicode arrows in the help.
- Backward compatibility preserved: every existing automation using raw chars like `*2`, `b236>`, `*2L#` continues to work without modification.

**Known nit:** `L` was originally documented as "long keypress" but the existing code actually treated it as a 2-second pause, not a held key. The new help calls this what it really is (`[WAIT]`) and drops the misleading "long keypress" framing. A real long-keypress mechanism (per IT-100 manual: send key, wait 1.5s, send break) could be added later if anyone actually needs it.

## v2026.05.11.10 (PREVIOUS)
**Status:** Superseded by 2026.05.11.11

**Two real problems found from the v9 test log:**

1. **Fixed-time waits between keystroke groups missed the actual panel state transitions.** The v8/v9 test sent `*8` then waited 6 seconds, but by the time we read the LCD the panel had already cycled through `Secure or Arm System` (the start of the `*` menu) and was preparing for `Enter Your Installers Code`. We then sent the code digits before the panel was actually on the code prompt, so the digits were processed as menu navigation instead. The test reported "wrong code" when the code was actually correct.

2. **Long-running button callbacks were timing out Indigo's UI thread.** Indigo expects UI callbacks to return within ~10 seconds. The v9 label write blocked for ~80 seconds and the v9 installer test for ~25 seconds, both triggering "timeout waiting for plugin response" and "received unexpected packet response UiAction" errors in Indigo's event log.

**Fixes in v10:**

- New helper `_waitForLCDPrompt(expected_substrings, timeout)` polls the keypad LCD state until any partition shows one of the expected substrings, or until timeout. Logs every transition seen during the wait. Case-insensitive substring matching.
- **Test Installer Code** is now an event-driven state machine:
  - Send `*8` together, wait up to 10s for "installer" or "enter code" on the LCD
  - Send code digits, wait up to 10s for "section" or "program" (= code accepted)
  - Send `##`, wait up to 10s for return to idle ("date", "ready", "disarmed")
  - Each stage advances as soon as the panel is ready, no fixed sleeps
  - Failures at any stage attempt to back out with `##` automatically
- **Both Test Installer Code AND Zone Label Write now run in background threads.** The UI callback validates inputs synchronously, then spawns `threading.Thread` for the actual keystroke + LCD-watch work. UI returns immediately; results stream into the event log as they happen. No more Indigo UI timeouts.
- Test now refuses to start a second concurrent run while one is already in flight.

## v2026.05.11.9 (PREVIOUS)
**Status:** Superseded by 2026.05.11.10
**Live LCD watching for label programming diagnostics.**

**Problem from v8:** Mike pointed out that the keypad device states (`LCDLine1`/`LCDLine2`) already contain everything we need to see what the IT-100 is displaying — they're updated in real time by the cmd 901 handler when the panel transmits LCD updates. But the test in v8 used `time.sleep()` then read a single snapshot at the end of each stage, which missed all the intermediate transitions. By the time the snapshot was taken, the panel had moved on through several display states.

**Fix:** New `_watchKeypadLCD(duration)` helper that polls the keypad device's LCD state every 200ms for `duration` seconds, logging each distinct transition with a timestamp. Static periods produce no output; only changes are logged. The user now sees a full timeline of LCD states in the event log.

Applied to:
- **Test Installer Code** — each of the three stages (`*8`, code digits, `##`) now spawns a watch window of appropriate duration. The log shows every LCD transition during each stage with a `t=X.Xs` timestamp.
- **Zone Label Write** — after queueing all keystrokes, the action thread blocks for the full estimated duration (~80s for a typical label) watching LCD transitions live. Every step of programming is captured: installer mode entry, section navigation, character cycling, save menu, exit. Blocks the action for the full duration, but that's the right behaviour — the user pressed a button and is waiting for diagnostic feedback in one place.
- Removed the redundant "snapshot before" log line since `_watchKeypadLCD` logs its own `t=0` baseline.

This blocks the action thread for as long as the watch window — up to ~85 seconds for a long label write. That's intentional. The main loop and queue draining continue running independently on their own thread, so the panel keeps processing keystrokes while we watch.

## v2026.05.11.8 (PREVIOUS)
**Status:** Superseded by 2026.05.11.9
**The big one:** Eight action methods were silently broken since the dual-interface refactor. They appended commands to `self.txCmdList`, an orphaned single-interface queue that was never read or drained anywhere in the plugin. The methods affected:

- Disarm Alarm
- Arm Alarm in Stay Mode
- Arm Alarm in Away Mode
- Arm Alarm in Away Global Mode
- Trigger Panic Alarm
- Send Single Keypress (070)
- Sync Date/Time with Indigo Server (both the manual action AND the 3am auto-sync)
- Adjust Thermostat

All of these are now routed through `sendCommand()` (or, for the thermostat, called directly into `setThermostat()` since it uses a multi-step synchronous protocol that doesn't fit the async queue model). The legacy `self.txCmdList` attribute has been removed entirely.

**Per-action interface picker:** Added a "Send Via" dropdown to each interface-agnostic action (Disarm, Arm Stay, Arm Away, Arm Global, Panic, Sync Time) so the user can override the plugin-wide preference on a per-action basis if needed. Options are Auto (use plugin default with fallback), IT-100, or Envisalink. The picker defaults to Auto so existing automations carry on working with no config change.

**Send Single Keypress (070) — IT-100 only:** TPI command 070 is not supported on Envisalink (where keystrokes use 071). The action is now explicitly labelled "Send Single Keypress – IT-100 only" and hard-refuses if IT-100 isn't connected (no fallback, because Envisalink would silently misinterpret the command). For Envisalink-based keystrokes use "Send Multiple Keypresses – EVL only" (071) instead.

**Backward compatibility:** Actions saved in existing Indigo schedules and triggers before v8 won't have a `commandInterface` prop on them. The helper `_resolveCommandInterface()` tolerates this and defaults missing values to Auto, so nothing breaks.

## v2026.05.11.7 (PREVIOUS)
**Status:** Superseded by 2026.05.11.8
**Changes:**
- ✅ Custom label text field on each zone device — lets you write whatever you want to the keypad slot rather than always using the Indigo device name. Pre-populates blank; falls back to the device name when left blank. Supports A-Z, a-z, 0-9, space; non-mappable chars become spaces.
- ✅ Mixed-case support. The label translator now preserves the case of letters in your input and inserts `*>*` case-toggle keystrokes (open the *-menu → advance to "Change Case" → select) where the case mode needs to change. Keypad starts in uppercase for each new label.
- ✅ Optional "Prefix with zone number" checkbox prepends e.g. "05 " to the label. Eats into the 14-char limit; the dry-run preview shows exactly what will fit.
- ✅ Label now pads to the full 14-char LCD width with trailing spaces, so the entire line is overwritten. Previously writing "Hi" over "Zone 1" would have left "Hine 1" on the LCD with leftover chars from the previous label.
- ✅ All "watch the keypad" language replaced with "watch the keypad device's LCDLine1/LCDLine2 states (or a control page mapped to them)" — the IT-100 has no physical display or beeper.
- ✅ "Test Installer Code" rewritten as a STAGED test that reads the keypad LCD state between each stage and logs it. Stages: baseline → after `*8` → after code digits → after `##`. Tells you which step (if any) failed to land. Includes guidance for interpreting the results in the log.
- ✅ Both the test and the real-write now flush any stale keystrokes from the IT-100 queue before starting, so they don't combine with leftover state from a previous failed attempt.
- ✅ Real-write logs the baseline LCD state before sending any keystrokes, as a reference point.

## v2026.05.11.6 (PREVIOUS)
**Status:** Superseded by 2026.05.11.7
**Changes:**
- ✅ Installer code moved from per-zone device config to plugin-wide config (Plugins → DSC Alarm → Configure). It's a per-panel value, not a per-zone one — having it on every zone was both repetitive and confusing.
- ✅ New "Test Installer Code" button in plugin config sends `*8 <code> ##` via IT-100 — enters installer mode then immediately exits. Lets you verify the code works without committing to a full label write. Same safety rails as the real write (IT-100 only, no partitions armed).
- ✅ Dropped the "all zones must be closed" check from label-write. Programming labels doesn't require zones closed — that was over-cautious belt-and-braces that wasn't actually needed. The disarmed-partitions check is kept (entering installer mode while armed would be very bad).
- ✅ Installer code no longer masked in the event log. Mike's call — it's a debugging tool, not a security boundary.

## v2026.05.11.5 (PREVIOUS)
**Status:** Superseded by 2026.05.11.6
**New feature: IT-100 Virtual Keypad Zone Label Programming**
- ✅ New "Write Device Label to IT-100 Virtual Keypad" button on each Alarm Zone device's config page. Translates the Indigo device name into the keypress sequence a human would type on a PK5500 keypad to program that zone's label, and queues those keystrokes onto the IT-100 via TPI command 070.
- ✅ Per-character translation table (A-Z, 0-9, space) following the PK5500 manual's key-cycling convention (1=ABC1, 2=DEF2, ..., 9=YZ90, 0=Space). Unmappable characters become spaces; runs of spaces collapse; truncated to 14 chars.
- ✅ Mandatory dry-run mode by default. The button logs every keystroke it would send and refuses to transmit until the "I have read the above and want to write for real" checkbox is ticked. Confirmation is per-device, not global.
- ✅ Hard refusal of Envisalink path. The Envisalink TPI locks out 070/071 keypresses in installer mode (error 017) and there is no programmatic escape — dead-lock requires a power cycle. We will not even try.
- ✅ Pre-flight safety checks: IT-100 enabled, IT-100 connected, all partitions disarmed, all zones closed, installer code present and 4-6 digits.
- ✅ "STOP / Exit Installer Mode" button alongside, which atomically flushes the queued keystrokes and sends `##` to back out. Best-effort — if the panel is truly stuck, only a power cycle helps.
- ✅ All status output via `_alwaysLog` so progress is visible regardless of Event Logging Level setting.
- ✅ Installer code is stored in the device's secure pluginProps (encrypted by Indigo). Not visible in event log output (masked as `?` characters in the printed sequence).
- ⏳ Bulk "program all zones" and "broadcast to other keypads" actions to come later, after the per-zone path is confirmed working.

## v2026.05.11.4 (PREVIOUS)
**Status:** Superseded by 2026.05.11.5
**Bug fix:**
- ✅ `Log Interface Source` checkbox now actually gates the `(via IT-100)` / `(via Envisalink)` suffix on Log Changes output. Previously the setting only controlled the verbose per-message debug logging in `_pollInterface`, but the source suffix on zone-change log lines was hardcoded on whenever a source was known. Now both are gated by the same checkbox: tick it for the dual-interface attribution, untick it for cleaner one-line zone messages.

## v2026.05.11.3 (PREVIOUS)
**Status:** Superseded by 2026.05.11.4
**Changes:**
- ✅ Test button output now uses `indigo.server.log` directly via a new `_alwaysLog` helper, so connection test results always appear in the event log regardless of the Event Logging Level setting. Pressing the button is an explicit, interactive request for feedback — it would be confusing for the global log-level filter to silently swallow the response.
- ✅ Per-zone Log Changes output (motion detected, door opened/closed, water detected, bypass changes, temp sensor changes) also bypasses the Event Logging Level filter via `_alwaysLog`. The Log Changes checkbox is itself a per-zone opt-in, so the global setting shouldn't override that decision.
- ✅ The `TRIPPED!` warning, debug diagnostics, and all internal/operational messages still respect the Event Logging Level — only the explicitly user-opted-in messages bypass it.
- ✅ `Log Interface Source` now defaults to ON. With both interfaces active it's the only way to see the dual-interface race playing out, so it earns its keep by default. (Existing installs: tick the box once in the plugin config to opt in — the XML default only affects new installs.)
- ✅ `debugInterfaceSource` and the `enableIT100`/`enableEnvisalink` flags are now read via `_propIsTrue` so they correctly handle the string values Indigo can store for checkboxes (`'true'`/`'false'`/`'0'`/`'1'`).

## v2026.05.11.2 (PREVIOUS)
**Status:** Superseded by 2026.05.11.3
**Bug fixes:**
- ✅ Test buttons rewritten to be non-destructive. The old IT-100 test opened a competing serial port (which on a live system can hang or produce no output); the old Envisalink test opened a competing TCP session and forced the live connection to drop with errno 54 (connection reset by peer) every time it was pressed. Both now report on the live connection state the plugin is already maintaining. For Envisalink, if no live session exists, a lightweight TCP reachability check (open+close, no login) runs to distinguish "wrong address/port" from "wrong password".
- ✅ `zoneLogChanges` checkbox parsing made fully robust. Indigo stores checkbox values as strings (`'1'`/`'0'`/`'true'`/`'false'`) depending on version and context; the v1 fix using `.get(key, True)` would have treated stored `'false'` as truthy. Added a `_propIsTrue` helper that handles bool, int, float, and all common string forms. Same helper used for the temperature-sensor Log Changes check.
- ✅ Added a debug-level diagnostic line in `updateZoneState` showing the raw `zoneLogChanges` value and parsed result — flip the log level to Debug if Log Changes still isn't firing for a zone and the log will tell you exactly what's stored.

## v2026.05.11.1 (PREVIOUS)
**Status:** Superseded by 2026.05.11.2
**Bug fixes & quality-of-life:**
- ✅ Fixed `Test IT-100 Connection` and `Test Envisalink Connection` buttons (`UiAction: No registered converter ... CXmlDict from type bool`) — PluginConfig button callbacks must return `valuesDict`, not a `(bool, dict, errorDict)` tuple. Results now surface via the log.
- ✅ Removed stale "restore previous Envisalink connection" logic from the test button — it was reusing an already-closed socket. The live connection is re-established by `runConcurrentThread` on its next pass, as the existing comment noted.
- ✅ Fixed `Log Changes` checkbox on zone & temp-sensor devices (was comparing the Python `bool` returned by the checkbox to the integer `1` — silently false). Now uses a truthy `.get('zoneLogChanges', True)` so older zones missing the prop still log per the XML default.
- ✅ Zone change log lines now show which interface delivered the message, e.g. `Alarm Zone 'Lounge Motion' Motion Detected. (via IT-100)` or `Alarm Zone 'Bathroom Door' Opened. (via Envisalink)`. This confirms the dual-interface race-wins logic is actually working.
- ✅ Type-aware verbs in Log Changes output: motion sensors say "Motion Detected/Cleared", water sensors say "Water Detected/Cleared", glass break / shock / fire / gas / CO / lock all read naturally; doors/windows keep the original "Opened/Closed" wording.
- ✅ Source attribution threaded through `_dispatchMessage` → `updateZoneState`, `updateZoneBypass`, `updateSensorTemp`, and the cmd 602 "Zone Restored" info log.

## v1.0.0 (PREVIOUS)
**Status:** Ready for testing
**Changes:**
- ✅ Updated to Indigo 2025.1 (API 3.7)
- ✅ Updated bundle ID to com.frightideas.indigoplugin.dscAlarm2
- ✅ Added sensor state support (onState/offState) for Home Management plugin compatibility
- ✅ Added device broadcasting (broadcastToSubscribers) for plugin subscriptions
- ✅ Updated Python requirement to 3.11
- ✅ Added SupportsSensorValue to alarmZone devices
- ✅ Code formatting improvements (aligned constants)
- ✅ Added custom log() function

**What Works:**
- All existing single-interface functionality (IT-100 OR Envisalink)
- Zone sensors now work as standard Indigo sensors
- Other plugins can subscribe to DSC zone state changes
- Fully backward compatible with existing configs

**Testing Needed:**
- Verify IT-100 connection still works
- Verify Envisalink connection still works
- Test Home Management plugin subscription
- Verify sensor states update correctly

## v1.0.1 (NEXT - DUAL-INTERFACE SUPPORT)
**Status:** Planned
**Major Changes:**

### 1. Configuration Architecture
**File:** PluginConfig.xml
- Replace single configInterface dropdown with dual checkboxes:
  - enableIT100 (checkbox)
  - enableEnvisalink (checkbox)
- Add primaryInterface dropdown ('IT-100', 'Envisalink', 'Auto')
- Show/hide IT-100 settings based on enableIT100
- Show/hide Envisalink settings based on enableEnvisalink
- Migration: Auto-detect old single-interface config and convert

### 2. Connection Management
**File:** plugin.py
- Create connection manager classes:
  ```python
  class ConnectionManager:
      # Base class for connection handling
  
  class IT100Connection(ConnectionManager):
      # Serial port management
      # Independent receive thread
      # Command queue processing
  
  class EnvisalinkConnection(ConnectionManager):
      # Socket management
      # Independent receive thread  
      # Command queue processing
  ```

### 3. Command Routing
**Changes to existing methods:**

**Envisalink-Only Commands** (force to Envisalink):
- methodDoorChimeEnable() - line ~596
- methodDoorChimeDisable() - line ~611
- methodBypassZone() - line ~538
- methodBypassZoneCancel() - line ~564
- methodBypassZoneRecall() - line ~580

**Change from:**
```python
if self.useSerial is True:
    self.logger.error("This Action does not work with IT-100.")
    return
self.txCmdList.append((kCmdNormal, tx))
```

**Change to:**
```python
if not self.envisalinkConnected:
    self.logger.error("This Action requires Envisalink connection.")
    return
self.sendCommand(tx, force_interface='envisalink')
```

**Universal Commands** (use primary):
- methodDisarmAlarm()
- methodArmStay()
- methodArmAway()
- All other alarm/zone commands

**Update to:**
```python
self.sendCommand(tx)  # Routes to primary automatically
```

### 4. Receive Processing
**Current:** Single rxCmd() processes data from one interface
**New:** Both interfaces feed to common processor

```python
def processCommand(self, cmd, dat, source='unknown'):
    """
    Process commands from either interface
    source: 'it100' or 'envisalink'
    """
    # Check for duplicate (already processed recently)
    key = f"{cmd}:{dat}"
    now = time.time()
    if key in self.lastStateUpdate:
        if now - self.lastStateUpdate[key] < 1.0:
            return  # Skip duplicate
    self.lastStateUpdate[key] = now
    
    # Existing rxCmd logic here
    ...
```

### 5. Run Concurrent Thread Refactor
**Current:** Single state machine
**New:** Dual connection management

```python
def runConcurrentThread(self):
    while not self.shutdown:
        # Manage IT-100 connection
        if self.enableIT100:
            self.manageIT100()
        
        # Manage Envisalink connection
        if self.enableEnvisalink:
            self.manageEnvisalink()
        
        # Process command queues
        self.processCommandQueues()
        
        # Update timers
        self.updateTimers()
        
        self.sleep(0.1)

def manageIT100(self):
    if not self.it100Connected:
        self.connectIT100()
    else:
        self.maintainIT100Connection()

def manageEnvisalink(self):
    if not self.envisalinkConnected:
        self.connectEnvisalink()
    else:
        self.maintainEnvisalinkConnection()
```

### 6. Configuration Reading
**Update getConfiguration():**
```python
# Detect old single-interface config and migrate
if 'configInterface' in valuesDict:
    if valuesDict['configInterface'] == 'serial':
        valuesDict['enableIT100'] = True
        valuesDict['primaryInterface'] = 'IT-100'
    elif valuesDict['configInterface'] == 'twods':
        valuesDict['enableEnvisalink'] = True
        valuesDict['primaryInterface'] = 'Envisalink'

self.enableIT100 = valuesDict.get('enableIT100', False)
self.enableEnvisalink = valuesDict.get('enableEnvisalink', False)
self.primaryInterface = valuesDict.get('primaryInterface', 'none')
```

### 7. Files to Modify
1. **plugin.py** (~2500 lines)
   - Add connection manager classes
   - Refactor runConcurrentThread
   - Update command routing methods
   - Update getConfiguration
   
2. **PluginConfig.xml** (~300 lines)
   - Redesign interface selection
   - Add dual-interface controls
   - Update visibility bindings

### 8. Testing Strategy
1. Test IT-100 only (backward compat)
2. Test Envisalink only (backward compat)
3. Test both connected, IT-100 primary
4. Test both connected, Envisalink primary
5. Test Envisalink-specific commands with IT-100 primary
6. Test reconnection scenarios
7. Test migration from v1.0.0 to v1.0.1

### 9. Estimated Effort
- Configuration UI: 2 hours
- Connection classes: 4 hours
- Command routing refactor: 3 hours
- runConcurrentThread refactor: 4 hours
- Testing & debugging: 6 hours
**Total: ~19 hours of development**

## v1.0.2 (FUTURE)
**Potential enhancements:**
- Connection status indicators in UI
- Interface failover (auto-switch if primary fails)
- Per-command interface preference (advanced)
- Connection statistics logging

## Notes
- v1.0.0 provides immediate value (sensor states, broadcasting)
- v1.0.1 is substantial refactoring but maintains backward compatibility
- All changes preserve existing functionality while adding dual-interface
