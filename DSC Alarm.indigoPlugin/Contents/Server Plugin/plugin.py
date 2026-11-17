#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename: plugin.py
# Author: Durosity (modified from Travis Cook and Monstergerm)

"""
DSC Alarm Plugin v1.0.0
Updated for Indigo 2025.1 (API 3.7) with dual-interface support, sensor states, and device broadcasting.
This plugin supports DSC alarm panels and can connect to BOTH IT-100 AND Envisalink simultaneously.
It does not work with Vista 20p panels.
Envisalink 4 is the recommended interface.
Anything related to thermostats has not been field tested.
The plugin can be found on Github and in the Indigo Plugin Store.
The Github repository also shows examples for a control page, buttons for Arm and Disarm and icons for zones, bypass etc.
https://github.com/IndigoDomotics/DSC-Alarm
This plugin requires Python 3.11 and Indigo 2025.1 and higher.

"""

import os
import platform
import sys
import re
import time
from datetime import datetime
import logging
import threading
import serial
try:
    import indigo
except ImportError:
    pass

# Note the "indigo" module is automatically imported and made available inside
# our global name space by the host process.


# ============================ Configuration Constants ============================
kSocketPort            = 1514
kSocketBufferSize      = 1024
kSocketTimeout         = 1

kZoneStateOpen         = 'open'
kZoneStateClosed       = 'closed'
kZoneStateTripped      = 'tripped'
kZoneStateOpenLatched  = 'openLatched'
# Zone alarm involvement (independent of the live open/closed state)
kAlarmStateNormal      = 'normal'
kAlarmStateTripped     = 'tripped'   # panel reports the zone in alarm right now (cmd 601)
kAlarmStateMemory      = 'memory'    # was in alarm, since restored (cmd 602); cleared on next arm
kZoneBypassNo          = 'nobypass'
kZoneBypassYes         = 'bypassed'
kZoneGroupStateOpen    = 'zoneOpen'
kZoneGroupStateClosed  = 'allZonesClosed'
kZoneGroupStateTripped = 'zoneTripped'
kAlarmStateDisarmed    = 'disarmed'
kAlarmStateExitDelay   = 'exitDelay'
kAlarmStateFailedToArm = 'FailedToArm'
kAlarmStateArmedStay   = 'armedStay'
kAlarmStateArmedAway   = 'armedAway'
kAlarmStateEntryDelay  = 'entryDelay'
kAlarmStateTripped     = 'tripped'
kKeypadStateChimeEnabled  = 'enabled'
kKeypadStateChimeDisabled = 'disabled'
kAlarmArmedStateDisarmed  = 'disarmed'
kAlarmArmedStateStay   = 'stay'
kAlarmArmedStateAway   = 'away'
kReadyStateTrue        = 'ready'
kReadyStateFalse       = 'notready'
kPanicStateNone        = 'none'
kPanicStateFire        = 'fire'
kPanicStateSmoke       = 'smoke'
kPanicStateAmbulance   = 'ambulance'
kPanicStatePanic       = 'panic'
kPanicStateDuress      = 'duress'

kLedIndexList   = ['None', 'Ready', 'Armed', 'Memory', 'Bypass', 'Trouble', 'Program', 'Fire', 'Backlight', 'AC']
kLedStateList   = ['off', 'on', 'flashing']
kArmedModeList  = ['Away', 'Stay', 'Away, No Delay', 'Stay, No Delay']
kPanicTypeList  = ['None', 'Fire', 'Ambulance', 'Panic']
kMonthList      = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
kLogLevelList   = ['Detailed Debug', 'Debug', 'Info', 'Warning', 'Error', 'Critical']

kCmdNormal              = 0
kCmdThermoSet           = 1
kPingInterval           = 301
kHoldRetryTimeMinutes   = 3
# =================================================================================


# ============================ Custom Log Function ================================
def log(message, level="INFO"):
    indigo.server.log(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", level=level)
# =================================================================================


##########################################################################################
class Plugin(indigo.PluginBase):

	########################################
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

		self.States = self.enum(STARTUP=1, HOLD=2, HOLD_RETRY=3, HOLD_RETRY_LOOP=4, BOTH_INIT=5, SO_CONNECT=6, ENABLE_TIME_BROADCAST=7, BOTH_PING=8, BOTH_POLL=9)
		self.state = self.States.STARTUP

		# ============================ Configure Logging =================================
		try:
			self.logLevel = int(self.pluginPrefs.get("logLevel",20))
			if self.logLevel < 5:  # convert old logging pref settings to level 20(INFO)
				self.logLevel = logging.INFO
				self.logger.info("We are converting old log settings.")
		except:
			self.logLevel = logging.INFO
			self.logger.info("We are forcing log settings to INFO.")
		# Set the format and level handlers for the logger
		log_format = '%(asctime)s.%(msecs)03d\t%(levelname)-10s\t%(name)s.%(funcName)-28s %(msg)s'
		self.plugin_file_handler.setFormatter(logging.Formatter(fmt=log_format, datefmt='%Y-%m-%d %H:%M:%S'))
		self.indigo_log_handler.setLevel(self.logLevel)
		# ================================================================================

		self.shutdown                    = False
		self.configRead                  = False
		self.interfaceState              = 0
		self.zoneList                    = {}
		self.tempList                    = {}
		self.zoneGroupList               = {}
		self.trippedZoneList             = []
		self.triggerList                 = []
		self.keypadList                  = {}
		self.createVariables             = False
		self.port                        = None
		self.repeatAlarmTripped          = False
		# Tracks when repeatAlarmTripped was last set to True. Used by the
		# stuck-flag watchdog in _watchdogRepeatAlarmTripped() to force-clear
		# the flag if it's been True for an unreasonably long time (which
		# breaks all zone-close updates — see cmd 610 handler). 0 means the
		# flag is currently False.
		self.repeatAlarmTrippedSetAt     = 0.0
		# Per-partition "alarm has been acknowledged" set. When a user disarms
		# after a trip (cmd 750/655 with repeatAlarmTripped=True), the
		# partition is added here. While a partition is in this set, any
		# subsequent cmd 654 (Partition In Alarm) broadcasts from the panel
		# are treated as info-only and do NOT re-set the partition state to
		# tripped. The panel periodically re-broadcasts 654 while in alarm
		# memory state for several minutes after an event, and without this
		# suppression those re-broadcasts would revert the user's disarm.
		# Cleared when the partition starts a new arming cycle (cmd 656 exit
		# delay or cmd 652 armed) so a legitimate next trip will be honoured.
		self.alarmAcknowledgedPartitions = set()
		self.isPortOpen                  = False
		self.useSerial                   = False
		# (txCmdList removed in v2026.05.11.8 — it was an orphaned legacy queue
		# from before the dual-interface refactor. All action methods now route
		# through sendCommand() which dispatches to it100TxCmdList or
		# envisalinkTxCmdList per the per-action interface picker.)
		self.closeTheseZonesList         = []
		self.currentHoldRetryTime        = kHoldRetryTimeMinutes
		self.ourVariableFolder           = None
		self.configEmailUrgent           = ""
		self.configEmailNotice           = ""
		self.configEmailDisarm           = ""
		self.configEmailUrgentSubject    = ""
		self.configEmailNoticeSubject    = ""
		self.configEmailDisarmSubject    = ""
		self.userCode                    = ""
		self.userLabel                   = ""
		self.userLabelDict               = {}
		self.configSpeakVariable         = None
		self.configKeepTimeSynced        = True
		self.configUseCustomIcons        = True
		# Historical-alarm handling. See cmd 654 handler. Init defaults match
		# the PluginConfig.xml defaults; real values get read in getConfiguration.
		self.configStartupAlertOnHistoricalAlarm = True
		self.configStartupGracePeriodSeconds     = 120
		# Zone-latching during alarm. If True, zone-close events that
		# arrive while repeatAlarmTripped is True are deferred to
		# closeTheseZonesList and replayed on disarm — preserves the
		# "what was open during the trip" snapshot for control pages.
		# If False (default), zone closes apply immediately even during
		# a trip, so downstream automation sees accurate state. See cmd
		# 610 handler.
		self.configLatchZonesDuringAlarm         = False
		# Connection health: devices are marked "unavailable" when no interface has
		# been connected for configUnavailableGrace seconds, and resynced on reconnect.
		self.configUnavailableGrace              = 30
		self.connLostAt                          = time.time()   # nothing connected yet at startup
		# Start "unavailable": nothing is confirmed until the first status report
		# after connecting has been processed (covers crashes / power cuts too).
		self.devicesUnavailable                  = True
		self.startupUnconfirmed                  = True
		self.resyncDeadline                      = 0
		self.resyncOpenZones                     = set()
		self.resyncCollecting                    = False   # recording zone-open reports
		# Global override for the per-zone "Log Changes" checkbox. When True,
		# every zone state change is logged regardless of the per-device
		# setting. Useful for diagnostics. See _shouldLogZoneChanges().
		self.configLogAllZoneChanges             = False
		# Time the plugin started — used to determine whether we're still
		# within the startup grace period when cmd 654 broadcasts arrive.
		self.pluginStartTime             = time.time()
		# Set to True once we've sent the post-connect cmd 001 status pull
		# for the current connection. Reset on disconnect / reconnect so
		# every fresh interface link does a one-shot status refresh.
		self._postConnectStatusSent_IT100      = False
		self._postConnectStatusSent_Envisalink = False
		self.timesyncflag                = True
		self.troubleCode                 = 0
		self.troubleClearedTimer         = 0
		
		# Dual-interface support variables
		self.enableIT100                 = False
		self.enableEnvisalink            = False
		self.commandInterface            = 'envisalink'  # Which interface sends outbound commands
		self.debugInterfaceSource        = True          # Log which interface delivered each message (default on)
		self.it100Connected              = False
		self.envisalinkConnected         = False
		self.it100Port                   = None
		self.envisalinkSocket            = None
		self.lastStateUpdate             = {}
		self.it100TxCmdList              = []
		self.envisalinkTxCmdList         = []
		self.msgDedupCache               = {}            # (cmd+dat) -> timestamp, for deduplication
		self.msgDedupWindowSecs          = 0.5           # Suppress duplicate messages within this window

		# TX pacing — minimum gap between outbound packets per interface.
		# Decoupled from the main loop tick so we can read at high rate
		# without flooding the keybus with outbound keystrokes.
		#
		# Was 0.4s in v12-v22 but that bumped up against the panel's
		# installer-code-prompt timeout: 6 code digits at 0.4s = ~2.4s of
		# TX, leaving only ~0.5s of margin before the panel reverts to
		# idle (observed ~3s window from prompt-shown to revert). With
		# any timing variance we'd lose the race and the panel would
		# time out waiting for the last digit.
		#
		# 0.2s gives 6 digits in ~1.2s — comfortably inside the panel's
		# window with margin to spare. PowerSeries panels handle this
		# rate fine in practice (a fast typist on a real keypad easily
		# exceeds 5 keys/sec). If this ends up causing IT-100 buffer
		# issues, dial it back up.
		self.outboundPacketIntervalSecs  = 0.2
		self._it100LastTxTime            = 0.0
		self._envisalinkLastTxTime       = 0.0
		
		# Connection failure tracking to prevent rapid reconnect loops
		self.connectionFailureCount      = 0
		self.lastConnectionFailure       = 0
		self.connectionBackoffTime       = 0
		
		# Latch timer tracking
		self.latchTimers                 = {}  # {zoneId: expirationTime}

		# Set True while the 'Read All Zone Labels' action is running, so
		# the per-zone Read button can refuse to start a second concurrent
		# read on the same IT-100.
		self.bulkLabelReadInProgress     = False

		# Set True when the user asks to cancel an in-progress bulk
		# operation. Workers check this flag between zones and exit
		# cleanly if set. Cleared at the start of each new bulk run.
		self.bulkOperationCancelRequested = False

		# Short label describing the currently-running bulk operation,
		# e.g. 'Read All Zone Labels', 'Write Specific Zone Labels',
		# 'Broadcast Labels'. Set by each worker at start, cleared in
		# its finally block. Used by the Cancel action to tell the user
		# what's actually running and whether it can be cancelled.
		self.bulkOperationKind            = None

		# Per-zone outcomes list, populated by _recordZoneOutcome during
		# a single-zone or bulk operation, dumped to the log by
		# _logZoneOutcomes when the operation finishes. None when no
		# operation is active (defensive — workers always call
		# _beginZoneOutcomes() at the top of their run).
		self.bulkOperationOutcomes        = None
		
		try:
			self.pluginPrefs["TwoDS_Port"]
		except KeyError:
			self.pluginPrefs["TwoDS_Port"] = "4025"
				
		if "DSC" not in indigo.variables.folders:
			indigo.variables.folder.create("DSC")
		if "DSC_Alarm_Text" not in indigo.variables:
			indigo.variable.create("DSC_Alarm_Text", value="", folder="DSC")
		if "DSC_Alarm_Memory" not in indigo.variables:
			indigo.variable.create("DSC_Alarm_Memory", value="no tripped zones", folder="DSC")
		if "DSC_Command" not in indigo.variables:
			indigo.variable.create("DSC_Command", value="#", folder="DSC")
		if "DSC_Last_User_Disarm" not in indigo.variables:
			indigo.variable.create("DSC_Last_User_Disarm", value="", folder="DSC")
		if "DSC_Last_Zone_Active" not in indigo.variables:
			indigo.variable.create("DSC_Last_Zone_Active", value="", folder="DSC")
		if "DSC_Last_Motion_Active" not in indigo.variables:
			indigo.variable.create("DSC_Last_Motion_Active", value="", folder="DSC")



	def enum(self, **enums):
		return type('Enum', (), enums)

	def __del__(self):
		indigo.PluginBase.__del__(self)

	@staticmethod
	def _propIsTrue(props, key, default=True):
		"""Robust truthy check for an Indigo pluginProps value.

		Indigo can hand back checkbox values as bool (`True`), as int (`1`), or
		as strings ('1', 'true', 'True', 'yes', 'on'). The original code did
		`pluginProps['key'] == 1` which is False for the string '1', so the
		Log Changes checkbox never fired. A naive `.get(key, True)` is also
		wrong, because the string 'false' is still truthy in Python.

		This helper normalises all the common shapes. Returns `default` when
		the prop is missing entirely (which preserves the XML defaultValue
		semantics for older devices that haven't re-saved their config since
		the prop was added).
		"""
		if key not in props:
			return default
		raw = props[key]
		if isinstance(raw, bool):
			return raw
		if isinstance(raw, (int, float)):
			return raw != 0
		if isinstance(raw, str):
			return raw.strip().lower() in ('1', 'true', 'yes', 'on')
		return bool(raw)

	def _shouldLogZoneChanges(self, props):
		"""Return True if a zone-state change should be logged to the
		event log.

		Combines the per-device `zoneLogChanges` pluginProp with the
		global `Log all zone state changes (override per-device setting)`
		plugin-config checkbox. The global override, when ticked, makes
		the plugin behave as if every zone device had Log Changes enabled —
		useful for diagnostics without needing to tick every device
		individually.

		Either condition being true is enough.
		"""
		if getattr(self, 'configLogAllZoneChanges', False):
			return True
		return self._propIsTrue(props, 'zoneLogChanges')

	def _alwaysLog(self, message, isError=False):
		"""Write a message directly to the Indigo event log, bypassing the
		Python logger's level filter (which is set from the `Event Logging
		Level` config dropdown).

		Use this sparingly — only for messages where the user has explicitly
		opted in to seeing them at the per-feature level (per-zone Log Changes,
		interactive connection-test results) and would be surprised if the
		global Event Logging Level setting silently hid them.

		`isError=True` colors the entry red and marks it as an error in the
		Indigo event log UI.
		"""
		indigo.server.log(message, type="DSC Alarm", isError=isError)

	def _setRepeatAlarmTripped(self, newValue, reason):
		"""Setter for self.repeatAlarmTripped that logs every change with
		the reason (typically the cmd code that triggered it). Centralised
		because this flag, when stuck at True, silently breaks all zone-
		close updates (see cmd 610 handler): zone-close messages from the
		panel get deferred into self.closeTheseZonesList instead of being
		applied to the device, so motion sensors etc. stay showing as 'open'
		forever. We've seen this stick in the wild for hours after a trip
		event without any visible errors, so capturing the lifecycle of
		this flag is critical for debugging.

		Also updates self.repeatAlarmTrippedSetAt so the watchdog can
		measure how long the flag has been True.
		"""
		oldValue = self.repeatAlarmTripped
		self.repeatAlarmTripped = bool(newValue)
		if oldValue != self.repeatAlarmTripped:
			if self.repeatAlarmTripped:
				self.repeatAlarmTrippedSetAt = time.time()
				self.logger.warning(
					f"repeatAlarmTripped: False -> True ({reason}). "
					f"Subsequent zone-close messages will be deferred to "
					f"self.closeTheseZonesList until the next disarm "
					f"(cmd 655/750/751) clears this flag."
				)
			else:
				heldSecs = time.time() - self.repeatAlarmTrippedSetAt if self.repeatAlarmTrippedSetAt else 0
				self.repeatAlarmTrippedSetAt = 0.0
				self.logger.info(
					f"repeatAlarmTripped: True -> False ({reason}). "
					f"Flag was held for {heldSecs:.1f}s. "
					f"{len(self.closeTheseZonesList)} deferred zone-close(s) "
					f"will now be applied."
				)
		else:
			# No-change assignment — still useful to log at debug level
			# so we can see when a disarm handler ran but found the flag
			# already in the expected state.
			self.logger.debug(
				f"repeatAlarmTripped: no change (already {self.repeatAlarmTripped}), reason={reason}"
			)

	def _watchdogRepeatAlarmTripped(self):
		"""Failsafe: if repeatAlarmTripped has been True for an unreasonably
		long time, force-clear it. This protects against the 'stuck tripped'
		bug where the disarm cmd from the panel apparently never arrived
		(or arrived but didn't clear the flag for some reason), leaving the
		plugin permanently unable to update zone states.

		The longest legitimate hold should be the panel's bell-cutoff +
		alarm-memory replay window, which is typically 4-15 minutes
		depending on programming. Anything past 30 minutes is almost
		certainly a stuck-flag bug.

		Called from the once-per-second concurrentThread tick loop. Doesn't
		touch the panel — purely an in-plugin failsafe.
		"""
		if not self.repeatAlarmTripped:
			return
		if self.repeatAlarmTrippedSetAt <= 0:
			return
		held = time.time() - self.repeatAlarmTrippedSetAt
		if held < 30 * 60:  # 30 minutes
			return
		# Force-clear and replay any deferred zone closes so the plugin
		# becomes responsive to zone updates again.
		self._alwaysLog(
			f"repeatAlarmTripped watchdog: flag has been True for "
			f"{held:.0f}s (>30 min) which is way past any legitimate "
			f"alarm-memory replay window. The disarm cmd from the panel "
			f"apparently never arrived. Force-clearing the flag and "
			f"applying {len(self.closeTheseZonesList)} deferred zone-"
			f"close(s) so zone updates resume. The keypad's state may "
			f"still show 'tripped' until a real disarm or arm cycle "
			f"happens — this watchdog only frees up the zone-update "
			f"path, it doesn't fake a disarm event.",
			isError=True,
		)
		self._setRepeatAlarmTripped(False, "watchdog timeout (>30 min)")
		for zone in self.closeTheseZonesList:
			try:
				self.updateZoneState(zone, kZoneStateClosed, source="watchdog")
			except Exception as err:
				self.logger.error(f"watchdog: failed to apply deferred close for zone {zone}: {err}")
		self.closeTheseZonesList = []

	def _isHistoricalAlarmReplay(self, partition):
		"""Decide whether an incoming cmd 654 (partition in alarm) for
		`partition` is a fresh live trip or a replay from alarm memory of
		a trip that happened while the plugin was offline.

		Two conditions must BOTH be true for it to be classified as a
		replay:
		  1. We're still within the configurable startup grace period
		     (default 120s). Outside this window we assume the panel has
		     stopped replaying alarm-memory broadcasts.
		  2. The partition is currently reporting ready/disarmed. A real
		     active trip leaves the partition in notready/tripped state
		     with the bell sounding, so if we see Ready/disarmed it means
		     the alarm cycle has already finished.

		Returns True if both conditions hold, False otherwise.

		If the user has set the grace period to 0 in config, this always
		returns False — every cmd 654 is treated as a fresh live trip.
		"""
		if self.configStartupGracePeriodSeconds <= 0:
			return False
		secondsSinceStart = time.time() - self.pluginStartTime
		if secondsSinceStart > self.configStartupGracePeriodSeconds:
			return False
		# Check the partition's current state. If the keypad device has it
		# at ready/disarmed, this 654 is almost certainly a memory replay.
		try:
			dev = indigo.devices[self.keypadList[partition]]
			currentState = dev.states.get('state', '')
			currentReady = dev.states.get('ReadyState', '')
			# State 'disarmed' is the post-restart default, AND it's also
			# what cmd 650 / 750 / 655 set when the alarm is over. So if
			# we see disarmed AND ready=ready, we know the alarm isn't
			# active right now.
			if currentState == kAlarmStateDisarmed and currentReady == kReadyStateTrue:
				return True
			# Also catch the case where state is still at the default
			# (post-restart, before any cmd updates) and Ready hasn't
			# arrived yet — the partition will have state=disarmed but
			# Ready may be empty. Be cautious here: only treat as replay
			# if we're VERY early in the grace period (first 30s), to
			# avoid suppressing a real fresh trip that fires before the
			# initial 001-status response has flowed through.
			if currentState == kAlarmStateDisarmed and secondsSinceStart < 30:
				return True
		except Exception:
			# If we can't read the device state, default to NOT classifying
			# as historical — better to over-notify than to suppress a
			# real trip.
			return False
		return False

	def _handleHistoricalAlarmDetected(self, partition, partitionName):
		"""Handle a cmd 654 that's been classified as a historical-alarm
		replay (a trip that happened while the plugin was offline).

		Behaviour:
		  - Always log loudly to the event log (so the trip event is
		    visible even if notifications are off).
		  - Always add the partition to alarmAcknowledgedPartitions so
		    subsequent cmd 654 replays for this partition are silently
		    suppressed (instead of repeatedly firing this branch).
		  - Always set LEDMemory='on' so the keypad device state reflects
		    that alarm memory has a recorded trip.
		  - If configStartupAlertOnHistoricalAlarm is True: fire the
		    eventHistoricalAlarmDetected trigger AND send an email using
		    the urgent recipient with a clearly-labelled subject so the
		    user can tell historical alerts apart from live ones.

		Does NOT set repeatAlarmTripped or update the partition state to
		tripped — the alarm is already over, so we leave the state at
		whatever the panel is currently reporting (typically disarmed).
		"""
		self._alwaysLog(
			f"HISTORICAL ALARM DETECTED on partition {partition} "
			f"'{partitionName}' — the panel is broadcasting alarm-memory "
			f"data for a trip that happened while the plugin was offline. "
			f"The alarm is no longer active. Check the keypad's alarm "
			f"memory or the panel's event buffer for details on what "
			f"tripped and when.",
			isError=True,
		)
		# Mark acknowledged so subsequent replays from the same alarm
		# memory are silently suppressed.
		self.alarmAcknowledgedPartitions.add(partition)
		# Reflect that alarm memory has a recorded trip.
		self.updateKeypad(partition, 'LEDMemory', 'on')

		if not self.configStartupAlertOnHistoricalAlarm:
			self._alwaysLog(
				"  (Historical-alarm notifications are disabled in plugin "
				"config — not firing trigger or email.)"
			)
			return

		# Fire trigger.
		try:
			self.triggerEvent('eventHistoricalAlarmDetected')
		except Exception as err:
			self.logger.error(f"Failed to fire eventHistoricalAlarmDetected: {err}")

		# Send email if configured. Use the urgent recipient (same as live
		# alarm) but with a clearly-distinct subject and body so the user
		# can tell them apart.
		if self.configEmailUrgent:
			try:
				subject = "POSSIBLE HISTORICAL ALARM detected on startup"
				body = (
					f"The DSC Alarm plugin started up and the panel "
					f"reported that partition {partition} ('{partitionName}') "
					f"has alarm memory data — meaning the alarm tripped "
					f"at some point while the plugin was offline.\n\n"
					f"The alarm is NOT currently active. Check the keypad's "
					f"alarm memory or the panel's event buffer for details "
					f"on what tripped and when.\n\n"
					f"This notification is sent because "
					f"'Alert on detected historical alarm' is enabled in "
					f"the DSC Alarm plugin config. You can disable these "
					f"notifications there.\n\n"
					f"--\nDSC Alarm Plugin\n"
				)
				self.logger.info(
					f"Sending historical-alarm email to {self.configEmailUrgent}."
				)
				indigo.server.sendEmailTo(
					self.configEmailUrgent, subject=subject, body=body
				)
			except Exception as err:
				self.logger.error(f"Failed to send historical-alarm email: {err}")

	def _snapshotKeypadLCD(self):
		"""Read the current LCDLine1/LCDLine2 states from all alarm keypad
		devices and return a dict {partition: (line1, line2)}.

		This is the IT-100's only feedback channel for label programming —
		there's no physical display or beeping on an IT-100, so the LCD line
		states (populated from cmd 901 panel messages) are how we tell the
		user whether keypresses are landing. The user can also see this in
		real time via a control page mapped to the keypad device states.

		Returns empty dict if no keypad devices are configured. Best-effort —
		any individual lookup failure is silently skipped.
		"""
		out = {}
		for partition, keypadId in getattr(self, 'keypadList', {}).items():
			try:
				keypad = indigo.devices[keypadId]
				line1 = keypad.states.get('LCDLine1', '')
				line2 = keypad.states.get('LCDLine2', '')
				out[partition] = (line1, line2)
			except Exception:
				pass
		return out

	def _logKeypadLCD(self, prefix="LCD"):
		"""Log the current LCDLine1/LCDLine2 for each keypad, via _alwaysLog.

		Used as diagnostic output during label programming and the installer
		code test, so the user (who has no physical keypad to look at on an
		IT-100-only setup) can see what the panel is actually displaying."""
		snap = self._snapshotKeypadLCD()
		if not snap:
			self._alwaysLog(f"  {prefix}: (no keypad devices configured to read from)")
			return
		for partition, (l1, l2) in snap.items():
			# Strip trailing whitespace but show the lines exactly as the
			# panel transmitted them, so weird characters or padding are
			# obvious. Empty lines log as "(blank)" so they don't look like
			# a missing field.
			showL1 = l1.rstrip() if l1 and l1.strip() else "(blank)"
			showL2 = l2.rstrip() if l2 and l2.strip() else "(blank)"
			self._alwaysLog(f"  {prefix} (partition {partition}): {showL1!r} / {showL2!r}")

	def _watchKeypadLCD(self, duration_seconds, poll_interval=0.2, prefix="LCD"):
		"""Poll the keypad LCD state for `duration_seconds`, logging every
		distinct transition observed on any partition. Returns the final
		state dict.

		This is the right way to capture LCD feedback during keystroke
		sequences. The plugin's cmd-901 handler updates LCDLine1/LCDLine2 as
		fast as the panel transmits them (which can be several updates per
		second during programming), but a single before/after snapshot only
		catches whatever happened to be there at sample time and misses the
		interesting transitions in between. Polling and logging changes lets
		us reconstruct the panel's state machine from the log.

		Polls every poll_interval seconds (default 200ms — fast enough to
		catch most updates, slow enough not to spam the log). Only logs when
		the LCD actually changes; static periods produce no output.
		"""
		end_time = time.time() + duration_seconds
		last = self._snapshotKeypadLCD()
		# Log the starting state once so we have a t=0 baseline in the log.
		for partition, (l1, l2) in last.items():
			showL1 = l1.rstrip() if l1 and l1.strip() else "(blank)"
			showL2 = l2.rstrip() if l2 and l2.strip() else "(blank)"
			self._alwaysLog(f"  {prefix} t=0.0s (p{partition}): {showL1!r} / {showL2!r}")
		start = time.time()
		while time.time() < end_time:
			time.sleep(poll_interval)
			current = self._snapshotKeypadLCD()
			for partition, lines in current.items():
				if last.get(partition) != lines:
					l1, l2 = lines
					showL1 = l1.rstrip() if l1 and l1.strip() else "(blank)"
					showL2 = l2.rstrip() if l2 and l2.strip() else "(blank)"
					elapsed = time.time() - start
					self._alwaysLog(
						f"  {prefix} t={elapsed:.1f}s (p{partition}): "
						f"{showL1!r} / {showL2!r}"
					)
			last = current
		return last

	def _waitForLCDPrompt(self, expected_substrings, timeout_seconds,
	                      failure_substrings=None, poll_interval=0.2, prefix="LCD",
	                      check_cancel=True):
		"""Poll the keypad LCD state until one of four things happens:
		  - SUCCESS: any partition's LCDLine1/LCDLine2 contains an expected
		    substring (case-insensitive)
		      → returns ('success', matched_text)
		  - FAILURE: any partition shows a failure substring (e.g. "Invalid
		    Access Code"). Lets the caller fail fast rather than waiting for
		    the success match to time out.
		      → returns ('failure', matched_text)
		  - CANCELLED: self.bulkOperationCancelRequested became True during
		    the wait. Lets a long wait (e.g. 60s broadcast, 15s installer-
		    mode entry) be interrupted within one poll interval (~200ms)
		    instead of having to ride out the full timeout. Set
		    `check_cancel=False` for callers where cancel-interrupt isn't
		    safe (e.g. mid-pre-flight backout).
		      → returns ('cancelled', None)
		  - TIMEOUT: none of the above within timeout_seconds
		      → returns ('timeout', None)

		Logs the starting state and every distinct transition observed during
		the wait, so the user gets the full timeline in the event log. Static
		periods produce no log output.

		Substring matching is case-insensitive and works against the
		concatenation of LCDLine1 and LCDLine2 per partition.

		failure_substrings is optional — pass None or [] to disable explicit
		failure detection (the call then resolves as 'success', 'cancelled',
		or 'timeout').
		"""
		start = time.time()
		end_time = start + timeout_seconds
		# Lower-case substring lists for case-insensitive matching
		needles = [s.lower() for s in expected_substrings]
		fail_needles = [s.lower() for s in (failure_substrings or [])]

		def _checkMatch(snap, needle_list):
			"""Return matched LCD text if any partition shows any needle, else None."""
			for partition, (l1, l2) in snap.items():
				combined = f"{l1 or ''} {l2 or ''}".lower()
				for needle in needle_list:
					if needle in combined:
						return f"p{partition}: {l1!r} / {l2!r}"
			return None

		last = self._snapshotKeypadLCD()
		# Log the t=0 starting state for context
		for partition, (l1, l2) in last.items():
			showL1 = l1.rstrip() if l1 and l1.strip() else "(blank)"
			showL2 = l2.rstrip() if l2 and l2.strip() else "(blank)"
			self._alwaysLog(f"  {prefix} t=0.0s (p{partition}): {showL1!r} / {showL2!r}")

		# Check immediately in case the panel is already in either state
		matched = _checkMatch(last, needles)
		if matched is not None:
			self._alwaysLog(f"  {prefix}: matched at t=0.0s on {matched}")
			return ('success', matched)
		matched_fail = _checkMatch(last, fail_needles)
		if matched_fail is not None:
			self._alwaysLog(f"  {prefix}: failure pattern at t=0.0s on {matched_fail}")
			return ('failure', matched_fail)

		while time.time() < end_time:
			# Cancel-check FIRST so a pending cancel doesn't have to wait
			# for the poll_interval sleep + LCD snapshot. ~200ms max delay
			# from cancel-flag-set to this branch returning.
			if check_cancel and self.bulkOperationCancelRequested:
				elapsed = time.time() - start
				self._alwaysLog(
					f"  {prefix}: CANCEL honoured at t={elapsed:.1f}s "
					f"during LCD wait."
				)
				return ('cancelled', None)
			time.sleep(poll_interval)
			current = self._snapshotKeypadLCD()
			# Log any transitions
			for partition, lines in current.items():
				if last.get(partition) != lines:
					l1, l2 = lines
					showL1 = l1.rstrip() if l1 and l1.strip() else "(blank)"
					showL2 = l2.rstrip() if l2 and l2.strip() else "(blank)"
					elapsed = time.time() - start
					self._alwaysLog(
						f"  {prefix} t={elapsed:.1f}s (p{partition}): "
						f"{showL1!r} / {showL2!r}"
					)
			# Failure check first so a wrong-code rejection isn't masked by
			# stale "installer" text still on the LCD from an earlier stage.
			matched_fail = _checkMatch(current, fail_needles)
			if matched_fail is not None:
				elapsed = time.time() - start
				self._alwaysLog(
					f"  {prefix}: failure pattern at t={elapsed:.1f}s on {matched_fail}"
				)
				return ('failure', matched_fail)
			matched = _checkMatch(current, needles)
			if matched is not None:
				elapsed = time.time() - start
				self._alwaysLog(
					f"  {prefix}: matched at t={elapsed:.1f}s on {matched}"
				)
				return ('success', matched)
			last = current

		# Timed out
		elapsed = time.time() - start
		self._alwaysLog(
			f"  {prefix}: TIMEOUT after {elapsed:.1f}s waiting for any of "
			f"{expected_substrings}. Last LCD state logged above."
		)
		return ('timeout', None)

	def _emergencyBackoutIT100(self):
		"""Send enough # keystrokes to exit any depth of installer menu and
		return the panel to idle. Each # backs out one menu level — four is
		sufficient for any reasonable depth. Sending too many on an idle
		panel is harmless (# does nothing from the Ready state).
		"""
		for _ in range(4):
			self._sendKeystrokeToIT100('#')

	def _preFlightBackoutToIdle(self, opLabel, maxRounds=3, settleSeconds=1.5):
		"""Try to return the panel to its idle (Ready/Date) state by
		repeatedly sending ####. Used before any installer-mode-entering
		operation, so we never `*8` into installer mode when the panel
		is already in a menu state from a previous half-completed run.

		Each round: send ####, wait `settleSeconds`, snapshot the LCD.
		If any partition shows menu-state words ('section', 'enter lcd',
		'program', 'enter data'), do another round, up to `maxRounds`
		rounds total. Stop as soon as no partition shows menu-state words.

		Why multiple rounds: on a busy keybus the first #### can have
		keystrokes dropped or the panel can briefly be unresponsive. A
		second round usually clears it. We also can't tell the user to
		"press # on a physical keypad" because OTHER keypads are locked
		out while the IT-100 holds installer mode.

		Returns:
		  'idle'         — panel is back at idle, safe to proceed
		  'still_stuck'  — after maxRounds rounds, at least one partition
		                   is still in a menu state. Logs an abort
		                   message and the caller should bail out.

		Logs each round, the final state, and (on failure) a sensible
		recovery suggestion that doesn't tell the user to do something
		impossible.
		"""
		menuWords = ('section', 'enter lcd', 'program', 'enter data')
		lastStuck = None
		for roundNum in range(1, maxRounds + 1):
			self._alwaysLog(
				f"  pre-flight round {roundNum}/{maxRounds}: sending #### "
				f"to back out of any latent installer mode "
				f"(harmless if panel is at idle)."
			)
			self._emergencyBackoutIT100()
			time.sleep(settleSeconds)
			snap = self._snapshotKeypadLCD()
			stuck = []
			for partition, (l1, l2) in snap.items():
				combined = f"{l1 or ''} {l2 or ''}".lower()
				if any(w in combined for w in menuWords):
					stuck.append((partition, l1, l2))
			if not stuck:
				if roundNum > 1:
					self._alwaysLog(
						f"  pre-flight: cleared after round {roundNum}."
					)
				return 'idle'
			lastStuck = stuck
			# Brief extra pause before the next round — gives the panel
			# room to settle if it was mid-transition during the snapshot.
			if roundNum < maxRounds:
				self._alwaysLog(
					f"  pre-flight round {roundNum} still shows menu state on "
					f"{len(stuck)} partition(s); trying another round."
				)

		# Out of rounds and still stuck. Log the actual LCD content so the
		# user can see what's going on, and give recovery advice that does
		# NOT tell them to press # on a locked-out keypad.
		stuckDescription = "; ".join(
			f"p{p}: {l1!r} / {l2!r}" for (p, l1, l2) in (lastStuck or [])
		)
		self._alwaysLog(
			f"{opLabel}: ABORTED — after {maxRounds} rounds of #### the "
			f"panel is still in a menu state ({stuckDescription}). Other "
			f"keypads are locked out while the IT-100 holds installer "
			f"mode, so pressing # on a physical keypad WON'T work. "
			f"Options: (1) wait ~30 minutes for the panel's installer-"
			f"mode timeout to expire on its own, (2) power-cycle the "
			f"panel, or (3) disable+re-enable the IT-100 device in "
			f"Indigo to reset the connection. After that, try the action "
			f"again.",
			isError=True,
		)
		return 'still_stuck'

	def _lcdLooksLikeIdle(self, snapshot):
		"""Return True if at least one partition's LCD in the given snapshot
		shows idle-style content (Date / Time / Ready / Disarmed). Used to
		detect 'panel timed out waiting for our keystrokes and reverted to
		idle' as a soft-fail-and-retry condition during stage 1 of installer
		mode entry.
		"""
		for partition, (l1, l2) in snapshot.items():
			combined = f"{l1 or ''} {l2 or ''}".lower()
			if any(w in combined for w in ('date', 'ready', 'disarmed')):
				return True
		return False

	def _enterInstallerModeWithRetry(self, opLabel, sendEnterInstallerSegment, maxAttempts=4):
		"""Drive the standard 'enter installer mode' stage with up to N
		attempts, used by all three operations that need to get into
		installer mode (test installer code, zone label read, zone label
		write).

		The retry exists because we occasionally see the panel revert to
		idle during the *8+code keystrokes — likely a dropped keystroke
		(possibly on the wire to the IT-100, possibly the panel just
		timing out on incomplete input). A second attempt with a fresh
		pre-flight backout usually succeeds.

		Returns one of:
		  - 'success'                    — installer menu reached
		  - 'wrong_code'                 — panel rejected the installer code
		  - 'failed_after_retries'       — gave up after maxAttempts
		  - 'io_error'                   — IT-100 went away mid-sequence

		The caller is responsible for emergency-backout on any non-success
		return (the helper itself doesn't back out — different callers may
		want to do different things, e.g. send a clearer error message
		before backing out).

		Parameters:
		  opLabel        — descriptive operation name for log messages
		                   (e.g. 'Zone label read (Lounge Motion Sensor)')
		  sendEnterInstallerSegment
		                 — callable that sends the *8+code keystrokes via
		                   the caller's _sendKeystrokeToIT100 wrapping.
		                   Returns True on success, False on IT-100
		                   communication failure.
		  maxAttempts    — total attempts including the first. Default 4.
		"""
		for attempt in range(1, maxAttempts + 1):
			if attempt > 1:
				# Fresh pre-flight before retry. The panel may have left
				# the LCD on a transitional state; #### gets us back to a
				# known-idle starting point.
				self._alwaysLog(
					f"{opLabel}: stage 1 attempt {attempt - 1} didn't reach "
					f"the installer menu — retrying (attempt {attempt}/"
					f"{maxAttempts}) after a fresh pre-flight backout."
				)
				self._emergencyBackoutIT100()
				time.sleep(1.5)

			if not sendEnterInstallerSegment():
				return 'io_error'

			result, matched = self._waitForLCDPrompt(
				expected_substrings=['section', 'program'],
				failure_substrings=['invalid access', 'invalid code'],
				timeout_seconds=15,
				prefix=f"stage 1 (enter installer, attempt {attempt}/{maxAttempts})",
			)
			if result == 'success':
				return 'success'
			if result == 'failure':
				# Wrong installer code — no point retrying with the same code.
				self._alwaysLog(
					f"{opLabel}: stage 1 — installer code rejected. Panel "
					f"showed {matched}. Not retrying."
				)
				return 'wrong_code'

			# result == 'timeout'. If the LCD has reverted to idle, the
			# panel probably timed out waiting for a complete keystroke
			# sequence — likely a dropped keystroke. Worth retrying.
			snapshot = self._snapshotKeypadLCD()
			if self._lcdLooksLikeIdle(snapshot):
				self._alwaysLog(
					f"{opLabel}: stage 1 — panel reverted to idle. Likely "
					f"a dropped keystroke during *8+code transmission."
				)
				# Loop will retry (if attempts remain).
				continue
			else:
				# Timeout but NOT at idle — panel is in some unexpected
				# state. Retrying probably won't help. Bail.
				self._alwaysLog(
					f"{opLabel}: stage 1 — timeout without revert to idle. "
					f"Panel is in some unexpected state; not retrying."
				)
				return 'failed_after_retries'

		# Exhausted attempts
		return 'failed_after_retries'

	def _resolveCommandInterface(self, action):
		"""Read the 'commandInterface' picker from an action's props and
		return a force_interface argument suitable for sendCommand().

		Returns:
		    None        -> 'auto', let sendCommand use the configured default
		    'it100'     -> hard target the IT-100 (sendCommand will still fall
		                   back to Envisalink if IT-100 is down, which is what
		                   most users will want)
		    'envisalink' -> hard target Envisalink (same fallback behaviour)

		Tolerates older actions saved before the picker existed (no key in
		props) by defaulting to 'auto'.
		"""
		try:
			choice = (action.props.get('commandInterface') or 'auto').strip().lower()
		except Exception:
			choice = 'auto'
		if choice in ('it100', 'envisalink'):
			return choice
		return None  # auto

	######################################################################################
	def startup(self):
		self.logger.debug(f"Startup called. Log Levels are set to \"{kLogLevelList[int(self.logLevel/10)]}\".")
		self.configRead = self.getConfiguration(self.pluginPrefs)

		spacer = " " * 35
		environment_state = f"\n"
		environment_state += spacer + f"{'='*20}{' Initializing New Plugin Session '}{'='*54}\n"
		environment_state += spacer + f"{'Plugin name:':<20} {self.pluginDisplayName}\n"
		environment_state += spacer + f"{'Plugin version:':<20} {self.pluginVersion}\n"
		environment_state += spacer + f"{'Plugin ID:':<20} {self.pluginId}\n"
		environment_state += spacer + f"{'Plugin Log Level:':<20} {kLogLevelList[int(self.logLevel/10)]}\n"
		environment_state += spacer + f"{'Indigo version:':<20} {indigo.server.version}\n"
		environment_state += spacer + "{0:<20} {1}\n".format("Python version:", sys.version.replace('\n', ''))
		environment_state += spacer + f"{'Mac OS Version:':<20} {platform.mac_ver()[0]}\n"
		environment_state += spacer + f"{'Process ID:':<20} {os.getpid()}\n"
		environment_state += spacer + f"{'':{'='}^107}\n"
		self.logger.info(environment_state)
		

	def shutdown(self):
		self.logger.debug("Shutdown called")
		# Don't leave live-looking states behind while the plugin isn't running.
		for dev in indigo.devices.iter("self"):
			try:
				if dev.deviceTypeId == 'alarmZone':
					dev.updateStateOnServer(key='displayState', value='unavailable', clearErrorState=False)
				dev.setErrorStateOnServer("unavailable")
			except Exception as e:
				self.logger.debug(f"Shutdown: could not mark '{dev.name}' unavailable: {e}")


	######################################################################################
	# Indigo Device Start/Stop
	######################################################################################

	def deviceStartComm(self, dev):
		self.logger.threaddebug(f"<<-- entering deviceStartComm: {dev.name} ({dev.id} - {dev.deviceTypeId})")

		props = dev.pluginProps

		if dev.deviceTypeId == 'alarmZoneGroup':

			if dev.id not in self.zoneGroupList:
				self.zoneGroupList[dev.id] = props['devList']

			if dev.states['state'] == 0:
				dev.updateStateOnServer(key="state", value=kZoneGroupStateClosed)

			if 'AnyMemberLastChangedShort' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()

			if 'EntireGroupLastChangedShort' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()

		elif dev.deviceTypeId == 'alarmZone':
			if 'zoneNumber' not in props:
				return

			zone = int(props['zoneNumber'])
			if zone not in list(self.zoneList.keys()):
				self.zoneList[zone] = dev.id
			else:
				self.logger.error(f"Zone {zone} is already assigned to another device.")

			# Check for new version zone states.
			# If they're not present tell Indigo to reread the Devices.xml file
			if 'LastChangedShort' not in dev.states or 'alarmState' not in dev.states or 'displayState' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()
				dev = indigo.devices[dev.id]
			# Older versions put 'tripped' into the live state. The live state is now
			# only open/closed; convert, keeping the alarm involvement as memory.
			if dev.states.get('state') == kZoneStateTripped:
				dev.updateStateOnServer(key='state', value=kZoneStateOpen)
				dev.updateStateOnServer(key='alarmState', value=kAlarmStateMemory)
				dev = indigo.devices[dev.id]

			# If state is invalid or not there, set to closed
			if dev.states['state'] == 0:
				dev.updateStateOnServer(key='state', value=kZoneStateClosed)

			# If plugin is v2.x we won't have a bypass key so set it to default
			if 'bypass' not in dev.states or dev.states['bypass'] == 0:
				dev.stateListOrDisplayStateIdChanged()
				dev.updateStateOnServer(key='bypass', value=kZoneBypassNo)

			# If plugin is v2.x we won't have a zonePartition key so set it to 1 by default
			if 'zonePartition' not in props:
				props.update({'zonePartition': '1'})
				dev.replacePluginPropsOnServer(props)

			dev.updateStateOnServer(key="LastChangedShort", value=self.getShortTime(dev.states["LastChangedTimer"]))

			# Check for latchSeconds property and set default if missing
			if 'latchSeconds' not in props:
				self.logger.debug(f"Adding latchSeconds to device {dev.name} properties.")
				props.update({"latchSeconds": "0"})
				dev.replacePluginPropsOnServer(props)

			# Check for sensor states and initialize onOffState
			if 'onOffState' not in dev.states:
				self.logger.debug(f"Adding sensor states to device {dev.name}.")
				dev.stateListOrDisplayStateIdChanged()
			
			# Initialize latchTimer state if missing
			if 'latchTimer' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()
				dev.updateStateOnServer(key='latchTimer', value=0)
			
			# For sensor type devices Indigo uses onOffState for the State column display
			# Use uiValue on onOffState to show custom text instead of on/off
			zoneType     = dev.pluginProps.get('zoneType', 'zoneTypeOther')
			currentState = dev.states['state']
			# Detect if zone is currently active based on stored raw state
			inactiveRaw  = (kZoneStateClosed, '')
			isActive     = currentState not in inactiveRaw
			displayText  = self._getDisplayText(zoneType, isActive=isActive)
			dev.updateStateOnServer(key='onOffState',   value=isActive, uiValue=displayText)
			dev.updateStateOnServer(key='displayText', value=displayText)
			# Mirror onOffState into the plugin's custom onState/offState
			# bool states. These have been part of the device's state list
			# for a long time (used by some users in triggers) but weren't
			# being written by any code path — they just sat at their
			# default False forever. Fixed here and at every other site
			# that updates onOffState so they accurately mirror activity.
			dev.updateStateOnServer(key='onState',  value=isActive)
			dev.updateStateOnServer(key='offState', value=not isActive)

			# Set sensor subType so Indigo uses correct native icon set for this zone type
			self._setZoneSubType(dev, zoneType)
			# Text, icon and displayState from live state + alarm state + availability
			self._refreshZoneDisplay(dev)

			# Check for new version properties to see if we need to refresh the device
			if 'occupancyGroup' not in props:
				self.logger.debug(f"Adding occupancyGroup to device {dev.name} properties.")
				props.update({"occupancyGroup": 0})
				dev.replacePluginPropsOnServer(props)

			# If the variable we used no longer exists then remove the varID
			if "var" in props:
				if props["var"] not in indigo.variables:
					props["var"] = None
					dev.replacePluginPropsOnServer(props)
			else:
				props["var"] = None
				dev.replacePluginPropsOnServer(props)


		elif dev.deviceTypeId == 'alarmKeypad':
			if 'alarmMemoryZones' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()
			partition = int(dev.pluginProps['partitionNumber'])
			if partition not in list(self.keypadList.keys()):
				self.keypadList[partition] = dev.id
				self.logger.debug(f"Adding keypad: {self.keypadList}")
			else:
				self.logger.error("Partition is already assigned to another device.")

			# If plugin is very old version v2.x we won't have a partitionName key so set it to "Default"
			if 'partitionName' not in dev.pluginProps:
				props['partitionName'] = 'Default'
				dev.replacePluginPropsOnServer(props)

			dev.updateStateOnServer(key='state', value=kAlarmStateDisarmed)
			dev.updateStateOnServer(key='ReadyState', value=kReadyStateTrue)
			dev.updateStateOnServer(key='PanicState', value=kPanicStateNone)
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)   # green circle
				#dev.updateStateImageOnServer(indigo.kStateImageSel.Unlocked)  # red open padlock
			else:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

			# Check for new keypad states.
			# If they're not present tell Indigo to reread the Devices.xml file
			if 'ArmedState' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()
			elif 'ReadyState' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()
			elif 'PanicState' not in dev.states:
				dev.stateListOrDisplayStateIdChanged()
			elif "armedAway" not in dev.states['state']:
				dev.stateListOrDisplayStateIdChanged()
			elif "armedStay" not in dev.states['state']:
				dev.stateListOrDisplayStateIdChanged()


		elif dev.deviceTypeId == 'alarmTemp':
			sensor = int(dev.pluginProps['sensorNumber'])
			if sensor not in list(self.tempList.keys()):
				self.tempList[sensor] = dev

		# Until the panel has confirmed states, every device shows as unavailable.
		if self.devicesUnavailable:
			try:
				dev.setErrorStateOnServer("unavailable")
			except Exception as e:
				self.logger.debug(f"Could not mark '{dev.name}' unavailable: {e}")

		self.logger.threaddebug("exiting deviceStartComm -->>")


	def deviceStopComm(self, dev):
		self.logger.threaddebug(f"<<-- entering deviceStopComm: {dev.name} ({dev.id} - {dev.deviceTypeId})")

		if dev.deviceTypeId == 'alarmZoneGroup':
			if dev.id in self.zoneGroupList:
				del self.zoneGroupList[dev.id]

		elif dev.deviceTypeId == 'alarmZone':
			if 'zoneNumber' in dev.pluginProps:
				zone = int(dev.pluginProps['zoneNumber'])
				if zone in list(self.zoneList.keys()):
					del self.zoneList[zone]
				#self.logger.debug(f"ZoneList is now: {self.zoneList}")

		elif dev.deviceTypeId == 'alarmKeypad':
			if 'partitionNumber' in dev.pluginProps:
				keyp = int(dev.pluginProps['partitionNumber'])
				if keyp in self.keypadList:
					del self.keypadList[keyp]

		elif dev.deviceTypeId == 'alarmTemp':
			if 'sensorNumber' in dev.pluginProps:
				tmp = int(dev.pluginProps['sensorNumber'])
				if tmp in self.tempList:
					del self.tempList[int(dev.pluginProps['sensorNumber'])]

		self.logger.threaddebug("exiting deviceStopComm -->>")


	######################################################################################
	# Indigo Trigger Start/Stop
	######################################################################################

	def triggerStartProcessing(self, trigger):
		self.logger.threaddebug(f"<<-- entering triggerStartProcessing: {trigger.name} ({trigger.id})")
		self.triggerList.append(trigger.id)
		self.logger.threaddebug("exiting triggerStartProcessing -->>")

	def triggerStopProcessing(self, trigger):
		self.logger.threaddebug(f"<<-- entering triggerStopProcessing: {trigger.name} ({trigger.id})")
		if trigger.id in self.triggerList:
			self.logger.threaddebug("TRIGGER FOUND")
			self.triggerList.remove(trigger.id)
		self.logger.threaddebug("exiting triggerStopProcessing -->>")


	######################################################################################
	# Indigo Trigger Firing
	######################################################################################

	######################################################################################
	# Connection health: "unavailable" marking and resync after reconnect
	######################################################################################
	RESYNC_SECONDS = 15

	def _anyInterfaceConnected(self):
		return bool(self.it100Connected or self.envisalinkConnected)

	def _checkConnectionHealth(self):
		"""Called every main-loop pass. Marks all plugin devices unavailable once no
		interface has been connected for the grace period, and finishes a resync
		once its window has elapsed."""
		now = time.time()
		if self._anyInterfaceConnected():
			self.connLostAt = None
		else:
			if self.connLostAt is None:
				self.connLostAt = now
			elif not self.devicesUnavailable and now - self.connLostAt >= self.configUnavailableGrace:
				self._markAllUnavailable()
		if self.resyncDeadline and now >= self.resyncDeadline:
			self._finishResync()

	def _markAllUnavailable(self):
		count = 0
		for dev in indigo.devices.iter("self"):
			if dev.enabled:
				try:
					dev.setErrorStateOnServer("unavailable")
					count += 1
				except Exception as e:
					self.logger.debug(f"Could not mark '{dev.name}' unavailable: {e}")
		self.devicesUnavailable = True
		for zoneNum, devId in list(self.zoneList.items()):
			try:
				self._refreshZoneDisplay(indigo.devices[devId])
			except Exception:
				pass
		self.logger.warning(
			f"No connection to the alarm panel for {self.configUnavailableGrace}s — "
			f"{count} device(s) marked unavailable. Last known states are kept until the panel reports again.")
		self.triggerEvent("eventConnectionLost")

	def _beginResyncCollection(self):
		"""Start recording which zones the panel reports open (cmd 609)."""
		self.resyncOpenZones = set()
		self.resyncCollecting = True

	def _startResync(self):
		"""Connection succeeded: finish the resync after the status report has had time to arrive."""
		if not self.resyncCollecting:
			self._beginResyncCollection()
		self.resyncDeadline = time.time() + self.RESYNC_SECONDS
		self.logger.debug(f"Resync started: collecting zone reports for {self.RESYNC_SECONDS}s")

	def _finishResync(self):
		self.resyncDeadline = 0
		self.resyncCollecting = False
		if not self._anyInterfaceConnected():
			return  # dropped again during the report; stay unavailable
		# The status report lists every open zone. Any zone Indigo still shows as open
		# that the panel did not report must have closed while we were not listening.
		closed = []
		for zoneNum, devId in list(self.zoneList.items()):
			try:
				dev = indigo.devices[devId]
				if dev.states.get('state') == kZoneStateOpen and zoneNum not in self.resyncOpenZones:
					self.updateZoneState(zoneNum, kZoneStateClosed)
					closed.append(dev.name)
			except Exception as e:
				self.logger.debug(f"Resync: could not check zone {zoneNum}: {e}")
		if closed:
			self.logger.info(f"Resync: {len(closed)} zone(s) closed while disconnected: {', '.join(closed)}")
		wasUnavailable = self.devicesUnavailable
		self.devicesUnavailable = False
		for zoneNum, devId in list(self.zoneList.items()):
			try:
				self._refreshZoneDisplay(indigo.devices[devId])
			except Exception as e:
				self.logger.debug(f"Resync: could not refresh zone {zoneNum}: {e}")
		cleared = 0
		for dev in indigo.devices.iter("self"):
			if dev.errorState:
				try:
					dev.setErrorStateOnServer(None)
					cleared += 1
				except Exception as e:
					self.logger.debug(f"Could not clear error state on '{dev.name}': {e}")
		if wasUnavailable:
			total = sum(1 for d in indigo.devices.iter("self") if d.enabled)
			if self.startupUnconfirmed:
				self.startupUnconfirmed = False
				self.logger.info(f"Alarm panel states confirmed — all {total} device(s) now available.")
			else:
				self.logger.info(f"Alarm panel connection restored — all {total} device(s) available again.")
				self.triggerEvent("eventConnectionRestored")

	def triggerEvent(self, eventId):
		self.logger.threaddebug(f"<<-- entering triggerEvent: {eventId} ")
		for trigId in self.triggerList:
			trigger = indigo.triggers[trigId]
			if trigger.pluginTypeId == eventId:
				indigo.trigger.execute(trigger)
		return


	######################################################################################
	# Indigo Action Methods
	######################################################################################
	#These are partition specific commands, except global and panic alarms.

	def methodDisarmAlarm(self, action, dev):
		keypname = str(dev.pluginProps['partitionName'])
		keyp = dev.pluginProps["partitionNumber"]
		self.logger.info(f"Disarming Alarm. (Partition {keyp} '{keypname}')")
		#tx = f"040{keyp}{self.pluginPrefs['code']:0<6}"
		tx = f"040{keyp}{self.pluginPrefs['code']}"
		self.sendCommand(tx, force_interface=self._resolveCommandInterface(action))


	def methodArmStay(self, action, dev):
		keypname = str(dev.pluginProps['partitionName'])
		keyp = dev.pluginProps["partitionNumber"]
		self.logger.info(f"Arming Alarm in Stay Mode. (Partition {keyp} '{keypname}')")
		self.sendCommand('031' + keyp, force_interface=self._resolveCommandInterface(action))


	def methodArmAway(self, action, dev):
		keypname = str(dev.pluginProps['partitionName'])
		keyp = dev.pluginProps["partitionNumber"]
		self.logger.info(f"Arming Alarm in Away Mode. (Partition {keyp} '{keypname}')")
		self.sendCommand('030' + keyp, force_interface=self._resolveCommandInterface(action))


	def methodArmStayForce(self, action, dev):
		keypname = str(dev.pluginProps['partitionName'])
		keypname = f" '{keypname}'"
		keyp = dev.pluginProps["partitionNumber"]
		keypstate = str(dev.states['state'])
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		if keypstate in (kAlarmStateArmedStay, kAlarmStateArmedAway):
			self.logger.warning("The Selected Partition is Already Armed.")
			return
		tx = f"071{keyp}*1" #starts bypass mode
		self.sendCommand(tx, force_interface='envisalink')
		self.sleep(1.25) #increase delay if keybus buffer overruns			
		if keyp != "1": #this sets all zones to nobypass if they are in partition 2-8, since those partitions do not report zone bypass status
			tx = f"071{keyp}00"  #cancels all zone bypass for this partition
			self.logger.debug("We have partition 2-8 and will cancel all bypass")
			self.sendCommand(tx, force_interface='envisalink')
			self.sleep(1.25) #increase delay if keybus buffer overruns
		for zoneNum in self.zoneList.keys():
			zone = indigo.devices[self.zoneList[zoneNum]]
			zonePartition = zone.pluginProps['zonePartition']
			if zone.states['state.open'] is True and zone.states['bypass'] == kZoneBypassNo and zonePartition == keyp:
				self.logger.info(f"Bypassing Zone '{zone.name}' in Partition {keyp}{keypname}.")
				zoneNum = str(zoneNum).zfill(2)
				tx = f"071{keyp}{zoneNum}"
				self.sendCommand(tx, force_interface='envisalink')
				self.sleep(1.25) #increase delay if keybus buffer overruns
		tx = f"071{keyp}1#" #ends bypass mode
		self.sendCommand(tx, force_interface='envisalink')
		self.sleep(1.5) #increase delay if keybus buffer overruns				
		self.logger.info(f"Arming Alarm in Forced Stay Mode. (Partition {keyp}{keypname})")
		self.sendCommand('031' + keyp, force_interface='envisalink')


	def methodArmAwayForce(self, action, dev):
		keypname = str(dev.pluginProps['partitionName'])
		keypname = " '{keypname}'"
		keyp = dev.pluginProps["partitionNumber"]
		keypstate = str(dev.states['state'])
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		if keypstate in (kAlarmStateArmedStay, kAlarmStateArmedAway):
			self.logger.warning("The Selected Partition is Already Armed.")
			return
		tx = f"071{keyp}*1" #starts bypass mode
		self.sendCommand(tx, force_interface='envisalink')
		self.sleep(1.25) #increase delay if keybus buffer overruns			
		if keyp != "1": #this sets all zones to nobypass if they are in partition 2-8, since those partitions do not report zone bypass status
			tx = f"071{keyp}00"  #cancels all zone bypass for this partition
			self.logger.debug("We have partition 2-8 and will cancel all bypass")
			self.sendCommand(tx, force_interface='envisalink')
			self.sleep(1.25) #increase delay if keybus buffer overruns
		for zoneNum in self.zoneList.keys():
			zone = indigo.devices[self.zoneList[zoneNum]]
			zonePartition = zone.pluginProps['zonePartition']
			if zone.states['state.open'] is True and zone.states['bypass'] == kZoneBypassNo and zonePartition == keyp:
				self.logger.info(f"Bypassing Zone '{zone.name}' in Partition {keyp}{keypname}.")
				zoneNum = str(zoneNum).zfill(2)
				tx = f"071{keyp}{zoneNum}"
				self.sendCommand(tx, force_interface='envisalink')
				self.sleep(1.25) #increase delay if keybus buffer overruns
		tx = f"071{keyp}1#" #ends bypass mode
		self.sendCommand(tx, force_interface='envisalink')
		self.sleep(1.5) #increase delay if keybus buffer overruns				
		self.logger.info(f"Arming Alarm in Forced Away Mode. (Partition {keyp}{keypname})")
		self.sendCommand('030' + keyp, force_interface='envisalink')


	def methodArmGlobal(self, action):
		#this action arms all defined partitions in away mode.
		self.logger.info("Arming Alarm in Global Mode (All Partitions).")
		target = self._resolveCommandInterface(action)
		for i in range(1, 9):
			if i in list(self.keypadList.keys()):
				key = str(i)
				self.sendCommand('030' + key, force_interface=target)
				self.sleep(1)


	def methodPanicAlarm(self, action):
		panicType = action.props['panicAlarmType']
		self.logger.info(f"Activating Panic Alarm! ({kPanicTypeList[int(panicType)]})")
		self.sendCommand('060' + panicType, force_interface=self._resolveCommandInterface(action))


	def methodSendKeypress070(self, action):
		"""Send a keystroke (or sequence) to the panel via IT-100 TPI
		command 070. IT-100 only — 070 is not valid on Envisalink (use the
		separate 'Send Multiple Keypresses' / 071 action for that).

		The user-supplied 'keys' string can be either raw chars matching the
		IT-100 alphabet (e.g. '*2' for trouble check) or bracketed tokens
		(e.g. '[STAR]2' or '[F2]236[RIGHT]') — see _parseKeypressString for
		the full grammar. Whitespace between keys is ignored. Any invalid
		input is rejected before any keystroke is sent.

		The token 'L' (or '[WAIT]') is a pause marker that sleeps 2 seconds
		without sending a key. All other tokens map to a single IT-100 070
		keypress packet followed by a 070^ break-key packet.
		"""
		self.logger.debug("Received Send Keypress 070 Action")
		if not self.enableIT100 or not self.it100Connected:
			self.logger.error(
				"Send Single Keypress (070) requires an active IT-100 connection. "
				"Command 070 is not supported on Envisalink — use 'Send Multiple "
				"Keypresses' (071) instead for Envisalink-based keystrokes."
			)
			return

		rawKeys = action.props.get('keys', '')
		if not rawKeys.strip():
			self.logger.warning("Send Keypress: 'Key To Press' field is empty.")
			return

		parsedKeys, parseErrors = self._parseKeypressString(rawKeys)
		if parseErrors:
			# Refuse to send a partial sequence — log all errors so the user
			# can fix everything at once rather than discovering issues one
			# at a time.
			self.logger.error(
				f"Send Keypress: input {rawKeys!r} has {len(parseErrors)} "
				f"error(s); not sending anything:"
			)
			for err in parseErrors:
				self.logger.error(f"  - {err}")
			return

		if not parsedKeys:
			self.logger.warning("Send Keypress: parsed input produced no keystrokes.")
			return

		self.logger.info(f"Send Keypress: queueing {parsedKeys} via IT-100")

		# Queue each key as (070X, 070^) pairs, with 'L' acting as a 2-second
		# pause rather than a keystroke. We do NOT emit a break key around
		# pauses — break keys go between consecutive keypresses, and a pause
		# is not a keypress.
		for k in parsedKeys:
			if k == 'L':
				self.sleep(2)
			else:
				if not self._sendKeystrokeToIT100(k):
					self.logger.error(
						"Send Keypress: IT-100 connection went away mid-sequence; "
						"some keystrokes may not have been sent."
					)
					return


	def methodSendKeypress071(self, action, dev):
		keypname = f" '{str(dev.pluginProps['partitionName'])}' "
		keyp = dev.pluginProps["partitionNumber"]
		keys = action.props['keys']
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		self.logger.info(f"Received Send Keypress Action [{keys}]. (Partition {keyp}{keypname})")
		cleanKeys = re.sub(r'[^a-e0-9LFAP<>=*#]+', '', keys)
		if len(keys) != len(cleanKeys):
			self.logger.warning("There are Invalid Keys in your Command.")
			return
		if len(keys) > 6:
			self.logger.warning("The Key Command is too long.")
			return
		tx = f"071{keyp}{keys}"
		self.sendCommand(tx, force_interface='envisalink')


	def methodSendKeypressVariable(self, action):
		keys = indigo.variables["DSC_Command"]
		keys = keys.value
		self.logger.info(f"Received Send Keypress Action [{keys}].")
		cleanKeys = re.sub(r'[^a-e0-9LFAP<>=*#]+', '', keys)
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		if len(keys) != len(cleanKeys):
			self.logger.warning("There are Invalid Keys in your DSCcommand Variable.")
			return
		if len(keys) > 7:
			self.logger.warning("The Key Command is too long.")
			return
		if keys[0] < "0" or keys[0] > "8":
			self.logger.warning("The First Character in your DSCcommand Needs to be a Valid Partition (1-8).")
		else:
			self.sendCommand('071' + keys, force_interface='envisalink')


	######################################################################################
	# IT-100 Virtual Keypad Zone Label Programming (experimental)
	######################################################################################
	#
	# These methods let the user push an Indigo device name to the corresponding
	# DSC keypad's zone label by emulating the physical keypress sequence
	# documented in the PK5500 installation guide. The user enters:
	#
	#     *8 <installer code>     enter installer programming mode
	#     001..064                three-digit section number for zone 1..64
	#     <character entry>       per-letter key-cycling with > arrows between
	#     *                       open the menu after the last char
	#     *                       select "Save" (which is the first menu item)
	#     ##                      exit section, exit installer mode
	#
	# Each char is "type the number key N times, then press > to advance the
	# cursor" — exactly what a human does on the physical keypad. We translate
	# the Indigo device name (truncated to 14 chars, uppercased, non-mappable
	# chars replaced with space) into that sequence.
	#
	# IT-100 ONLY. The Envisalink TPI explicitly locks out keystrokes 070/071
	# once the panel is in installer mode (error 017 'Keybus Busy - Installers
	# Mode'), so attempting this over Envisalink can dead-lock the panel with
	# no escape but a power cycle. This is confirmed by the Envisalink
	# developer on the EyezOn forum. We hard-refuse the Envisalink path.

	# Each char maps to (keypad_digit, number_of_presses_to_cycle_to_it). Per
	# the PK5500 manual: key 1 = A B C 1, key 2 = D E F 2, key 3 = G H I 3,
	# key 4 = J K L 4, key 5 = M N O 5, key 6 = P Q R 6, key 7 = S T U 7,
	# key 8 = V W X 8, key 9 = Y Z 9 0, key 0 = Space.
	#
	# We store uppercase mappings; lowercase letters use the same digit/press
	# count but require the keypad to be in lowercase mode (toggled via the
	# *-menu's "Change Case" entry). The case-tracking logic is in
	# _labelStringToKeystrokes.
	LABEL_CHAR_MAP = {
		'A': ('1', 1), 'B': ('1', 2), 'C': ('1', 3), '1': ('1', 4),
		'D': ('2', 1), 'E': ('2', 2), 'F': ('2', 3), '2': ('2', 4),
		'G': ('3', 1), 'H': ('3', 2), 'I': ('3', 3), '3': ('3', 4),
		'J': ('4', 1), 'K': ('4', 2), 'L': ('4', 3), '4': ('4', 4),
		'M': ('5', 1), 'N': ('5', 2), 'O': ('5', 3), '5': ('5', 4),
		'P': ('6', 1), 'Q': ('6', 2), 'R': ('6', 3), '6': ('6', 4),
		'S': ('7', 1), 'T': ('7', 2), 'U': ('7', 3), '7': ('7', 4),
		'V': ('8', 1), 'W': ('8', 2), 'X': ('8', 3), '8': ('8', 4),
		'Y': ('9', 1), 'Z': ('9', 2), '9': ('9', 3), '0': ('9', 4),
		' ': ('0', 1),
	}
	LABEL_MAX_CHARS = 14  # one line of the PK5500 LCD

	# Case-toggle keystroke sequence — opens the *-menu (asterisk), advances
	# one menu position with > to reach "Change Case" (Save is the first item,
	# Change Case is the second), and selects it with another asterisk. The
	# panel applies the toggle and returns to the editing cursor.
	CASE_TOGGLE_KEYS = ['*', '>', '*']

	def _labelStringToKeystrokes(self, label):
		"""Convert a human-readable label string into the IT-100 single-key
		sequence (suitable for TPI command 070, one character at a time)
		needed to type it on the keypad. Returns (keystrokes, normalised,
		preview).

		See `_labelStringToPerCharSequences` for a version that breaks the
		output up per-character (used by the per-character-verification
		path in the write flow). This flat-keystroke version is kept for
		the dry-run preview and any other callers that just want the raw
		key list.

		Behaviour:
		- Preserves case where possible (letters A-Z stay upper, a-z stay
		  lower; case is toggled via the *-menu's "Change Case" entry when
		  needed)
		- Replaces unmappable characters with spaces, then collapses
		  consecutive spaces to one
		- Truncates the result to 14 chars (one LCD line)
		- Pads to exactly 14 chars with trailing spaces so the entire LCD
		  line is overwritten — otherwise the existing label's trailing chars
		  would leak through (e.g. writing "Hi" over "Zone 1" would leave
		  "Hine 1")
		- Inserts case-toggle keystrokes when the case mode needs to change
		- Emits '>' after each char to advance the cursor, EXCEPT after the
		  last char (cursor doesn't need to move past the end)

		The keypad's starting case mode is UPPERCASE — that's the default
		when entering a new label section.
		"""
		# Normalise: preserve A-Z and a-z, keep digits and space, replace
		# anything else with a space. Don't uppercase or lowercase here — we
		# want to preserve the case the user typed so they can write "Front
		# Door" with capitals but "the cat" all-lowercase if they want.
		normalised = ''
		for c in label:
			upper = c.upper()
			if c in 'abcdefghijklmnopqrstuvwxyz':
				normalised += c  # keep lowercase letter as-is
			elif upper in self.LABEL_CHAR_MAP:
				normalised += upper  # uppercase letter, digit, or space
			else:
				normalised += ' '  # unmappable — fall back to space
		# Collapse runs of spaces to single spaces, then trim, then truncate.
		normalised = ' '.join(normalised.split())
		normalised = normalised[:self.LABEL_MAX_CHARS]
		# PAD to full LCD width with trailing spaces, so we overwrite any
		# leftover characters from the previous label. Without this, writing
		# "Hi" over "Zone 1" would leave "Hine 1" on the LCD because we only
		# touch positions 0 and 1.
		padded = normalised.ljust(self.LABEL_MAX_CHARS)
		preview = padded  # exactly what will appear on the LCD

		keys = []
		caseMode = 'upper'  # keypad starts in uppercase mode for a fresh label
		for i, c in enumerate(padded):
			# Determine if we need to toggle case for this character. Digits
			# and spaces are case-agnostic — emit them without changing mode.
			if c in 'abcdefghijklmnopqrstuvwxyz' and caseMode != 'lower':
				keys.extend(self.CASE_TOGGLE_KEYS)
				caseMode = 'lower'
			elif c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' and caseMode != 'upper':
				keys.extend(self.CASE_TOGGLE_KEYS)
				caseMode = 'upper'

			# Look up the digit and press-count using the uppercase map
			# (lowercase letters use the same digit cycle, just in lowercase
			# mode after the toggle).
			lookup = c.upper() if c in 'abcdefghijklmnopqrstuvwxyz' else c
			digit, presses = self.LABEL_CHAR_MAP[lookup]
			keys.extend([digit] * presses)
			if i < len(padded) - 1:
				keys.append('>')
		return keys, normalised, preview

	def _labelStringToPerCharSequences(self, label):
		"""Like `_labelStringToKeystrokes`, but breaks the output into
		per-character entries so each character can be sent and verified
		individually by the write flow.

		Returns (charSequences, normalised, preview) where charSequences
		is a list of dicts, one per character position (always exactly
		14 entries since the label is padded to LABEL_MAX_CHARS):

		    {
		      'expected': str,           # the character that should appear at this position
		      'caseToggle': list[str],   # keystrokes to send before the digit presses
		                                 # (empty list if no case change needed)
		      'digitPresses': list[str], # keystrokes for the digit presses themselves
		    }

		The CURSOR ADVANCE (`>`) is NOT included in any of these entries —
		the caller decides when/if to advance the cursor based on whether
		the character verified correctly.

		Note: the caseToggle field is set based on the case state that
		would be in effect at the START of typing this character, assuming
		all previous characters were typed successfully. If a retry/recovery
		happens that changes the case mode, the caller needs to re-derive
		caseToggle for any subsequent characters.
		"""
		# Re-use normalisation from the flat version.
		normalised = ''
		for c in label:
			upper = c.upper()
			if c in 'abcdefghijklmnopqrstuvwxyz':
				normalised += c
			elif upper in self.LABEL_CHAR_MAP:
				normalised += upper
			else:
				normalised += ' '
		normalised = ' '.join(normalised.split())
		normalised = normalised[:self.LABEL_MAX_CHARS]
		padded = normalised.ljust(self.LABEL_MAX_CHARS)
		preview = padded

		charSequences = []
		caseMode = 'upper'  # keypad starts in uppercase mode for a fresh label
		for c in padded:
			caseToggle = []
			if c in 'abcdefghijklmnopqrstuvwxyz' and caseMode != 'lower':
				caseToggle = list(self.CASE_TOGGLE_KEYS)
				caseMode = 'lower'
			elif c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' and caseMode != 'upper':
				caseToggle = list(self.CASE_TOGGLE_KEYS)
				caseMode = 'upper'

			lookup = c.upper() if c in 'abcdefghijklmnopqrstuvwxyz' else c
			digit, presses = self.LABEL_CHAR_MAP[lookup]
			digitPresses = [digit] * presses

			charSequences.append({
				'expected':     c,
				'caseToggle':   caseToggle,
				'digitPresses': digitPresses,
			})

		return charSequences, normalised, preview

	def _buildZoneLabelProgrammingSequence(self, zoneNumber, installerCode, label):
		"""Build the keystroke sequence to program one zone label, broken
		into named segments so the caller can drive a state machine (send
		segment → wait for the expected LCD prompt → send next segment).

		Returns (segments, normalised_label, preview_lines).

		'segments' is a list of (name, keys) tuples in execution order:
		  ('enter_installer', ['*', '8', '8', '8', '3', '6', '8', '8'])
		  ('select_lcd_menu', ['*'])
		  ('section', ['0', '0', '1'])
		  ('label_text', [...key cycles for each char...])
		  ('save', ['*', '*'])
		  ('exit_section', ['#'])
		  ('exit_installer', ['#'])

		Per the PK5500 Installation Manual, "Programming Labels":
		  Step 1: [*][8][Installer Code][*]    enter LCD label programming
		  Step 2: [NNN]                         3-digit section for the zone
		  Step 3: type the label (key cycles + > to advance the cursor)
		  Step 4: [*] to open the menu, then [*] again to Save
		  Step 5: [#] to exit section, [#] again to exit installer mode

		CRITICAL: the `[*]` in step 1 AFTER the installer code is what drops
		you into LCD label programming. Without it the section number that
		follows is interpreted as a top-level installer section — and
		section 001 is Zone Definitions / data programming. Skipping that *
		is what caused v14 to overwrite zone 1's definition byte.
		"""
		section = f"{zoneNumber:03d}"
		labelKeys, normalised, preview = self._labelStringToKeystrokes(label)

		segments = [
			('enter_installer', ['*', '8'] + list(installerCode)),
			('select_lcd_menu', ['*']),
			('section',         list(section)),
			('label_text',      labelKeys),
			('save',            ['*', '*']),
			('exit_section',    ['#']),
			('exit_installer',  ['#']),
		]
		return segments, normalised, preview

	def _parseZoneRangeSpec(self, spec):
		"""Parse a user-supplied zone-range string into a sorted list of
		unique zone numbers.

		Accepts:
		  - single zone numbers: '5'
		  - ranges: '5-10' (inclusive both ends)
		  - comma-separated combinations: '1,3,5-10,15'
		  - whitespace anywhere is tolerated: ' 1 , 3, 5 - 10 , 15 '

		Returns (zones, errors) where zones is a sorted list of unique
		ints in 1-64 and errors is a list of human-readable strings
		describing problems with the input. If errors is non-empty,
		zones contains everything that parsed successfully — the caller
		can choose to proceed with the valid subset or abort.
		"""
		zones = set()
		errors = []
		if not spec or not spec.strip():
			errors.append("Zone list is empty.")
			return [], errors

		parts = [p.strip() for p in spec.split(',') if p.strip()]
		for part in parts:
			if '-' in part:
				# Range. Split on first '-' to handle weird cases.
				sub = part.split('-', 1)
				if len(sub) != 2 or not sub[0].strip() or not sub[1].strip():
					errors.append(f"Bad range {part!r} — expected 'N-M'.")
					continue
				try:
					lo = int(sub[0].strip())
					hi = int(sub[1].strip())
				except ValueError:
					errors.append(
						f"Range {part!r} — both ends must be integers."
					)
					continue
				if lo > hi:
					errors.append(
						f"Range {part!r} — start {lo} is greater than end {hi}."
					)
					continue
				if lo < 1 or hi > 64:
					errors.append(
						f"Range {part!r} — zones must be 1-64."
					)
					# Still add the in-range subset so it's not wholly lost.
					lo = max(lo, 1)
					hi = min(hi, 64)
				for z in range(lo, hi + 1):
					zones.add(z)
			else:
				# Single number.
				try:
					z = int(part)
				except ValueError:
					errors.append(f"Not a number: {part!r}.")
					continue
				if z < 1 or z > 64:
					errors.append(f"Zone {z} out of range (must be 1-64).")
					continue
				zones.add(z)

		return sorted(zones), errors

	def _recordZoneOutcome(self, devId, outcome, panelLabel=None, details=None):
		"""Record a per-zone outcome from a read/write operation. The
		recorded outcomes are dumped to the log at end-of-operation
		via `_logZoneOutcomes`, which gives the user a tidy summary
		of what happened.

		Single-zone (per-zone button) and bulk runs both record into
		the same per-operation list — see `_beginZoneOutcomes` /
		`_logZoneOutcomes` for the lifecycle.

		`outcome` is a short string like 'Write SUCCESS', 'Write FAIL',
		'Read SUCCESS', 'Read FAIL'. `panelLabel` is the label as last
		seen on the panel (None if unknown). `details` is an optional
		short reason for failures.

		Best-effort: any exception is caught so a recording failure
		can't break the worker.
		"""
		try:
			if self.bulkOperationOutcomes is None:
				return  # nobody called _beginZoneOutcomes — defensive
			# Look up device for its name + zone number so the summary
			# is human-readable.
			try:
				dev = indigo.devices[devId]
				devName = dev.name
				zoneNumber = int(dev.pluginProps.get('zoneNumber', '0') or '0')
			except Exception:
				devName = f"<device id {devId}>"
				zoneNumber = 0
			self.bulkOperationOutcomes.append({
				'devId':       devId,
				'devName':     devName,
				'zoneNumber':  zoneNumber,
				'outcome':     outcome,
				'panelLabel':  panelLabel,
				'details':     details,
			})
		except Exception as err:
			self._alwaysLog(
				f"_recordZoneOutcome: internal error recording outcome "
				f"for device {devId}: {err}. Continuing.",
				isError=True,
			)

	def _beginZoneOutcomes(self):
		"""Start a fresh per-zone outcomes list. Call from the top of
		any operation that wants the end-of-run summary (single-zone
		read/write button, bulk read, bulk write). Safe to call when
		one's already in progress — it just resets.
		"""
		self.bulkOperationOutcomes = []

	def _logZoneOutcomes(self, displayLabel):
		"""Dump self.bulkOperationOutcomes to the log as a tidy
		per-zone summary block. Call from the operation's finally
		clause. Always clears the list afterwards.

		Single-zone runs (one entry) get a compact one-line summary:
		    DSC Alarm: Zone Label Read summary: zone 048 (Loft Hatch Sensor): SUCCESS - 'Loft Hatch'

		Multi-zone runs get a block with header/footer:
		    ---- Read All Zone Labels: per-zone results (3 zone(s)) ----
		      zone 001 (Front Door)        : SUCCESS - 'Front Door'
		      zone 002 (Hallway Motion)    : SUCCESS - 'Hallway Motion' (after 2 attempts)
		      zone 048 (Loft Hatch Sensor) : FAIL - stage 4 - per-char mismatch on char 02 (after 3 attempts)
		    ---- End per-zone results ----

		When a bulk operation runs multiple passes (retry passes), each
		zone may have several entries in the outcomes list — one per
		attempt. We collapse them so the summary shows ONE line per zone
		with the FINAL outcome, plus an "(after N attempts)" suffix when
		the zone was attempted more than once.

		Zones are sorted by zone number.
		"""
		outcomes = self.bulkOperationOutcomes or []
		# Always clear the list so the next operation starts fresh,
		# even on early returns.
		self.bulkOperationOutcomes = None

		if not outcomes:
			# Nothing to summarise — operation aborted before any
			# zone-level outcome was recorded (e.g. validation
			# failure, IT-100 not connected). The caller has already
			# logged the abort reason; no summary needed.
			return

		# Collapse by devId — keep the LAST outcome (final result after
		# any retries) and count total attempts for that zone.
		finalByDevId = {}     # devId -> the last outcome dict
		attemptsByDevId = {}  # devId -> int
		for o in outcomes:
			devId = o.get('devId')
			finalByDevId[devId] = o
			attemptsByDevId[devId] = attemptsByDevId.get(devId, 0) + 1

		collapsed = list(finalByDevId.values())

		def _formatTail(o, attempts):
			outcome = o.get('outcome', '?')
			panelLabel = o.get('panelLabel')
			details = o.get('details')
			if panelLabel is not None and details:
				tail = f" - {panelLabel!r} - {details}"
			elif panelLabel is not None:
				tail = f" - {panelLabel!r}"
			elif details:
				tail = f" - {details}"
			else:
				tail = ''
			if attempts > 1:
				tail += f" (after {attempts} attempts)"
			return outcome, tail

		def _formatOutcomeLine(o, attempts):
			zn = o.get('zoneNumber', 0)
			nm = o.get('devName', '?')
			outcome, tail = _formatTail(o, attempts)
			return f"zone {zn:03d} ({nm}): {outcome}{tail}"

		# Sort by zone number for human readability.
		ordered = sorted(collapsed, key=lambda o: (o.get('zoneNumber', 0), o.get('devName', '')))

		if len(ordered) == 1:
			o = ordered[0]
			devId = o.get('devId')
			self._alwaysLog(
				f"{displayLabel} summary: {_formatOutcomeLine(o, attemptsByDevId.get(devId, 1))}"
			)
			return

		# Multi-zone: header + indented lines + footer. Compute name
		# column width so the SUCCESS/FAIL part lines up.
		nameWidth = max(
			len(f"zone {o.get('zoneNumber', 0):03d} ({o.get('devName', '?')})")
			for o in ordered
		)
		self._alwaysLog(
			f"---- {displayLabel}: per-zone results ({len(ordered)} zone(s)) ----"
		)
		for o in ordered:
			zn = o.get('zoneNumber', 0)
			nm = o.get('devName', '?')
			devId = o.get('devId')
			header = f"zone {zn:03d} ({nm})"
			outcome, tail = _formatTail(o, attemptsByDevId.get(devId, 1))
			self._alwaysLog(f"  {header:<{nameWidth}} : {outcome}{tail}")
		self._alwaysLog(f"---- End {displayLabel} per-zone results ----")

	def _sendKeystrokeToIT100(self, keyChar):
		"""Queue a single keystroke to the IT-100 via TPI command 070.

		Each keystroke is 070<char> followed by 070^ (break key), as per the
		IT-100 manual section 3.6 ('each simulated Keypress must be followed
		by a keybreak'). Uses force_interface='it100' explicitly — we never
		want to fall back to Envisalink for label programming because the
		Envisalink TPI locks out keypresses in installer mode.

		Returns True on success, False if IT-100 isn't connected.
		"""
		if not self.it100Connected:
			return False
		# sendCommand handles the queue + checksum. Bypass the fallback
		# behaviour by NOT going through it — write directly to the IT-100
		# queue so a missing IT-100 connection is a hard fail rather than
		# silently jumping to Envisalink.
		self.it100TxCmdList.append((kCmdNormal, '070' + keyChar))
		self.it100TxCmdList.append((kCmdNormal, '070^'))
		return True

	# IT-100 TPI 070 keypress alphabet. Each char in here corresponds to one
	# physical key on a PowerSeries keypad and gets sent as '070X' on the
	# wire. (The break key '^' is sent automatically between consecutive
	# keypresses by the queueing layer, not by the user.)
	#
	# Per the IT-100 v1.0 developer's guide:
	#   Digits 0-9        ASCII '0'-'9'
	#   Star              ASCII '*'  (0x2A)
	#   Hash              ASCII '#'  (0x23)
	#   Left arrow        ASCII '<'  (0x3C)
	#   Right arrow       ASCII '>'  (0x3E)
	#   Both arrows       ASCII '='  (0x3D)   used to enter the language menu
	#   Function keys     ASCII 'a','b','c','d','e'   F1, F2, F3, F4, F5
	#   Emergency keys    ASCII 'F','A','P'           Fire, Auxiliary, Panic
	IT100_KEYPRESS_ALPHABET = set('0123456789*#<>=abcdeFAP')

	# Friendly bracketed-token aliases for the keypress alphabet. Lets users
	# write '[STAR]' instead of '*' and '[LEFT]' instead of '<' — particularly
	# useful for the arrow keys which aren't obvious from the displayed glyph,
	# and for the function/emergency keys whose ASCII mapping isn't intuitive.
	# Each token maps to its single-char equivalent so the rest of the parser
	# downstream doesn't need to know about tokens.
	#
	# [WAIT] is special: it expands to 'L', which the executor treats as a
	# pause marker (sleep 2s) rather than a keystroke. Bare 'L' is also
	# accepted for backward compatibility with existing automations.
	IT100_KEYPRESS_TOKENS = {
		'STAR':  '*',
		'HASH':  '#',
		'LEFT':  '<',
		'RIGHT': '>',
		'BOTH':  '=',
		'F1':    'a',
		'F2':    'b',
		'F3':    'c',
		'F4':    'd',
		'F5':    'e',
		'FIRE':  'F',
		'AUX':   'A',
		'PANIC': 'P',
		'WAIT':  'L',
	}

	# Pre-compiled regex for matching a bracketed token in the user input.
	_IT100_TOKEN_RE = re.compile(r'\[([A-Z0-9]+)\]')

	def _parseKeypressString(self, raw):
		"""Parse a user-supplied keypress string into a list of raw key
		chars suitable for queueing to the IT-100. Returns (keys, errors).

		Rules:
		- Bracketed tokens like [STAR] or [F1] are expanded via
		  IT100_KEYPRESS_TOKENS.
		- Whitespace between keys is ignored ('* 2' == '*2').
		- Bare characters are validated against IT100_KEYPRESS_ALPHABET, plus
		  'L' as a pause marker for backward compatibility.
		- Anything else (Unicode arrows, punctuation, etc.) is rejected with
		  a descriptive error pointing the user at the allowed alphabet.

		On any error the caller should refuse to send anything — partial
		sends could leave the panel in an unexpected state.
		"""
		keys = []
		errors = []
		pos = 0
		while pos < len(raw):
			ch = raw[pos]
			if ch.isspace():
				pos += 1
				continue
			# Try matching a bracketed token starting at this position
			if ch == '[':
				m = self._IT100_TOKEN_RE.match(raw, pos)
				if m:
					tokenName = m.group(1)
					if tokenName in self.IT100_KEYPRESS_TOKENS:
						keys.append(self.IT100_KEYPRESS_TOKENS[tokenName])
						pos = m.end()
						continue
					else:
						validTokens = ', '.join(
							f"[{t}]" for t in sorted(self.IT100_KEYPRESS_TOKENS.keys())
						)
						errors.append(
							f"Unknown token '[{tokenName}]' at position {pos}. "
							f"Valid tokens: {validTokens}"
						)
						pos = m.end()
						continue
				else:
					errors.append(
						f"Malformed token (missing ']') at position {pos}: "
						f"{raw[pos:pos+10]!r}"
					)
					pos += 1
					continue
			# Bare character
			if ch in self.IT100_KEYPRESS_ALPHABET or ch == 'L':
				keys.append(ch)
			else:
				errors.append(
					f"Invalid character {ch!r} at position {pos}. "
					f"Valid raw chars: 0-9 * # < > = a b c d e F A P, "
					f"or use bracketed tokens like [STAR] [LEFT] [F1] [PANIC]"
				)
			pos += 1
		return keys, errors

	def readZoneLabelFromKeypad(self, valuesDict, typeId, devId):
		"""Device-config button callback. Briefly enters installer mode,
		navigates to the LCD-label-programming section for this zone, takes
		a snapshot of the LCD (which displays the current label), then
		exits installer mode. Writes the captured label back to the device's
		`labelText` pluginProp so it appears in the config dialog when
		reopened.

		Same safety preconditions as the write path: IT-100 must be
		enabled and connected, all partitions must be disarmed, installer
		code must be configured in pluginPrefs. Runs in a background thread
		because the full sequence takes ~15-20 seconds.
		"""
		try:
			dev = indigo.devices[devId]
		except Exception:
			self._alwaysLog(
				"Zone label read: could not look up device — save the zone "
				"first so it has an Indigo ID, then try again.",
				isError=True,
			)
			return valuesDict

		# --- Validation (same shape as the write path) ---
		zoneNumberRaw = valuesDict.get('zoneNumber', '')
		if not zoneNumberRaw:
			self._alwaysLog(
				f"Zone label read ({dev.name}): no Zone Number set.",
				isError=True,
			)
			return valuesDict
		try:
			zoneNumber = int(zoneNumberRaw)
		except (TypeError, ValueError):
			self._alwaysLog(
				f"Zone label read ({dev.name}): zone number {zoneNumberRaw!r} "
				f"is not a valid integer.",
				isError=True,
			)
			return valuesDict
		if not 1 <= zoneNumber <= 64:
			self._alwaysLog(
				f"Zone label read ({dev.name}): zone number {zoneNumber} out "
				f"of range (1-64).",
				isError=True,
			)
			return valuesDict

		installerCode = (self.pluginPrefs.get('installerCode') or '').strip()
		if not installerCode:
			self._alwaysLog(
				f"Zone label read ({dev.name}): installer code is not set in "
				f"plugin config.",
				isError=True,
			)
			return valuesDict
		if not installerCode.isdigit() or not 4 <= len(installerCode) <= 6:
			self._alwaysLog(
				f"Zone label read ({dev.name}): installer code in plugin "
				f"config must be 4-6 digits.",
				isError=True,
			)
			return valuesDict

		if not self.enableIT100:
			self._alwaysLog(
				f"Zone label read ({dev.name}): IT-100 is not enabled. "
				f"This operation is IT-100 only — Envisalink TPI key commands "
				f"lock out in installer mode. Aborting.",
				isError=True,
			)
			return valuesDict
		if not self.it100Connected:
			self._alwaysLog(
				f"Zone label read ({dev.name}): IT-100 is not connected.",
				isError=True,
			)
			return valuesDict

		if self.bulkLabelReadInProgress:
			self._alwaysLog(
				f"Zone label read ({dev.name}): a bulk 'Read All Zone "
				f"Labels' operation is already in progress — refusing to "
				f"start a second IT-100 read at the same time. Wait for "
				f"the bulk read to finish (watch the log), then try again.",
				isError=True,
			)
			return valuesDict

		# Refuse if any partition is armed — entering installer mode while
		# armed would be a disaster.
		notReady = []
		for partition, keypadId in self.keypadList.items():
			try:
				keypad = indigo.devices[keypadId]
			except Exception:
				continue
			state = keypad.states.get('state', 'unknown')
			if state != kAlarmStateDisarmed:
				notReady.append(f"partition {partition} is in state '{state}'")
		if notReady:
			self._alwaysLog(
				f"Zone label read ({dev.name}): cannot enter installer mode — "
				+ "; ".join(notReady)
				+ ". Disarm all partitions first.",
				isError=True,
			)
			return valuesDict

		# Flush any leftover keystrokes from a previous operation.
		oldQueue = self.it100TxCmdList
		self.it100TxCmdList = []
		if oldQueue:
			self._alwaysLog(
				f"Zone label read ({dev.name}): flushed {len(oldQueue)} stale "
				f"keystrokes from the IT-100 queue before starting."
			)

		self._alwaysLog(
			f"Zone label read ({dev.name}): starting in background. Will enter "
			f"installer mode, navigate to section {zoneNumber:03d}, read the "
			f"LCD, and exit. Estimated runtime ~15-20s. DO NOT touch any "
			f"physical keypad. When done, you MUST click CANCEL on the "
			f"config dialog (NOT OK/Save), then reopen it to see the "
			f"captured label."
		)

		t = threading.Thread(
			target=self._runZoneLabelRead,
			args=(devId, dev.name, zoneNumber, installerCode),
			name=f"DSCAlarm-ZoneLabelRead-{zoneNumber}",
			daemon=True,
		)
		t.start()
		return valuesDict

	# Menu/confirmation words seen on LCDLine1 while NOT on the label
	# edit screen. Anything containing one of these strings is a
	# transient menu/confirmation screen (Select Option, Change Case,
	# Lower Case Now, Save?, etc.) that appears during a case toggle
	# and consumes keystrokes — we should wait it out before sending
	# digit presses for character entry.
	#
	# Deliberately excluded: 'system is', 'ready to arm', 'before
	# arming' and similar status-broadcast strings. Those can fire as
	# transient overlays at any time but are NOT menus and don't
	# consume our keystrokes — including them here would cause spurious
	# waits.
	LABEL_TRANSIENT_LCD_WORDS = (
		'select option',
		'save changes',
		'save?',
		'change case',
		'cycle case',
		'lower case now',
		'upper case now',
		'selected',
	)

	def _waitForLabelEditScreen(self, charIndex, maxWaitSeconds=4.0, pollIntervalSeconds=0.3):
		"""Wait for a case-toggle sequence to complete: the panel must
		first ENTER a menu screen (proving the case-toggle keystrokes
		have started landing), then LEAVE that menu screen (proving the
		toggle has been committed and we're back on the label edit view).

		Without the two-phase check, the wait can exit prematurely
		BEFORE the panel processes the case-toggle keystrokes — because
		the case-toggle keystrokes are queued and take time to leave the
		TX path. If we start polling immediately after queueing them,
		the first poll sees the LCD still on the label edit view (because
		the panel hasn't yet seen the '*' keystroke) and falsely concludes
		'case toggle done'. The caller then sends digit presses while
		the case-toggle keystrokes are STILL draining, producing
		undefined behaviour.

		Returns True if the case toggle was observed to enter AND leave
		a menu screen within the timeout. Returns False otherwise.
		"""
		import time as _time
		deadline = _time.time() + maxWaitSeconds
		observed = []
		enteredMenu = False
		settled = False

		def _logLCD(l1):
			if l1 is not None and (not observed or observed[-1] != l1):
				observed.append(l1)

		def _snapshot():
			snap = self._snapshotKeypadLCD()
			for partition, (l1, _l2) in snap.items():
				if l1:
					return l1
			return None

		# Phase 1: wait for the panel to ENTER a menu screen.
		while _time.time() < deadline:
			_time.sleep(pollIntervalSeconds)
			lcdLine1 = _snapshot()
			_logLCD(lcdLine1)
			if lcdLine1 is None:
				continue
			lower = lcdLine1.lower()
			if any(w in lower for w in self.LABEL_TRANSIENT_LCD_WORDS):
				enteredMenu = True
				break

		# Phase 2: wait for the panel to LEAVE the menu (back to label edit).
		if enteredMenu:
			while _time.time() < deadline:
				_time.sleep(pollIntervalSeconds)
				lcdLine1 = _snapshot()
				_logLCD(lcdLine1)
				if lcdLine1 is None:
					continue
				lower = lcdLine1.lower()
				if not any(w in lower for w in self.LABEL_TRANSIENT_LCD_WORDS):
					settled = True
					break

		if settled:
			self._alwaysLog(
				f"  char {charIndex+1:02d}: case-toggle settled, LCD back on "
				f"label edit (saw {len(observed)} screen(s) en route: "
				f"{observed})."
			)
		elif enteredMenu:
			self._alwaysLog(
				f"  char {charIndex+1:02d}: case-toggle entered menu but did "
				f"NOT settle within {maxWaitSeconds}s — last LCD: "
				f"{observed[-1] if observed else 'N/A'!r}. Continuing anyway; "
				f"verification will catch any resulting mismatch."
			)
		else:
			self._alwaysLog(
				f"  char {charIndex+1:02d}: case-toggle did NOT cause any "
				f"menu screen to appear within {maxWaitSeconds}s — the "
				f"keystrokes may have been dropped. Last LCD: "
				f"{observed[-1] if observed else 'N/A'!r}. Continuing "
				f"anyway; verification will catch any resulting mismatch."
			)
		return settled

	def _runZoneLabelRead(self, devId, devName, zoneNumber, installerCode, manageOutcomes=True):
		"""Background-thread body for readZoneLabelFromKeypad. Runs the
		read as a staged state machine, taking the LCD snapshot once the
		panel reaches the label-edit screen.

		Same stages 0-3 as the write path (pre-flight, enter installer, *
		for LCD label menu, section number). Where write proceeds with
		stages 4-7 (type, save, exit-section, exit-installer), read just
		snapshots the LCD and does the exit-installer stages.

		`manageOutcomes` controls per-zone-outcomes lifecycle. The
		per-zone button calls with the default True, so this method
		opens a fresh outcomes list at the top and logs the summary at
		the bottom. The bulk read worker passes False — IT owns the
		outcomes list (one list spanning all zones in the bulk run).
		"""
		if manageOutcomes:
			self._beginZoneOutcomes()
		section = f"{zoneNumber:03d}"

		segmentDict = {
			'enter_installer': ['*', '8'] + list(installerCode),
			'select_lcd_menu': ['*'],
			'section':         list(section),
			# No label-text, no save — just exit. Three # needed to fully
			# exit because we're three menu levels deep:
			#   inside section 005 → # → 'Enter LCD / Section ---'
			#   LCD label sub-menu → # → 'Enter Section ---' (outer installer)
			#   outer installer    → # → idle
			# Previously we only sent two #s and relied on the panel's
			# installer-mode idle timeout (~6s) to drop us out. That worked
			# sometimes but timed out the verification wait other times.
			'exit_section':    ['#'],
			'exit_lcd_menu':   ['#'],
			'exit_installer':  ['#'],
		}

		def _sendSegment(name):
			keys = segmentDict.get(name, [])
			self._alwaysLog(
				f"  segment {name!r}: queueing {len(keys)} keystrokes "
				f"({''.join(keys)!r})"
			)
			for k in keys:
				if not self._sendKeystrokeToIT100(k):
					self._alwaysLog(
						f"Zone label read ({devName}): IT-100 went away "
						f"during segment '{name}'. The panel may still be "
						f"in installer mode — press the STOP button or "
						f"send #### on a physical keypad to back out.",
						isError=True,
					)
					return False
			return True

		def _emergencyBackout():
			"""Send enough # keystrokes to exit any depth of installer menu."""
			for _ in range(4):
				self._sendKeystrokeToIT100('#')

		try:
			self._alwaysLog(
				f"Zone label read ({devName}): --- staged read for zone "
				f"{zoneNumber:03d} ---"
			)
			opLabel = f"Zone label read ({devName})"

			# Pre-flight: clear any latent installer state with up to 3
			# rounds of #### (a single round can fall short on a busy
			# keybus). If we still can't get back to idle the helper
			# logs an abort message that does NOT tell the user to do
			# anything impossible (other keypads are locked out while
			# the IT-100 holds installer mode).
			if self._preFlightBackoutToIdle(opLabel) != 'idle':
				return

			# Stage 1: *8 + code → wait for installer menu, with retry on
			# panel-revert-to-idle (which usually means a dropped keystroke).
			stage1Result = self._enterInstallerModeWithRetry(
				opLabel=opLabel,
				sendEnterInstallerSegment=lambda: _sendSegment('enter_installer'),
			)
			if stage1Result == 'success':
				pass  # Continue to stage 2
			elif stage1Result == 'wrong_code':
				self._alwaysLog(
					f"{opLabel}: ABORTED at stage 1 — check the installer "
					f"code in the plugin config. Backing out.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Read FAIL',
					panelLabel=None,
					details='stage 1 - wrong installer code',
				)
				_emergencyBackout()
				return
			elif stage1Result == 'io_error':
				# IT-100 went away — emergency backout might not even work.
				# The _sendSegment call already logged the failure.
				self._recordZoneOutcome(
					devId, 'Read FAIL',
					panelLabel=None,
					details='stage 1 - IT-100 IO error',
				)
				return
			else:  # 'failed_after_retries'
				self._alwaysLog(
					f"{opLabel}: ABORTED at stage 1 — couldn't reach the "
					f"installer menu after retries. See LCD transitions above. "
					f"Backing out.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Read FAIL',
					panelLabel=None,
					details='stage 1 - did not reach installer menu after retries',
				)
				_emergencyBackout()
				return

			# Stage 2: * → wait for LCD label menu
			if not _sendSegment('select_lcd_menu'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['enter lcd'],
				timeout_seconds=10,
				prefix="stage 2 (select LCD label menu)",
			)
			if result != 'success':
				self._alwaysLog(
					f"Zone label read ({devName}): ABORTED at stage 2 — "
					f"panel didn't show 'Enter LCD' within 10 seconds after "
					f"sending '*'. Backing out.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Read FAIL',
					panelLabel=None,
					details="stage 2 - panel didn't show 'Enter LCD'",
				)
				_emergencyBackout()
				return

			# Stage 3: section number → wait for label-edit display.
			# Same critical 'Enter Data' guard as the write path — if we
			# see that, we're in a DATA section, not the label section, and
			# must abort. Sending no further keystrokes makes us safe (#
			# always backs out without saving).
			if not _sendSegment('section'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=[],
				failure_substrings=['enter data'],
				timeout_seconds=5,
				prefix="stage 3 (enter section)",
			)
			if result == 'failure':
				self._alwaysLog(
					f"Zone label read ({devName}): ABORTED at stage 3 — "
					f"panel showed {matched}. Landed in a DATA section, "
					f"not the label screen. Backing out.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Read FAIL',
					panelLabel=None,
					details=f"stage 3 - landed in DATA section ({matched})",
				)
				_emergencyBackout()
				return

			# At this point the LCD shows the current label on line 1.
			# Snapshot it across all keypads (both partitions get the same
			# broadcast). Trim trailing whitespace — DSC right-pads labels
			# to 14 chars with spaces, but the human-meaningful content
			# stops at the last non-space character.
			snapshot = self._snapshotKeypadLCD()
			readLabel = None
			for partition, (l1, l2) in snapshot.items():
				self._alwaysLog(
					f"  zone {zoneNumber:03d} LCD on p{partition}: "
					f"line1={l1!r} line2={l2!r}"
				)
				# Prefer the first partition's reading. They should all be
				# identical anyway since cmd 901 broadcasts to all keypads.
				if readLabel is None and l1:
					readLabel = l1.rstrip()

			if readLabel is None:
				self._alwaysLog(
					f"Zone label read ({devName}): WARNING — couldn't get "
					f"any LCD content from the keypad devices. Aborting "
					f"with backout.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Read FAIL',
					panelLabel=None,
					details='no LCD content captured from keypad',
				)
				_emergencyBackout()
				return

			self._alwaysLog(
				f"Zone label read ({devName}): captured label "
				f"{readLabel!r} for zone {zoneNumber:03d}."
			)

			# Exit cleanly. We're three menu levels deep so we need three #s:
			# inside section → LCD label sub-menu → outer installer → idle.

			# Stage 4: # to exit the section view → 'Enter LCD / Section ---'
			if not _sendSegment('exit_section'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['enter lcd'],
				timeout_seconds=10,
				prefix="stage 4 (exit section)",
			)
			# Soft fail — continue to next exit regardless.

			# Stage 5: # to exit the LCD label sub-menu → 'Enter Section ---'
			if not _sendSegment('exit_lcd_menu'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['enter section'],
				timeout_seconds=10,
				prefix="stage 5 (exit LCD label menu)",
			)
			# Soft fail — continue.

			# Stage 6: # to exit installer mode → idle.
			if not _sendSegment('exit_installer'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['date', 'ready', 'disarmed'],
				timeout_seconds=10,
				prefix="stage 6 (exit installer)",
			)
			if result != 'success':
				self._alwaysLog(
					f"Zone label read ({devName}): exited installer mode "
					f"but couldn't verify return to idle within 10s. The "
					f"label was captured successfully and saved to the "
					f"device props above — but the panel may still be in "
					f"a menu state. Manually press # on a keypad if needed.",
					isError=True,
				)
				# Still try to save the label since we captured it.

			# Persist the read label to the device's pluginProps so it
			# shows up in the labelText field next time the user opens the
			# config dialog. This is the whole point of the operation.
			#
			# IMPORTANT UX NOTE: the dialog the user clicked Read from is
			# still open with the OLD valuesDict in its in-memory state.
			# If they click OK/Save now, that OLD valuesDict overwrites
			# what we just wrote to pluginProps. They MUST click Cancel
			# instead, then reopen the dialog to see the new value. This
			# is a fundamental Indigo dialog-API limitation — there's no
			# supported way to push values into an open modal dialog from
			# a background thread.
			try:
				# Re-fetch the device fresh so we get the current props.
				# (Don't reuse the `dev` from earlier in this method — it
				# was captured when the read started, and may now be stale.)
				dev = indigo.devices[devId]
				# Use .copy() to get a mutable dict we own — the Indigo
				# docs are explicit that dev.pluginProps returns a copy but
				# being explicit makes the intent clearer.
				newProps = dict(dev.pluginProps)
				self._alwaysLog(
					f"  diagnostic: pluginProps BEFORE write has "
					f"{len(newProps)} keys: {sorted(newProps.keys())}"
				)
				newProps['labelText'] = readLabel
				# Also persist lastSyncedLabelText — used by the
				# 'Write Changed Zone Labels' action to detect zones
				# where the user has edited labelText locally and needs
				# to push the change to the panel. Updating it here on
				# every successful read means a read counts as 'I'm in
				# sync with the panel', so a subsequent write is only
				# needed if the user manually edits labelText afterward.
				newProps['lastSyncedLabelText'] = readLabel
				dev.replacePluginPropsOnServer(newProps)
				# Re-fetch and log to confirm the write took effect
				dev2 = indigo.devices[devId]
				afterProps = dict(dev2.pluginProps)
				self._alwaysLog(
					f"  diagnostic: pluginProps AFTER write has "
					f"{len(afterProps)} keys: {sorted(afterProps.keys())}"
				)
				if 'labelText' in afterProps:
					self._alwaysLog(
						f"  diagnostic: labelText now persisted as "
						f"{afterProps['labelText']!r}"
					)
				else:
					self._alwaysLog(
						f"  diagnostic: labelText is NOT in pluginProps after "
						f"the write — replacePluginPropsOnServer didn't "
						f"persist it. This is unexpected and is the bug.",
						isError=True,
					)
				self._alwaysLog(
					f"Zone label read ({devName}): SUCCESS — captured label "
					f"{readLabel!r} and saved it to the device's "
					f"'labelText' property."
				)
				self._recordZoneOutcome(
					devId, 'Read SUCCESS', panelLabel=readLabel
				)
				self._alwaysLog(
					f"  IMPORTANT: the config dialog is still open with its "
					f"OLD label value in memory. If you click OK/Save now, "
					f"that OLD value will overwrite what was just saved. "
					f"You MUST click CANCEL on the dialog first, then "
					f"reopen it — the Label Text field will then show the "
					f"captured label {readLabel!r}."
				)
			except Exception as err:
				self._alwaysLog(
					f"Zone label read ({devName}): captured label "
					f"{readLabel!r} but couldn't write it to the device "
					f"props: {err}. The label IS in this log above for you "
					f"to copy/paste manually.",
					isError=True,
				)

		except Exception as err:
			self._alwaysLog(
				f"Zone label read ({devName}): unexpected error in "
				f"background thread: {err}. The panel may still be in "
				f"installer mode — sending #### to back out.",
				isError=True,
			)
			try:
				self._recordZoneOutcome(
					devId, 'Read FAIL',
					panelLabel=None,
					details=f"unexpected error: {err}",
				)
			except Exception:
				pass
			try:
				_emergencyBackout()
			except Exception:
				pass
		finally:
			# If we opened a fresh outcomes list at the top, dump the
			# summary and clear it. Bulk callers pass manageOutcomes=False
			# and own the lifecycle themselves.
			if manageOutcomes:
				try:
					self._logZoneOutcomes(f"Zone Label Read ({devName})")
				except Exception:
					pass

	def _parseRetryConfig(self, action, defaultPasses=2, defaultInterPass=15, defaultInterZone=5):
		"""Parse the retryPasses / interPassDelaySeconds / interZoneDelaySeconds
		fields from a bulk-action's props dict. All three are optional textfield
		ints with sensible defaults; if missing, malformed, or out of range we
		clamp to the recommended ranges and log a warning rather than failing
		the action.

		Returns a dict with keys 'retryPasses' (0-5), 'interPassDelaySeconds'
		(0-120), 'interZoneDelaySeconds' (0-30).
		"""
		def _parseClampedInt(field, default, lo, hi):
			raw = (action.props.get(field) or '').strip() if hasattr(action, 'props') else ''
			if raw == '':
				return default
			try:
				v = int(raw)
			except (TypeError, ValueError):
				self._alwaysLog(
					f"Bulk action: '{field}' value {raw!r} is not a valid "
					f"integer — using default ({default}).",
					isError=True,
				)
				return default
			if v < lo:
				self._alwaysLog(
					f"Bulk action: '{field}' value {v} is below the minimum "
					f"({lo}) — clamping to {lo}.",
					isError=True,
				)
				return lo
			if v > hi:
				self._alwaysLog(
					f"Bulk action: '{field}' value {v} is above the maximum "
					f"({hi}) — clamping to {hi}.",
					isError=True,
				)
				return hi
			return v

		return {
			'retryPasses':           _parseClampedInt('retryPasses',           defaultPasses,   0,   5),
			'interPassDelaySeconds': _parseClampedInt('interPassDelaySeconds', defaultInterPass, 0, 120),
			'interZoneDelaySeconds': _parseClampedInt('interZoneDelaySeconds', defaultInterZone, 0,  30),
		}

	def methodReadAllZoneLabels(self, action):
		"""Action callback: read EVERY configured zone's label from the
		panel, overwriting any existing labelText. See
		`_kickOffBulkLabelRead` for the shared implementation.
		"""
		cfg = self._parseRetryConfig(action)
		self._kickOffBulkLabelRead(
			zoneFilter='all',
			zoneSpec=None,
			displayLabel="Read All Zone Labels",
			retryCfg=cfg,
		)

	def methodReadEmptyZoneLabels(self, action):
		"""Action callback: read only zones whose labelText is currently
		empty. Lets you re-run safely after editing some labels manually
		without overwriting your edits. See `_kickOffBulkLabelRead` for
		the shared implementation.
		"""
		cfg = self._parseRetryConfig(action)
		self._kickOffBulkLabelRead(
			zoneFilter='empty',
			zoneSpec=None,
			displayLabel="Read Empty Zone Labels",
			retryCfg=cfg,
		)

	def methodReadSpecificZoneLabels(self, action):
		"""Action callback: read labels for the zones specified in the
		action's `zoneList` field (e.g. '1,3,5-10,15'). Useful for
		picking up a handful of specific zones without re-reading the
		whole panel. See `_kickOffBulkLabelRead` for the shared
		implementation.
		"""
		# Action callbacks get the action's ConfigUI values via action.props.
		# Use getattr with a fallback to be safe across Indigo versions.
		props = getattr(action, 'props', None) or {}
		zoneSpec = (props.get('zoneList') or '').strip()
		cfg = self._parseRetryConfig(action)
		self._kickOffBulkLabelRead(
			zoneFilter='specific',
			zoneSpec=zoneSpec,
			displayLabel="Read Specific Zone Labels",
			retryCfg=cfg,
		)

	def methodCancelBulkOperation(self, action):
		"""Action callback: set the cancel flag for any in-progress
		bulk label operation. Workers (bulk read, bulk write) check
		this flag between zones and exit cleanly if set. The
		currently-in-progress zone is allowed to finish first (can
		take up to ~90s for a write), so cancellation isn't instant.

		The 'Broadcast Labels' operation cannot be cancelled — once
		section 998 is sent the panel is broadcasting autonomously on
		the keybus and doesn't accept any interruption. If the user
		hits cancel during a broadcast, we explain that and do
		nothing.
		"""
		if not self.bulkLabelReadInProgress:
			self._alwaysLog(
				"Cancel Bulk Label Operation: no bulk operation is "
				"currently running. Nothing to cancel."
			)
			return

		kind = self.bulkOperationKind or "the running bulk operation"

		# Special case: broadcast cannot be cancelled.
		if self.bulkOperationKind == "Broadcast Labels":
			self._alwaysLog(
				"Cancel Bulk Label Operation: 'Broadcast Labels' is "
				"running and CANNOT be cancelled — once section 998 is "
				"sent the panel is broadcasting autonomously on the keybus "
				"and doesn't accept interruption. The broadcast will "
				"complete on its own (typically 30-90 seconds total). "
				"Watch the log for the completion message."
			)
			return

		if self.bulkOperationCancelRequested:
			self._alwaysLog(
				f"Cancel Bulk Label Operation: a cancel request for "
				f"'{kind}' has already been sent — the worker is "
				f"finishing its current zone and will stop. Watch the "
				f"log for the cancellation summary."
			)
			return

		self.bulkOperationCancelRequested = True
		self._alwaysLog(
			f"Cancel Bulk Label Operation: REQUESTED for '{kind}'. "
			f"Flushing any pending keystrokes and sending #### "
			f"immediately to push the panel out of installer mode, "
			f"then signalling the worker to stop. Watch the log for "
			f"the cancellation summary."
		)
		# Push the panel back to idle RIGHT NOW rather than waiting for
		# the worker's per-zone state machine to notice and do its own
		# backout. Two rounds of #### with a small gap because a single
		# round can have keystrokes dropped on a busy keybus during the
		# transition. Harmless if the panel is already at idle (# does
		# nothing from the Ready state).
		#
		# Flush the pending IT-100 keystroke queue first so any half-typed
		# label characters from the in-progress write don't keep landing
		# on the panel after we've sent ####. Without this, the panel
		# would type out the rest of "Loung..." or whatever, then receive
		# our #### afterwards — messy and slow. Flushing skips straight
		# to the backout.
		#
		# We do this from the cancel-action's own thread (not the worker
		# thread) so the user sees the panel come back to home immediately,
		# even if the worker is mid-character-verification or mid-wait.
		try:
			flushed = len(self.it100TxCmdList)
			self.it100TxCmdList = []
			if flushed:
				self._alwaysLog(
					f"Cancel Bulk Label Operation: flushed {flushed} "
					f"pending IT-100 keystroke(s) from the queue."
				)
			self._emergencyBackoutIT100()
			time.sleep(0.5)
			self._emergencyBackoutIT100()
		except Exception as err:
			self._alwaysLog(
				f"Cancel Bulk Label Operation: error sending #### during "
				f"cancel: {err}. The worker will still stop, but the panel "
				f"may need manual recovery if it's stuck in installer mode.",
				isError=True,
			)

	def _kickOffBulkLabelRead(self, zoneFilter, zoneSpec, displayLabel, retryCfg=None):
		"""Shared implementation for the three bulk-read actions.

		`zoneFilter` selects which configured zones to read:
		  - 'all':      every configured zone (overwrites existing labelText)
		  - 'empty':    only zones whose labelText pluginProp is blank
		  - 'specific': only zones in `zoneSpec` (a string like '1,3,5-10,15')
		`zoneSpec` is only used when zoneFilter == 'specific'.
		`displayLabel` is the human-readable name used in log messages so
		the user can tell the three modes apart.

		Designed as a workaround for the dialog-display bug where the
		Label Text field on a device's config dialog doesn't pick up
		changes made via the per-zone Read button while the dialog is
		open. Running this action while NO config dialogs are open avoids
		the dialog-overwrite problem entirely — each device's pluginProps
		are written cleanly and stay written.

		Validation is done up-front (same shape as the per-zone read),
		then a single background worker thread runs the reads sequentially
		via `_runZoneLabelRead`. The IT-100 must be enabled and connected,
		all partitions must be disarmed for the entire duration, and the
		installer code must be configured in pluginPrefs.
		"""
		installerCode = (self.pluginPrefs.get('installerCode') or '').strip()
		if not installerCode:
			self._alwaysLog(
				f"{displayLabel}: installer code is not set in plugin "
				f"config. Configure it in Plugins → DSC Alarm → Configure.",
				isError=True,
			)
			return
		if not installerCode.isdigit() or not 4 <= len(installerCode) <= 6:
			self._alwaysLog(
				f"{displayLabel}: installer code in plugin config must "
				f"be 4-6 digits.",
				isError=True,
			)
			return

		if not self.enableIT100:
			self._alwaysLog(
				f"{displayLabel}: IT-100 is not enabled. This operation "
				f"is IT-100 only — Envisalink TPI key commands lock out in "
				f"installer mode. Aborting.",
				isError=True,
			)
			return
		if not self.it100Connected:
			self._alwaysLog(
				f"{displayLabel}: IT-100 is not connected.",
				isError=True,
			)
			return

		if self.bulkLabelReadInProgress:
			self._alwaysLog(
				f"{displayLabel}: a bulk read is already in progress. "
				f"Wait for it to finish (watch the log) before starting "
				f"another.",
				isError=True,
			)
			return

		# Refuse if any partition is armed — entering installer mode while
		# armed would be a disaster.
		notReady = []
		for partition, keypadId in self.keypadList.items():
			try:
				keypad = indigo.devices[keypadId]
			except Exception:
				continue
			state = keypad.states.get('state', 'unknown')
			if state != kAlarmStateDisarmed:
				notReady.append(f"partition {partition} is in state '{state}'")
		if notReady:
			self._alwaysLog(
				f"{displayLabel}: cannot enter installer mode — "
				+ "; ".join(notReady)
				+ ". Disarm all partitions first.",
				isError=True,
			)
			return

		# Build the candidate zone list based on filter.
		# 'all':      every configured zone
		# 'empty':    configured zones whose labelText pluginProp is blank
		# 'specific': zones from zoneSpec, intersected with configured zones
		if zoneFilter == 'specific':
			parsedZones, parseErrors = self._parseZoneRangeSpec(zoneSpec)
			if parseErrors:
				self._alwaysLog(
					f"{displayLabel}: problems with zone list {zoneSpec!r}:",
					isError=True,
				)
				for err in parseErrors:
					self._alwaysLog(f"  {err}", isError=True)
				if not parsedZones:
					self._alwaysLog(
						f"{displayLabel}: no usable zones in the list. "
						f"Aborting.",
						isError=True,
					)
					return
				self._alwaysLog(
					f"{displayLabel}: proceeding with the valid subset: "
					f"{parsedZones}"
				)
			candidateZones = parsedZones
		else:
			# 'all' or 'empty' — every configured zone is a candidate; the
			# 'empty' filter is applied below.
			candidateZones = sorted(self.zoneList.keys())

		# Materialise into (zoneNumber, devId, devName) and apply
		# per-filter skip logic. zoneList is populated in deviceStartComm,
		# so any zone without a configured Indigo device is naturally
		# skipped — but for 'specific' we count those so the user can see
		# what got filtered.
		todo = []
		skippedNoDevice = 0
		skippedAlreadyHasLabel = 0
		for zoneNumber in candidateZones:
			devId = self.zoneList.get(zoneNumber)
			if devId is None:
				skippedNoDevice += 1
				continue
			try:
				dev = indigo.devices[devId]
			except Exception:
				# Device was deleted but zoneList still has it — skip.
				skippedNoDevice += 1
				continue

			if zoneFilter == 'empty':
				existingLabel = (dev.pluginProps.get('labelText') or '').strip()
				if existingLabel:
					skippedAlreadyHasLabel += 1
					continue

			todo.append((zoneNumber, devId, dev.name))

		# Build a skip-summary line so the user can see what got filtered.
		skipNotes = []
		if skippedNoDevice:
			skipNotes.append(f"{skippedNoDevice} not configured in Indigo")
		if skippedAlreadyHasLabel:
			skipNotes.append(f"{skippedAlreadyHasLabel} already have labels")
		skipSummary = (" (skipped: " + ", ".join(skipNotes) + ")") if skipNotes else ""

		if not todo:
			if zoneFilter == 'empty':
				self._alwaysLog(
					f"{displayLabel}: every configured zone already has a "
					f"non-empty Label Text{skipSummary}. Nothing to do. To "
					f"force a re-read use 'Read All Zone Labels from "
					f"Panel' instead."
				)
			elif zoneFilter == 'specific':
				self._alwaysLog(
					f"{displayLabel}: nothing to read{skipSummary}. "
					f"Did the zone list match any configured zones?",
					isError=True,
				)
			else:
				self._alwaysLog(
					f"{displayLabel}: no configured zone devices found. "
					f"Nothing to read.",
					isError=True,
				)
			return

		estimatedMinutes = int((len(todo) * 18) / 60) + 1
		self._alwaysLog(
			f"{displayLabel}: starting bulk read of {len(todo)} "
			f"configured zone(s){skipSummary}. Estimated total runtime "
			f"~{estimatedMinutes} minute(s). DO NOT touch any physical "
			f"keypad or open any zone's config dialog during this "
			f"operation. Watch the log for per-zone progress. After it "
			f"completes, open each device's config dialog to see the "
			f"captured label in the Label Text field."
		)

		# Clear any stale cancel request from a prior run.
		self.bulkOperationCancelRequested = False
		# Defensive default if no retry config was passed.
		if retryCfg is None:
			retryCfg = {'retryPasses': 2, 'interPassDelaySeconds': 15, 'interZoneDelaySeconds': 5}
		t = threading.Thread(
			target=self._runBulkZoneLabelRead,
			args=(todo, installerCode, displayLabel, retryCfg),
			name="DSCAlarm-BulkZoneLabelRead",
			daemon=True,
		)
		t.start()

	def _runBulkZoneLabelRead(self, todo, installerCode, displayLabel="Bulk Read", retryCfg=None):
		"""Background-thread worker for the bulk-read actions. Calls
		`_runZoneLabelRead` for each (zoneNumber, devId, devName) tuple
		in `todo`, serially. Each individual read does its own pre-flight,
		installer-mode entry, navigation, snapshot, and clean exit, so
		failures on one zone don't poison the rest — the loop continues
		with the next zone.

		Multi-pass retry: after the initial pass, any zones that failed
		(determined by checking the outcomes recorded by _runZoneLabelRead)
		are retried in subsequent passes. `retryCfg['retryPasses']`
		controls how many EXTRA passes are made (0 = no retry, just the
		initial pass; 2 = up to 3 total attempts per zone). Between passes
		we wait `retryCfg['interPassDelaySeconds']` to let the panel fully
		recover from installer mode. Between zones within a single pass
		we wait `retryCfg['interZoneDelaySeconds']` (avoids the next zone's
		stage 1 colliding with the previous zone's exit packets).

		Success/failure counts are derived from the FINAL outcome per
		devId (collapsed by _logZoneOutcomes), not pass-by-pass tallies —
		so a zone that failed pass 1 and succeeded pass 2 counts as ONE
		success in the COMPLETE summary line.
		"""
		if retryCfg is None:
			retryCfg = {'retryPasses': 2, 'interPassDelaySeconds': 15, 'interZoneDelaySeconds': 5}
		retryPasses = retryCfg.get('retryPasses', 2)
		interPassDelay = retryCfg.get('interPassDelaySeconds', 15)
		interZoneDelay = retryCfg.get('interZoneDelaySeconds', 5)

		self.bulkLabelReadInProgress = True
		self.bulkOperationKind = displayLabel
		self._beginZoneOutcomes()
		try:
			totalZones = len(todo)
			cancelled = False
			pendingTodo = list(todo)  # zones still needing an attempt
			passNumber = 0  # 0 = initial pass, 1+ = retry passes
			passesRun = 0   # count of passes actually executed

			while pendingTodo:
				passLabel = "initial pass" if passNumber == 0 else f"retry pass {passNumber}"
				if passNumber > 0:
					self._alwaysLog(
						f"{displayLabel}: starting {passLabel} — "
						f"{len(pendingTodo)} zone(s) failed previous pass and "
						f"will be retried."
					)

				passFailedTodo, passCancelled = self._runZoneReadPass(
					pendingTodo, installerCode, displayLabel, passLabel, interZoneDelay
				)
				passesRun += 1

				if passCancelled:
					cancelled = True
					break

				if not passFailedTodo:
					# All zones in this pass succeeded — we're done.
					break

				# Some zones failed. Are we allowed another retry pass?
				if passNumber >= retryPasses:
					# Out of retry budget — leave the failures as-is for
					# the final summary to report.
					break

				# Otherwise, retry the failed zones in the next pass after
				# the inter-pass delay (lets the panel fully recover from
				# installer mode). Honour cancel during the delay.
				self._alwaysLog(
					f"{displayLabel}: {passLabel} complete with "
					f"{len(passFailedTodo)} failure(s). Waiting "
					f"{interPassDelay}s for the panel to recover before "
					f"retry pass {passNumber + 1}."
				)
				delayRemaining = interPassDelay
				while delayRemaining > 0:
					if self.bulkOperationCancelRequested:
						cancelled = True
						break
					step = min(1.0, delayRemaining)
					time.sleep(step)
					delayRemaining -= step
				if cancelled:
					self._alwaysLog(
						f"{displayLabel}: CANCEL honoured during inter-pass "
						f"delay. Stopping the bulk run."
					)
					break

				pendingTodo = passFailedTodo
				passNumber += 1

			# Compute final counts from outcomes (collapsed by devId).
			# Any devId whose LAST outcome string starts with "Read SUCCESS"
			# is a success; everything else is a failure. Zones that never
			# got an outcome (e.g. cancelled before being attempted) count
			# as not-attempted.
			finalOutcomeByDevId = {}
			for o in (self.bulkOperationOutcomes or []):
				finalOutcomeByDevId[o.get('devId')] = (o.get('outcome') or '')
			successes = sum(
				1 for outcome in finalOutcomeByDevId.values()
				if outcome.startswith('Read SUCCESS')
			)
			failures = sum(
				1 for outcome in finalOutcomeByDevId.values()
				if not outcome.startswith('Read SUCCESS')
			)
			notAttempted = totalZones - len(finalOutcomeByDevId)

			passSuffix = "" if passesRun == 1 else f" (across {passesRun} pass(es))"
			if cancelled:
				self._alwaysLog(
					f"{displayLabel}: CANCELLED. {successes}/{totalZones} "
					f"zone(s) read successfully before cancellation"
					+ (f", {failures} failed" if failures else "")
					+ (f", {notAttempted} not attempted" if notAttempted else "")
					+ f"{passSuffix}. "
					f"To resume, you can use 'Read Only Empty Zone Labels' "
					f"(which will pick up zones still without a label) or "
					f"trigger this action again."
				)
			else:
				self._alwaysLog(
					f"{displayLabel}: COMPLETE. {successes}/{totalZones} "
					f"zone(s) read successfully"
					+ (f", {failures} failed" if failures else "")
					+ f"{passSuffix}"
					+ ". Open each device's config dialog to see the captured "
					f"labels in the Label Text field. (Make sure NO config "
					f"dialogs were open during the run — if any were, those "
					f"specific zones' Label Text fields may have been "
					f"overwritten by the dialog's stale in-memory state on "
					f"close.)"
				)
		except Exception as err:
			self._alwaysLog(
				f"{displayLabel}: unexpected error in bulk worker: "
				f"{err}. Sending #### to back out of installer mode if "
				f"stuck.",
				isError=True,
			)
			try:
				self._sendKeystrokeToIT100('#')
				self._sendKeystrokeToIT100('#')
				self._sendKeystrokeToIT100('#')
				self._sendKeystrokeToIT100('#')
			except Exception:
				pass
		finally:
			self.bulkLabelReadInProgress = False
			self.bulkOperationKind = None
			try:
				self._logZoneOutcomes(displayLabel)
			except Exception:
				pass

	def _runZoneReadPass(self, todo, installerCode, displayLabel, passLabel, interZoneDelay):
		"""Run ONE pass over `todo` (list of (zoneNumber, devId, devName)
		tuples) calling _runZoneLabelRead for each. Determines per-zone
		success by snapshotting the outcomes list before each zone and
		checking what (if anything) the read recorded.

		Returns (failedTodo, cancelled):
		  - failedTodo: list of (zoneNumber, devId, devName) tuples whose
		    read did NOT record a 'Read SUCCESS' outcome this pass. Goes
		    to the next retry pass.
		  - cancelled: True if cancel was honoured mid-pass; the outer
		    worker will not start another pass.

		Does NOT manage the bulkLabelReadInProgress flag or the outcomes
		lifecycle — the OUTER multi-pass coordinator owns those.
		"""
		failedTodo = []
		cancelled = False
		totalInPass = len(todo)
		for i, (zoneNumber, devId, devName) in enumerate(todo, start=1):
			# Honour cancel BEFORE starting each zone. The
			# currently-in-progress zone (if any) has already
			# finished by this point in the loop.
			if self.bulkOperationCancelRequested:
				cancelled = True
				self._alwaysLog(
					f"{displayLabel} ({passLabel}): CANCEL honoured before "
					f"starting zone {zoneNumber:03d} ({devName}). Stopping "
					f"the pass."
				)
				# Remaining un-attempted zones (including this one) are
				# treated as "not attempted" by the outer worker — don't
				# add them to failedTodo because we won't retry them.
				break

			self._alwaysLog(
				f"{displayLabel} ({passLabel}) [{i}/{totalInPass}]: starting "
				f"zone {zoneNumber:03d} ({devName})..."
			)

			# Snapshot outcomes-list length so we can tell what (if
			# anything) this zone's read recorded.
			outcomesBefore = len(self.bulkOperationOutcomes or [])

			try:
				# _runZoneLabelRead is a regular method that returns when its
				# state machine completes (or aborts via emergency backout).
				# It handles its own logging, its own pluginProps write, and
				# records its own outcome to self.bulkOperationOutcomes.
				self._runZoneLabelRead(devId, devName, zoneNumber, installerCode, manageOutcomes=False)
			except Exception as err:
				self._alwaysLog(
					f"{displayLabel} ({passLabel}) [{i}/{totalInPass}]: zone "
					f"{zoneNumber:03d} ({devName}) raised an unexpected "
					f"exception: {err}. Continuing with the next zone.",
					isError=True,
				)
				# Ensure an outcome IS recorded so the summary count matches
				# reality (the exception path didn't record one itself).
				try:
					self._recordZoneOutcome(
						devId, "Read FAIL",
						details=f"unexpected exception: {err}",
					)
				except Exception:
					pass
				# Try to back out of installer mode in case the read left
				# the panel stuck somewhere.
				try:
					self._sendKeystrokeToIT100('#')
					self._sendKeystrokeToIT100('#')
					self._sendKeystrokeToIT100('#')
					self._sendKeystrokeToIT100('#')
					time.sleep(2.0)
				except Exception:
					pass

			# Determine pass outcome by inspecting what was recorded.
			# The zone may have produced multiple outcome entries (rare
			# but possible) — we look at the LAST one added during this
			# zone's attempt to decide success vs failure.
			outcomesNow = self.bulkOperationOutcomes or []
			zoneOutcomeStr = None
			for entry in reversed(outcomesNow[outcomesBefore:]):
				if entry.get('devId') == devId:
					zoneOutcomeStr = entry.get('outcome') or ''
					break

			if zoneOutcomeStr is None or not zoneOutcomeStr.startswith('Read SUCCESS'):
				# Failed this pass — queue for retry.
				failedTodo.append((zoneNumber, devId, devName))
				# If NO outcome was recorded at all, defensively record one
				# now so the summary line count and the recorded outcomes
				# stay in sync.
				if zoneOutcomeStr is None:
					try:
						self._recordZoneOutcome(
							devId, "Read FAIL",
							details="no outcome recorded by zone state machine (defensive)",
						)
					except Exception:
						pass

			# Cancel check IMMEDIATELY after each zone — don't wait for the
			# next iteration's pre-zone check. If the zone aborted because
			# cancel was set mid-flow, _waitForLCDPrompt returned 'cancelled'
			# and triggered the zone's emergency-backout. Stop the pass now
			# so the user sees the cancellation summary promptly instead of
			# after the inter-zone delay.
			if self.bulkOperationCancelRequested:
				cancelled = True
				self._alwaysLog(
					f"{displayLabel} ({passLabel}): CANCEL honoured after "
					f"zone {zoneNumber:03d} ({devName}). Stopping the pass."
				)
				break

			# Brief pause between zones so the panel can settle.
			# Without this the next read's stage 1 may collide with
			# lingering inbound packets from the previous read's exit.
			if i < totalInPass and interZoneDelay > 0:
				# Sleep in 1s chunks so cancel is reasonably responsive.
				delayRemaining = interZoneDelay
				while delayRemaining > 0:
					if self.bulkOperationCancelRequested:
						break
					step = min(1.0, delayRemaining)
					time.sleep(step)
					delayRemaining -= step

		return failedTodo, cancelled

	def methodBroadcastLabels(self, action):
		"""Action callback: trigger DSC section 998 to broadcast the
		labels stored on this keypad to every other PK5500 on the bus.

		Sequence: enter installer mode (*8 + installer code), navigate
		to section 998, wait for the broadcast to complete, exit
		installer mode. The broadcast itself takes ~30-90 seconds
		depending on how many keypads are on the bus.

		Same validation shape as the bulk reads. Uses the
		`bulkLabelReadInProgress` lock so other label operations can't
		fire concurrently.
		"""
		installerCode = (self.pluginPrefs.get('installerCode') or '').strip()
		if not installerCode:
			self._alwaysLog(
				"Broadcast Labels: installer code is not set in plugin "
				"config. Configure it in Plugins → DSC Alarm → Configure.",
				isError=True,
			)
			return
		if not installerCode.isdigit() or not 4 <= len(installerCode) <= 6:
			self._alwaysLog(
				"Broadcast Labels: installer code in plugin config must be "
				"4-6 digits.",
				isError=True,
			)
			return

		if not self.enableIT100:
			self._alwaysLog(
				"Broadcast Labels: IT-100 is not enabled. This operation is "
				"IT-100 only — Envisalink TPI key commands lock out in "
				"installer mode. Aborting.",
				isError=True,
			)
			return
		if not self.it100Connected:
			self._alwaysLog(
				"Broadcast Labels: IT-100 is not connected.",
				isError=True,
			)
			return

		if self.bulkLabelReadInProgress:
			self._alwaysLog(
				"Broadcast Labels: another bulk label operation is already "
				"in progress. Wait for it to finish (watch the log) before "
				"starting the broadcast.",
				isError=True,
			)
			return

		# Refuse if any partition is armed.
		notReady = []
		for partition, keypadId in self.keypadList.items():
			try:
				keypad = indigo.devices[keypadId]
			except Exception:
				continue
			state = keypad.states.get('state', 'unknown')
			if state != kAlarmStateDisarmed:
				notReady.append(f"partition {partition} is in state '{state}'")
		if notReady:
			self._alwaysLog(
				"Broadcast Labels: cannot enter installer mode — "
				+ "; ".join(notReady)
				+ ". Disarm all partitions first.",
				isError=True,
			)
			return

		self._alwaysLog(
			"Broadcast Labels: starting. Will enter installer mode, trigger "
			"section 998, wait for the broadcast to complete (~30-90s), and "
			"exit. DO NOT touch any physical keypad during this operation."
		)

		t = threading.Thread(
			target=self._runBroadcastLabels,
			args=(installerCode,),
			name="DSCAlarm-BroadcastLabels",
			daemon=True,
		)
		t.start()

	def _runBroadcastLabels(self, installerCode):
		"""Background-thread worker for methodBroadcastLabels. State
		machine matching the real PK5500 LCD prompts:

		  Stage 0 (pre-flight): send #### to back out of any latent
		    installer mode. Harmless if panel is at idle.
		  Stage 1: send *8 + installer code, wait for the LCD programming
		    entry screen: line 1 'Enter LCD', line 2 'Section ---'.
		    Retries on dropped keystrokes (same pattern as the read/write
		    paths). Watches for 'invalid access'/'invalid code' so a
		    wrong installer code fails fast.
		  Stage 2: send 998. The prompt CHANGES to line 1 'Broadcast
		    Labels', line 2 'Yes    No'. (This is a confirmation prompt —
		    the broadcast hasn't started yet.)
		  Stage 3: send * to confirm 'Yes'. The LCD changes to 'In
		    Progress' (or similar) and the panel begins the broadcast on
		    the keybus, which takes ~60s on a typical system.
		  Stage 4: wait for the broadcast to complete — LCD returns to
		    the LCD programming entry screen ('Enter LCD' / 'Section ---').
		    Max wait 120s.
		  Stage 5: send # to exit installer mode.
		  Stage 6: wait for the panel to confirm idle.

		v41 correction: previously sent 998 and assumed the broadcast
		started immediately; in reality the panel shows a Yes/No
		confirmation that must be confirmed with *. Without the confirm,
		the keystrokes after 998 would have been interpreted by the
		Yes/No prompt as something else. The plugin also previously
		waited for 'Enter Section', which doesn't match what this stage
		actually displays.
		"""
		self.bulkLabelReadInProgress = True
		self.bulkOperationKind = "Broadcast Labels"
		try:
			# --- Stage 0: pre-flight ---
			self._alwaysLog(
				"  Broadcast: pre-flight, sending #### to back out of any "
				"latent installer mode."
			)
			for _ in range(4):
				self._sendKeystrokeToIT100('#')
			time.sleep(1.5)

			# --- Stage 1: enter LCD programming ---
			# Build the keystroke sequence and try up to 4 times if the
			# panel doesn't reach 'Enter LCD' within 15s. Same retry
			# behaviour as the read/write paths.
			installerKeys = ['*', '8'] + list(installerCode)
			maxAttempts = 4
			reachedSection = False
			for attempt in range(1, maxAttempts + 1):
				if attempt > 1:
					self._alwaysLog(
						f"  Broadcast: stage 1 attempt {attempt}/{maxAttempts} "
						f"after pre-flight backout."
					)
					for _ in range(4):
						self._sendKeystrokeToIT100('#')
					time.sleep(1.5)
				self._alwaysLog(
					f"  Broadcast: stage 1 (enter LCD programming, attempt "
					f"{attempt}/{maxAttempts}): sending '*8{installerCode}'"
				)
				for k in installerKeys:
					if not self._sendKeystrokeToIT100(k):
						self._alwaysLog(
							"Broadcast Labels: IT-100 went away during "
							"installer-mode entry. Backing out.",
							isError=True,
						)
						return

				# Wait for 'Enter LCD' to appear (or timeout after 15s).
				# Failure substrings catch wrong-code rejection so we fail
				# fast instead of retrying with a code the panel rejected.
				status, matched = self._waitForLCDPrompt(
					expected_substrings=['enter lcd'],
					timeout_seconds=15.0,
					failure_substrings=['invalid access', 'invalid code'],
					prefix=f"stage 1 (enter installer) attempt {attempt}",
				)
				if status == 'failure':
					self._alwaysLog(
						f"Broadcast Labels: stage 1 — panel rejected the "
						f"installer code ({matched}). Aborting (no point "
						f"retrying with the same code).",
						isError=True,
					)
					self._broadcastEmergencyExit()
					return
				if status == 'success':
					reachedSection = True
					break
				# 'timeout' — could be a dropped keystroke; check if panel
				# reverted to idle and retry from scratch.
				snap = self._snapshotKeypadLCD()
				l1 = ''
				for partition, (line1, _l2) in snap.items():
					if line1:
						l1 = line1
						break
				if l1 and 'date' in l1.lower():
					self._alwaysLog(
						f"  Broadcast: stage 1 — panel reverted to idle "
						f"({l1!r}). Likely a dropped keystroke during "
						f"installer-mode entry."
					)
					# loop will retry

			if not reachedSection:
				self._alwaysLog(
					f"Broadcast Labels: could not reach LCD programming "
					f"entry screen after {maxAttempts} attempts. Aborting.",
					isError=True,
				)
				self._broadcastEmergencyExit()
				return

			# --- Stage 2: send 998, wait for Yes/No confirmation ---
			self._alwaysLog(
				"  Broadcast: stage 2 — sending 998 to bring up the "
				"'Broadcast Labels — Yes/No' confirmation prompt."
			)
			for k in '998':
				if not self._sendKeystrokeToIT100(k):
					self._alwaysLog(
						"Broadcast Labels: IT-100 went away while sending "
						"998. Backing out.",
						isError=True,
					)
					self._broadcastEmergencyExit()
					return

			status, matched = self._waitForLCDPrompt(
				expected_substrings=['broadcast'],
				timeout_seconds=10.0,
				failure_substrings=['invalid'],
				prefix="stage 2 (998 confirmation)",
			)
			if status != 'success':
				self._alwaysLog(
					f"Broadcast Labels: stage 2 — didn't see the 'Broadcast "
					f"Labels' confirmation prompt within 10s (status="
					f"{status}, matched={matched}). The panel may not be on "
					f"the LCD programming menu, or 998 was not accepted. "
					f"Aborting.",
					isError=True,
				)
				self._broadcastEmergencyExit()
				return

			# --- Stage 3: confirm with * (selects 'Yes') ---
			self._alwaysLog(
				"  Broadcast: stage 3 — sending * to confirm 'Yes' on the "
				"Broadcast Labels prompt."
			)
			if not self._sendKeystrokeToIT100('*'):
				self._alwaysLog(
					"Broadcast Labels: IT-100 went away while confirming. "
					"Backing out.",
					isError=True,
				)
				self._broadcastEmergencyExit()
				return

			# Wait briefly for 'In Progress' (or similar) to appear so we
			# can be sure the broadcast actually started. Don't fail hard
			# if we miss the transient — some firmware may skip it or
			# show different wording — but log loudly if we don't see it.
			status, matched = self._waitForLCDPrompt(
				expected_substrings=['in progress', 'progress'],
				timeout_seconds=10.0,
				failure_substrings=None,
				prefix="stage 3 (broadcast in progress)",
			)
			if status == 'success':
				self._alwaysLog(
					f"  Broadcast: stage 3 — broadcast started ({matched})."
				)
			else:
				self._alwaysLog(
					f"  Broadcast: stage 3 — didn't see an 'In Progress' "
					f"transient within 10s (status={status}). Proceeding to "
					f"wait for completion anyway — the broadcast may have "
					f"started silently or with different wording on this "
					f"firmware."
				)

			# --- Stage 4: wait for broadcast to complete ---
			# Completion = LCD returns to the LCD programming entry screen
			# ('Enter LCD' / 'Section ---'). Typical 60s on a small bus,
			# allow up to 180s for larger systems.
			self._alwaysLog(
				"  Broadcast: stage 4 — waiting for broadcast to complete "
				"(typically ~60s, up to 180s). Other keypads will show "
				"transient messages during this time. DO NOT touch any "
				"keypad."
			)
			status, matched = self._waitForLCDPrompt(
				expected_substrings=['enter lcd'],
				timeout_seconds=180.0,
				failure_substrings=None,
				prefix="stage 4 (broadcast complete)",
				poll_interval=1.0,
			)
			if status == 'success':
				self._alwaysLog(
					f"  Broadcast: stage 4 complete — LCD returned to "
					f"'Enter LCD / Section ---' ({matched})."
				)
			else:
				self._alwaysLog(
					"Broadcast Labels: stage 4 timed out after 180s waiting "
					"for the broadcast to finish. The broadcast may still "
					"be in progress on the keybus — proceeding to exit "
					"installer mode anyway.",
					isError=True,
				)

			# --- Stage 5: exit installer mode ---
			self._alwaysLog(
				"  Broadcast: stage 5 — sending # to exit installer mode."
			)
			self._sendKeystrokeToIT100('#')

			# --- Stage 6: wait for idle confirmation ---
			status, matched = self._waitForLCDPrompt(
				expected_substrings=['date', 'secure system', 'ready to arm', 'disarmed'],
				timeout_seconds=10.0,
				failure_substrings=None,
				prefix="stage 6 (back to idle)",
			)
			if status == 'success':
				self._alwaysLog(
					f"  Broadcast: stage 6 — panel back at idle ({matched})."
				)

			self._alwaysLog(
				"Broadcast Labels: SUCCESS — section 998 triggered and "
				"confirmed. Labels have been pushed to all PK5500 keypads "
				"on the bus. Other keypads should now show the updated "
				"zone labels."
			)
		except Exception as err:
			self._alwaysLog(
				f"Broadcast Labels: unexpected error in worker: {err}. "
				f"Sending #### to back out of installer mode if stuck.",
				isError=True,
			)
			self._broadcastEmergencyExit()
		finally:
			self.bulkLabelReadInProgress = False
			self.bulkOperationKind = None

	def _broadcastEmergencyExit(self):
		"""Best-effort cleanup if the broadcast worker goes wrong: send
		several # keystrokes to back out of installer mode regardless of
		current panel state.
		"""
		try:
			for _ in range(4):
				self._sendKeystrokeToIT100('#')
		except Exception:
			pass

	def methodWriteAllZoneLabels(self, action):
		"""Action callback: write the label for EVERY configured zone
		device to the panel. See `_kickOffBulkLabelWrite` for the shared
		implementation.
		"""
		cfg = self._parseRetryConfig(action)
		self._kickOffBulkLabelWrite(
			zoneFilter='all',
			zoneSpec=None,
			displayLabel="Write All Zone Labels",
			retryCfg=cfg,
		)

	def methodWriteSpecificZoneLabels(self, action):
		"""Action callback: write labels for the zones specified in the
		action's `zoneList` field (e.g. '1,3,5-10,15'). See
		`_kickOffBulkLabelWrite` for the shared implementation.
		"""
		# Action callbacks get the action's ConfigUI values via action.props.
		# Use getattr with a fallback to be safe across Indigo versions.
		props = getattr(action, 'props', None) or {}
		zoneSpec = (props.get('zoneList') or '').strip()
		cfg = self._parseRetryConfig(action)
		self._kickOffBulkLabelWrite(
			zoneFilter='specific',
			zoneSpec=zoneSpec,
			displayLabel="Write Specific Zone Labels",
			retryCfg=cfg,
		)

	def methodWriteChangedZoneLabels(self, action):
		"""Action callback: write labels only for zones whose labelText
		differs from lastSyncedLabelText. Zones that have never been
		synced (no lastSyncedLabelText prop) are skipped — the user must
		do an initial sync via 'Read All' or 'Write All' first.
		"""
		cfg = self._parseRetryConfig(action)
		self._kickOffBulkLabelWrite(
			zoneFilter='changed',
			zoneSpec=None,
			displayLabel="Write Changed Zone Labels",
			retryCfg=cfg,
		)

	def _kickOffBulkLabelWrite(self, zoneFilter, zoneSpec, displayLabel, retryCfg=None):
		"""Shared implementation for the three bulk-write actions.
		`zoneFilter` is one of 'all', 'specific', 'changed'. `zoneSpec`
		is the comma/range zone-list string when zoneFilter='specific',
		otherwise ignored. `displayLabel` is used in log messages.

		Validates pre-conditions (IT-100 enabled/connected, partitions
		disarmed, installer code set, no other bulk op in progress),
		builds the todo list according to zoneFilter, then spawns a
		single background worker thread that calls _runZoneLabelWrite
		for each todo entry sequentially.
		"""
		installerCode = (self.pluginPrefs.get('installerCode') or '').strip()
		if not installerCode:
			self._alwaysLog(
				f"{displayLabel}: installer code is not set in plugin "
				f"config. Configure it in Plugins → DSC Alarm → Configure.",
				isError=True,
			)
			return
		if not installerCode.isdigit() or not 4 <= len(installerCode) <= 6:
			self._alwaysLog(
				f"{displayLabel}: installer code in plugin config must be "
				f"4-6 digits.",
				isError=True,
			)
			return

		if not self.enableIT100:
			self._alwaysLog(
				f"{displayLabel}: IT-100 is not enabled. This operation is "
				f"IT-100 only — Envisalink TPI key commands lock out in "
				f"installer mode. Aborting.",
				isError=True,
			)
			return
		if not self.it100Connected:
			self._alwaysLog(
				f"{displayLabel}: IT-100 is not connected.",
				isError=True,
			)
			return

		if self.bulkLabelReadInProgress:
			self._alwaysLog(
				f"{displayLabel}: another bulk label operation is already "
				f"in progress. Wait for it to finish (watch the log) before "
				f"starting another.",
				isError=True,
			)
			return

		# Refuse if any partition is armed.
		notReady = []
		for partition, keypadId in self.keypadList.items():
			try:
				keypad = indigo.devices[keypadId]
			except Exception:
				continue
			state = keypad.states.get('state', 'unknown')
			if state != kAlarmStateDisarmed:
				notReady.append(f"partition {partition} is in state '{state}'")
		if notReady:
			self._alwaysLog(
				f"{displayLabel}: cannot enter installer mode — "
				+ "; ".join(notReady)
				+ ". Disarm all partitions first.",
				isError=True,
			)
			return

		# Build the candidate zone list based on filter.
		# 'all': every configured zone
		# 'specific': zones from zoneSpec, intersected with configured zones
		# 'changed': configured zones where labelText != lastSyncedLabelText
		if zoneFilter == 'specific':
			parsedZones, parseErrors = self._parseZoneRangeSpec(zoneSpec)
			if parseErrors:
				self._alwaysLog(
					f"{displayLabel}: problems with zone list {zoneSpec!r}:",
					isError=True,
				)
				for err in parseErrors:
					self._alwaysLog(f"  {err}", isError=True)
				if not parsedZones:
					self._alwaysLog(
						f"{displayLabel}: no usable zones in the list. "
						f"Aborting.",
						isError=True,
					)
					return
				self._alwaysLog(
					f"{displayLabel}: proceeding with the valid subset: "
					f"{parsedZones}"
				)
			candidateZones = parsedZones
		else:
			# 'all' or 'changed' — every configured zone is a candidate.
			candidateZones = sorted(self.zoneList.keys())

		# Materialise into (zoneNumber, devId, devName, labelToWrite) and
		# apply per-filter skip logic.
		todo = []
		skippedNoDevice = 0
		skippedNoLabel = 0
		skippedUnchanged = 0
		skippedNeverSynced = 0
		for zoneNumber in candidateZones:
			devId = self.zoneList.get(zoneNumber)
			if devId is None:
				skippedNoDevice += 1
				continue
			try:
				dev = indigo.devices[devId]
			except Exception:
				skippedNoDevice += 1
				continue

			# Decide the label text for this zone. Same logic as the
			# per-zone write path: labelText if non-empty, else device
			# name, optionally prefixed with the 2-digit zone number.
			props = dev.pluginProps
			labelTextRaw = (props.get('labelText') or '').strip()
			labelText = labelTextRaw or dev.name
			if self._propIsTrue(props, 'labelPrefixZoneNumber', default=False):
				labelText = f"{zoneNumber:02d} {labelText}"

			if not labelText:
				skippedNoLabel += 1
				continue

			if zoneFilter == 'changed':
				lastSynced = props.get('lastSyncedLabelText')
				if lastSynced is None:
					skippedNeverSynced += 1
					continue
				# Compare both stripped so trailing whitespace differences
				# don't trigger a write.
				if labelTextRaw.strip() == (lastSynced or '').strip():
					skippedUnchanged += 1
					continue

			todo.append((zoneNumber, devId, dev.name, labelText))

		# Build a skip-summary line so the user can see what got filtered.
		skipNotes = []
		if skippedNoDevice:
			skipNotes.append(f"{skippedNoDevice} not configured in Indigo")
		if skippedNoLabel:
			skipNotes.append(f"{skippedNoLabel} with no label text")
		if skippedNeverSynced:
			skipNotes.append(f"{skippedNeverSynced} never synced (no lastSyncedLabelText)")
		if skippedUnchanged:
			skipNotes.append(f"{skippedUnchanged} unchanged since last sync")
		skipSummary = (" (skipped: " + ", ".join(skipNotes) + ")") if skipNotes else ""

		if not todo:
			if zoneFilter == 'changed' and skippedNeverSynced:
				self._alwaysLog(
					f"{displayLabel}: nothing to write{skipSummary}. To "
					f"establish a baseline for 'changed' detection, run "
					f"'Read All Zone Labels' or 'Write All Zone Labels' "
					f"first."
				)
			elif zoneFilter == 'changed':
				self._alwaysLog(
					f"{displayLabel}: nothing to write — all configured "
					f"zones are already in sync with the panel{skipSummary}."
				)
			else:
				self._alwaysLog(
					f"{displayLabel}: nothing to write{skipSummary}.",
					isError=True,
				)
			return

		# Per-zone writes take roughly 60-90 seconds each — call it 75s
		# average for the estimate.
		estimatedMinutes = max(1, int((len(todo) * 75) / 60))
		self._alwaysLog(
			f"{displayLabel}: starting bulk write of {len(todo)} "
			f"zone(s){skipSummary}. Estimated total runtime "
			f"~{estimatedMinutes} minute(s). DO NOT touch any physical "
			f"keypad or open any zone's config dialog during this "
			f"operation. Watch the event log for per-zone progress. "
			f"When it finishes, you'll typically want to run "
			f"'Broadcast Labels to All Keypads' to push the changes to "
			f"all keypads."
		)

		# Clear any stale cancel request from a prior run.
		self.bulkOperationCancelRequested = False
		# Defensive default if no retry config was passed.
		if retryCfg is None:
			retryCfg = {'retryPasses': 2, 'interPassDelaySeconds': 15, 'interZoneDelaySeconds': 5}
		t = threading.Thread(
			target=self._runBulkZoneLabelWrite,
			args=(todo, installerCode, displayLabel, retryCfg),
			name="DSCAlarm-BulkZoneLabelWrite",
			daemon=True,
		)
		t.start()

	def _runBulkZoneLabelWrite(self, todo, installerCode, displayLabel="Bulk Write", retryCfg=None):
		"""Background-thread worker for the bulk-write actions. Calls
		the per-zone write state machine for each (zoneNumber, devId,
		devName, labelToWrite) tuple in `todo`, serially.

		Each individual write does its own pre-flight, installer-mode
		entry, navigation, per-character verification + retry, save,
		and clean exit. Failures on one zone don't poison the rest —
		the loop continues with the next zone.

		Multi-pass retry: after the initial pass, any zones that failed
		(determined by checking the outcomes recorded by the per-zone
		write state machine) are retried in subsequent passes.
		`retryCfg['retryPasses']` controls how many EXTRA passes are made
		(0 = no retry, just the initial pass; 2 = up to 3 total attempts
		per zone). Between passes we wait `retryCfg['interPassDelaySeconds']`
		to let the panel fully recover from installer mode. Between zones
		within a single pass we wait `retryCfg['interZoneDelaySeconds']`
		(avoids the post-installer 'System is Unavailable' transient
		state interfering with the next zone's installer-mode entry).

		Success/failure counts are derived from the FINAL outcome per
		devId (collapsed by _logZoneOutcomes), not pass-by-pass tallies —
		so a zone that failed pass 1 and succeeded pass 2 counts as ONE
		success in the COMPLETE summary line.

		On each successful per-zone write, the device's labelText AND
		lastSyncedLabelText are updated by `_runZoneLabelWrite` itself —
		we don't need to do that here.
		"""
		if retryCfg is None:
			retryCfg = {'retryPasses': 2, 'interPassDelaySeconds': 15, 'interZoneDelaySeconds': 5}
		retryPasses = retryCfg.get('retryPasses', 2)
		interPassDelay = retryCfg.get('interPassDelaySeconds', 15)
		interZoneDelay = retryCfg.get('interZoneDelaySeconds', 5)

		self.bulkLabelReadInProgress = True
		self.bulkOperationKind = displayLabel
		self._beginZoneOutcomes()
		try:
			totalZones = len(todo)
			cancelled = False
			pendingTodo = list(todo)  # zones still needing an attempt
			passNumber = 0  # 0 = initial pass, 1+ = retry passes
			passesRun = 0   # count of passes actually executed

			while pendingTodo:
				passLabel = "initial pass" if passNumber == 0 else f"retry pass {passNumber}"
				if passNumber > 0:
					self._alwaysLog(
						f"{displayLabel}: starting {passLabel} — "
						f"{len(pendingTodo)} zone(s) failed previous pass and "
						f"will be retried."
					)

				passFailedTodo, passCancelled = self._runZoneWritePass(
					pendingTodo, installerCode, displayLabel, passLabel, interZoneDelay
				)
				passesRun += 1

				if passCancelled:
					cancelled = True
					break

				if not passFailedTodo:
					# All zones in this pass succeeded — we're done.
					break

				# Some zones failed. Are we allowed another retry pass?
				if passNumber >= retryPasses:
					# Out of retry budget — leave the failures as-is for
					# the final summary to report.
					break

				# Otherwise, retry the failed zones in the next pass after
				# the inter-pass delay (lets the panel fully recover from
				# installer mode). Honour cancel during the delay.
				self._alwaysLog(
					f"{displayLabel}: {passLabel} complete with "
					f"{len(passFailedTodo)} failure(s). Waiting "
					f"{interPassDelay}s for the panel to recover before "
					f"retry pass {passNumber + 1}."
				)
				delayRemaining = interPassDelay
				while delayRemaining > 0:
					if self.bulkOperationCancelRequested:
						cancelled = True
						break
					step = min(1.0, delayRemaining)
					time.sleep(step)
					delayRemaining -= step
				if cancelled:
					self._alwaysLog(
						f"{displayLabel}: CANCEL honoured during inter-pass "
						f"delay. Stopping the bulk run."
					)
					break

				pendingTodo = passFailedTodo
				passNumber += 1

			# Compute final counts from outcomes (collapsed by devId).
			# Any devId whose LAST outcome string starts with "Write SUCCESS"
			# is a success; everything else is a failure. Zones that never
			# got an outcome (e.g. cancelled before being attempted) count
			# as not-attempted.
			finalOutcomeByDevId = {}
			for o in (self.bulkOperationOutcomes or []):
				finalOutcomeByDevId[o.get('devId')] = (o.get('outcome') or '')
			successes = sum(
				1 for outcome in finalOutcomeByDevId.values()
				if outcome.startswith('Write SUCCESS')
			)
			failures = sum(
				1 for outcome in finalOutcomeByDevId.values()
				if not outcome.startswith('Write SUCCESS')
			)
			notAttempted = totalZones - len(finalOutcomeByDevId)

			passSuffix = "" if passesRun == 1 else f" (across {passesRun} pass(es))"
			if cancelled:
				self._alwaysLog(
					f"{displayLabel}: CANCELLED. {successes}/{totalZones} "
					f"zone(s) written successfully before cancellation"
					+ (f", {failures} failed" if failures else "")
					+ (f", {notAttempted} not attempted" if notAttempted else "")
					+ f"{passSuffix}. "
					f"To resume, you can use 'Write Changed Zone Labels' "
					f"(which will pick up only the zones whose labelText "
					f"still differs from the panel) or trigger this action "
					f"again."
				)
			else:
				self._alwaysLog(
					f"{displayLabel}: COMPLETE. {successes}/{totalZones} "
					f"zone(s) written successfully"
					+ (f", {failures} failed" if failures else "")
					+ f"{passSuffix}"
					+ ". To push the new labels to all PK5500 keypads on the "
					f"system, run 'Broadcast Labels to All Keypads' (give the "
					f"panel ~30 seconds to recover from installer mode first)."
				)
		except Exception as err:
			self._alwaysLog(
				f"{displayLabel}: unexpected error in bulk worker: {err}. "
				f"Sending #### to back out of installer mode if stuck.",
				isError=True,
			)
			try:
				for _ in range(4):
					self._sendKeystrokeToIT100('#')
			except Exception:
				pass
		finally:
			self.bulkLabelReadInProgress = False
			self.bulkOperationKind = None
			try:
				self._logZoneOutcomes(displayLabel)
			except Exception:
				pass

	def _runZoneWritePass(self, todo, installerCode, displayLabel, passLabel, interZoneDelay):
		"""Run ONE pass over `todo` (list of (zoneNumber, devId, devName,
		labelText) tuples) calling _runZoneLabelWrite for each. Determines
		per-zone success by snapshotting the outcomes list before each
		zone and checking what (if anything) the write recorded.

		Returns (failedTodo, cancelled):
		  - failedTodo: list of tuples whose write did NOT record a
		    'Write SUCCESS' outcome this pass. Goes to the next retry
		    pass.
		  - cancelled: True if cancel was honoured mid-pass; the outer
		    worker will not start another pass.

		Does NOT manage the bulkLabelReadInProgress flag or the outcomes
		lifecycle — the OUTER multi-pass coordinator owns those.

		Bug fixes vs the previous flat-loop version:
		  1. Success/fail decision is based on the OUTCOME recorded by
		     the per-zone write state machine, not whether the function
		     returned without raising. Previously a write that hit
		     emergency-backout returned normally and was counted as a
		     success even though it recorded a FAIL outcome — that's
		     how a 26-fail run could log '27/27 successfully'.
		  2. The normalised-empty branch now records a FAIL outcome
		     (previously it just incremented a local counter — the
		     outcomes list never got the entry, so the summary line
		     count didn't match reality).
		  3. The unexpected-exception catch now records a FAIL outcome
		     too (same reason).
		"""
		failedTodo = []
		cancelled = False
		totalInPass = len(todo)
		for i, (zoneNumber, devId, devName, labelText) in enumerate(todo, start=1):
			# Honour cancel BEFORE starting each zone. The
			# currently-in-progress zone (if any) has already
			# finished by this point in the loop.
			if self.bulkOperationCancelRequested:
				cancelled = True
				self._alwaysLog(
					f"{displayLabel} ({passLabel}): CANCEL honoured before "
					f"starting zone {zoneNumber:03d} ({devName}). Stopping "
					f"the pass."
				)
				# Remaining un-attempted zones (including this one) are
				# treated as "not attempted" by the outer worker — don't
				# add them to failedTodo because we won't retry them.
				break

			self._alwaysLog(
				f"{displayLabel} ({passLabel}) [{i}/{totalInPass}]: starting "
				f"zone {zoneNumber:03d} ({devName}) → {labelText!r}..."
			)

			# Snapshot outcomes-list length so we can tell what (if
			# anything) this zone's write recorded.
			outcomesBefore = len(self.bulkOperationOutcomes or [])

			try:
				segments, normalised, preview = self._buildZoneLabelProgrammingSequence(
					zoneNumber, installerCode, labelText
				)
				if not normalised:
					self._alwaysLog(
						f"{displayLabel} ({passLabel}) [{i}/{totalInPass}]: "
						f"zone {zoneNumber:03d} ({devName}) label "
						f"{labelText!r} normalised to empty — no typeable "
						f"characters. Skipping this zone.",
						isError=True,
					)
					# Record outcome so the summary count matches reality.
					try:
						self._recordZoneOutcome(
							devId, "Write FAIL",
							details=f"label {labelText!r} normalised to empty (no typeable chars)",
						)
					except Exception:
						pass
				else:
					# Call the per-zone write worker synchronously.
					# It returns when its state machine completes.
					self._runZoneLabelWrite(
						devId, devName, zoneNumber, segments, normalised, preview,
						manageOutcomes=False
					)
			except Exception as err:
				self._alwaysLog(
					f"{displayLabel} ({passLabel}) [{i}/{totalInPass}]: zone "
					f"{zoneNumber:03d} ({devName}) raised an unexpected "
					f"exception: {err}. Continuing with the next zone.",
					isError=True,
				)
				# Record outcome so the summary count matches reality
				# (the exception path didn't record one itself).
				try:
					self._recordZoneOutcome(
						devId, "Write FAIL",
						details=f"unexpected exception: {err}",
					)
				except Exception:
					pass
				# Try to back out of installer mode in case the write left
				# the panel stuck somewhere.
				try:
					for _ in range(4):
						self._sendKeystrokeToIT100('#')
					time.sleep(2.0)
				except Exception:
					pass

			# Determine pass outcome by inspecting what was recorded.
			# The zone may have produced multiple outcome entries (rare
			# but possible) — we look at the LAST one added during this
			# zone's attempt to decide success vs failure.
			outcomesNow = self.bulkOperationOutcomes or []
			zoneOutcomeStr = None
			for entry in reversed(outcomesNow[outcomesBefore:]):
				if entry.get('devId') == devId:
					zoneOutcomeStr = entry.get('outcome') or ''
					break

			if zoneOutcomeStr is None or not zoneOutcomeStr.startswith('Write SUCCESS'):
				# Failed this pass — queue for retry.
				failedTodo.append((zoneNumber, devId, devName, labelText))
				# If NO outcome was recorded at all, defensively record one
				# now so the summary line count and the recorded outcomes
				# stay in sync.
				if zoneOutcomeStr is None:
					try:
						self._recordZoneOutcome(
							devId, "Write FAIL",
							details="no outcome recorded by zone state machine (defensive)",
						)
					except Exception:
						pass

			# Cancel check IMMEDIATELY after each zone — don't wait for the
			# next iteration's pre-zone check. If the zone aborted because
			# cancel was set mid-flow, _waitForLCDPrompt returned 'cancelled'
			# and triggered the zone's emergency-backout. Stop the pass now
			# so the user sees the cancellation summary promptly instead of
			# after the inter-zone delay.
			if self.bulkOperationCancelRequested:
				cancelled = True
				self._alwaysLog(
					f"{displayLabel} ({passLabel}): CANCEL honoured after "
					f"zone {zoneNumber:03d} ({devName}). Stopping the pass."
				)
				break

			# Brief pause between zones so the panel can settle back to
			# idle (and avoid the post-installer 'System is Unavailable'
			# transient state interfering with the next zone's
			# installer-mode entry). Sleep in 1s chunks so cancel is
			# reasonably responsive.
			if i < totalInPass and interZoneDelay > 0:
				delayRemaining = interZoneDelay
				while delayRemaining > 0:
					if self.bulkOperationCancelRequested:
						break
					step = min(1.0, delayRemaining)
					time.sleep(step)
					delayRemaining -= step

		return failedTodo, cancelled

	def writeZoneLabelToKeypad(self, valuesDict, typeId, devId):
		"""Device-config button callback. Triggers either a dry-run or a real
		write of the Indigo device name to the corresponding DSC zone label.

		PluginConfig/Devices.xml <Field type="button"> callbacks MUST return
		valuesDict (or nothing) — returning a tuple triggers the same CXmlDict
		converter error that bit us on the test-connection buttons.
		"""
		try:
			dev = indigo.devices[devId]
		except Exception:
			self._alwaysLog(
				"Zone label write: could not look up device — save the zone "
				"first so it has an Indigo ID, then try again.",
				isError=True,
			)
			return valuesDict

		# --- Validate device state ---
		zoneNumberRaw = valuesDict.get('zoneNumber', '')
		if not zoneNumberRaw:
			self._alwaysLog(
				f"Zone label write ({dev.name}): no Zone Number set — "
				f"select a zone first, save the device, then try again.",
				isError=True,
			)
			return valuesDict
		try:
			zoneNumber = int(zoneNumberRaw)
		except (TypeError, ValueError):
			self._alwaysLog(
				f"Zone label write ({dev.name}): zone number '{zoneNumberRaw}' is not a valid integer.",
				isError=True,
			)
			return valuesDict
		if not 1 <= zoneNumber <= 64:
			self._alwaysLog(
				f"Zone label write ({dev.name}): zone number {zoneNumber} out of range (1-64).",
				isError=True,
			)
			return valuesDict

		# Installer code lives in plugin-wide prefs, not on each zone — it's a
		# per-panel value, not a per-zone one. Read it from pluginPrefs.
		installerCode = (self.pluginPrefs.get('installerCode') or '').strip()
		if not installerCode:
			self._alwaysLog(
				f"Zone label write ({dev.name}): installer code is not set. "
				f"Open Plugins → DSC Alarm → Configure and enter your installer code.",
				isError=True,
			)
			return valuesDict
		if not installerCode.isdigit() or not 4 <= len(installerCode) <= 6:
			self._alwaysLog(
				f"Zone label write ({dev.name}): installer code in plugin config "
				f"must be 4-6 digits. Open Plugins → DSC Alarm → Configure to fix.",
				isError=True,
			)
			return valuesDict

		isRealRun = self._propIsTrue(valuesDict, 'labelConfirmedWrite', default=False)

		# Determine the label text. Priority:
		#   1. User-entered 'Label Text' field (if non-empty after strip)
		#   2. Falls back to the Indigo device name
		# Then optionally prefix with the 2-digit zone number per the
		# 'Prefix with zone number' checkbox.
		labelTextRaw = (valuesDict.get('labelText') or '').strip()
		labelText = labelTextRaw or dev.name
		if self._propIsTrue(valuesDict, 'labelPrefixZoneNumber', default=False):
			labelText = f"{zoneNumber:02d} {labelText}"

		# --- Build the keystroke sequence (always done, even for dry-run) ---
		segments, normalised, preview = self._buildZoneLabelProgrammingSequence(
			zoneNumber, installerCode, labelText
		)
		# Flatten for total-keystroke counts and the human-readable summary.
		# Segments are not joined with separators — they represent contiguous
		# bursts on the keybus.
		allKeys = [k for (_name, keys) in segments for k in keys]
		humanReadable = ''.join(allKeys)

		mode = "REAL WRITE" if isRealRun else "DRY RUN"
		self._alwaysLog(f"Zone label write ({dev.name}) — {mode}")
		self._alwaysLog(f"  Source text      : {labelText!r}")
		self._alwaysLog(f"  Normalised label : {normalised!r} (will appear as {preview!r})")
		self._alwaysLog(f"  Zone section     : {zoneNumber:03d}")
		self._alwaysLog(f"  Total keystrokes : {len(allKeys)}")
		self._alwaysLog(f"  Sequence         : {humanReadable}")
		# Also log each segment so the user can see what each stage does.
		for (segName, segKeys) in segments:
			self._alwaysLog(f"    {segName:<18}: {''.join(segKeys)!r}  ({len(segKeys)} keys)")

		if not normalised:
			self._alwaysLog(
				f"Zone label write ({dev.name}): the label text produced an empty "
				f"result after normalisation (no A-Z, a-z, 0-9, or space characters). "
				f"Edit the Label Text field with something typeable.",
				isError=True,
			)
			return valuesDict

		if not isRealRun:
			self._alwaysLog(
				"  ↑ DRY RUN — nothing was sent. Review the above, then "
				"tick 'I have read the above and want to write for real' "
				"in this device's config and press the button again."
			)
			return valuesDict

		# --- Real-run safety checks ---
		if not self.enableIT100:
			self._alwaysLog(
				f"Zone label write ({dev.name}): IT-100 is not enabled in plugin config. "
				f"This operation is IT-100 only — Envisalink would lock the panel into "
				f"installer mode with no escape. Aborting.",
				isError=True,
			)
			return valuesDict
		if not self.it100Connected:
			self._alwaysLog(
				f"Zone label write ({dev.name}): IT-100 is not connected. "
				f"Cannot send keystrokes. Aborting.",
				isError=True,
			)
			return valuesDict

		if self.bulkLabelReadInProgress:
			self._alwaysLog(
				f"Zone label write ({dev.name}): a bulk 'Read All Zone "
				f"Labels' operation is currently in progress — refusing "
				f"to start a write at the same time. Wait for the bulk "
				f"read to finish (watch the log), then try again.",
				isError=True,
			)
			return valuesDict

		# Check every keypad's state. If ANY partition is not disarmed/ready,
		# refuse — entering installer mode while armed would be catastrophic.
		notReady = []
		for partition, keypadId in self.keypadList.items():
			try:
				keypad = indigo.devices[keypadId]
			except Exception:
				continue
			state = keypad.states.get('state', 'unknown')
			if state != kAlarmStateDisarmed:
				notReady.append(f"partition {partition} is in state '{state}'")
		if notReady:
			self._alwaysLog(
				f"Zone label write ({dev.name}): cannot enter installer mode — "
				+ "; ".join(notReady)
				+ ". Disarm all partitions first.",
				isError=True,
			)
			return valuesDict

		# Flush any leftover keystrokes from a previous (failed?) operation so
		# we don't accidentally mix with stale state. Atomic-replace pattern
		# to avoid a race with processIT100CommandQueue.
		oldQueue = self.it100TxCmdList
		self.it100TxCmdList = []
		if oldQueue:
			self._alwaysLog(
				f"Zone label write ({dev.name}): flushed {len(oldQueue)} stale "
				f"keystrokes from the IT-100 queue before starting."
			)

		# Snapshot is unnecessary here — _watchKeypadLCD below will log its own
		# t=0 baseline when we start watching, which serves the same purpose.

		# --- Queue the keystrokes ---
		# Each keystroke becomes (070X, 070^) so it's two packets in the
		# IT-100 queue. With the v23+ throttle of ~0.2s between presses,
		# total time is roughly 0.3s per keystroke. Per-char verification
		# adds ~2.5s wait after each of the 14 characters, so that's ~35s
		# on top of the raw TX time.
		estimatedSeconds = int(len(allKeys) * 0.3) + 14 * 2 + 15
		self._alwaysLog(
			f"Zone label write ({dev.name}): starting in background. Will send "
			f"{len(allKeys)} keystrokes in {len(segments)} segments, with "
			f"per-character LCD verification on the label-text segment. "
			f"Estimated runtime ~{estimatedSeconds}s. DO NOT touch any "
			f"physical keypad during this operation."
		)
		# Kick off in a background thread so this UI callback returns quickly
		# and Indigo doesn't time us out at 10s.
		t = threading.Thread(
			target=self._runZoneLabelWrite,
			args=(dev.id, dev.name, zoneNumber, segments, normalised, preview),
			name=f"DSCAlarm-ZoneLabelWrite-{zoneNumber}",
			daemon=True,
		)
		t.start()
		return valuesDict

	def _runZoneLabelWrite(self, devId, devName, zoneNumber, segments, normalised, preview, manageOutcomes=True):
		"""Background-thread body for writeZoneLabelToKeypad. Runs the label
		write as a staged state machine, verifying the panel reaches the
		expected LCD prompt after each segment before sending the next one.

		Critically, after entering the section number we sanity-check that
		the LCD is showing a label-editing display, NOT 'Enter Data'. If we
		see 'Enter Data' that means we landed in a data-programming section
		(e.g. Zone Definitions) and any further keystrokes would corrupt
		panel programming — so we abort immediately and back out.

		`devId` is needed to update the device's labelText/lastSyncedLabelText
		pluginProps and to record per-zone outcomes.

		`manageOutcomes` controls per-zone-outcomes lifecycle. The
		per-zone button calls with the default True, so this method
		opens a fresh outcomes list at the top and logs the summary at
		the bottom. The bulk write worker passes False — IT owns the
		outcomes list (one list spanning all zones in the bulk run).
		"""
		if manageOutcomes:
			self._beginZoneOutcomes()
		segmentDict = dict(segments)

		def _sendSegment(name):
			"""Queue all keystrokes for the named segment. Returns False if
			the IT-100 connection dropped mid-sequence."""
			keys = segmentDict.get(name, [])
			self._alwaysLog(
				f"  segment {name!r}: queueing {len(keys)} keystrokes "
				f"({''.join(keys)!r})"
			)
			for k in keys:
				if not self._sendKeystrokeToIT100(k):
					self._alwaysLog(
						f"Zone label write ({devName}): IT-100 went away "
						f"during segment '{name}'. The panel may still be "
						f"in installer mode — press the STOP button or send "
						f"#### on a physical keypad to back out.",
						isError=True,
					)
					return False
			return True

		def _emergencyBackout():
			"""Send enough # keystrokes to exit any depth of installer menu."""
			for _ in range(4):
				self._sendKeystrokeToIT100('#')

		try:
			self._alwaysLog(
				f"Zone label write ({devName}): --- staged write for zone "
				f"{zoneNumber:03d}, label {preview!r} ---"
			)

			opLabel = f"Zone label write ({devName})"

			# Pre-flight: clear any latent installer state with up to 3
			# rounds of #### (a single round can fall short on a busy
			# keybus). If we still can't get back to idle the helper
			# logs an abort message that does NOT tell the user to do
			# anything impossible (other keypads are locked out while
			# the IT-100 holds installer mode).
			if self._preFlightBackoutToIdle(opLabel) != 'idle':
				return

			# --- Stage 1: enter installer programming (*8 + code) ---
			# Success: 'Enter Section ---' (outer installer-programming menu)
			# Failure: 'Invalid Access Code' (wrong installer code)
			# Retry on revert-to-idle (likely dropped keystroke).
			stage1Result = self._enterInstallerModeWithRetry(
				opLabel=opLabel,
				sendEnterInstallerSegment=lambda: _sendSegment('enter_installer'),
			)
			if stage1Result == 'success':
				pass
			elif stage1Result == 'wrong_code':
				self._alwaysLog(
					f"{opLabel}: ABORTED at stage 1 — check the installer "
					f"code in the plugin config. Backing out.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Write FAIL',
					panelLabel=None,
					details='stage 1 - wrong installer code',
				)
				_emergencyBackout()
				return
			elif stage1Result == 'io_error':
				self._recordZoneOutcome(
					devId, 'Write FAIL',
					panelLabel=None,
					details='stage 1 - IT-100 IO error',
				)
				return
			else:  # 'failed_after_retries'
				self._alwaysLog(
					f"{opLabel}: ABORTED at stage 1 — couldn't reach the "
					f"installer menu after retries. See LCD transitions above. "
					f"Backing out.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Write FAIL',
					panelLabel=None,
					details='stage 1 - did not reach installer menu after retries',
				)
				_emergencyBackout()
				return

			# --- Stage 2: select LCD label programming sub-menu (*) ---
			# Success: 'Enter LCD / Section ---'
			# (Without this *, we'd stay in the outer installer menu and
			#  the section number that follows would be interpreted as a
			#  top-level installer section — section 001 = Zone Definitions
			#  / data programming, which would let us corrupt panel data.)
			if not _sendSegment('select_lcd_menu'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['enter lcd'],
				timeout_seconds=10,
				prefix="stage 2 (select LCD label menu)",
			)
			if result != 'success':
				self._alwaysLog(
					f"Zone label write ({devName}): ABORTED at stage 2 — "
					f"panel didn't show 'Enter LCD / Section' within 10 "
					f"seconds after sending '*'. The panel firmware may "
					f"use different wording, or we may be in an unexpected "
					f"menu state. Backing out.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Write FAIL',
					panelLabel=None,
					details="stage 2 - panel didn't show 'Enter LCD'",
				)
				_emergencyBackout()
				return

			# --- Stage 3: enter the 3-digit section number ---
			# Success: LCD shows the current zone label (any text other
			#   than 'Enter LCD' / 'Enter Section' / 'Enter Data')
			# Failure (CRITICAL): 'Enter Data' — means we somehow landed
			#   in a data section despite the trailing *. If this fires we
			#   ABORT before sending any label keystrokes, because typing
			#   into a data section corrupts panel programming.
			if not _sendSegment('section'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=[],  # No reliable success indicator (LCD shows arbitrary label text)
				failure_substrings=['enter data'],
				timeout_seconds=5,
				prefix="stage 3 (enter section)",
			)
			if result == 'failure':
				self._alwaysLog(
					f"Zone label write ({devName}): ABORTED at stage 3 — "
					f"panel showed {matched}. This means we landed in a "
					f"DATA section, not the label-editing screen. Sending "
					f"any more keystrokes would corrupt panel programming. "
					f"Backing out immediately. This shouldn't happen if "
					f"stage 2 succeeded — please report the LCD transitions "
					f"above.",
					isError=True,
				)
				self._recordZoneOutcome(
					devId, 'Write FAIL',
					panelLabel=None,
					details=f"stage 3 - landed in DATA section ({matched})",
				)
				_emergencyBackout()
				return
			# Stage 3 doesn't have a clean success substring because the
			# LCD content is the zone's current label (which is arbitrary
			# user text). So we treat 'timeout without seeing Enter Data'
			# as success and log the current label for the user's records.
			currentLCD = self._snapshotKeypadLCD()
			for partition, (l1, l2) in currentLCD.items():
				self._alwaysLog(
					f"  current zone {zoneNumber:03d} label on p{partition}: "
					f"{l1!r} / {l2!r}"
				)

			# --- Stage 4: type the label, ONE CHARACTER AT A TIME with
			# per-char LCD verification ---
			#
			# Earlier versions batched all the label keystrokes into the
			# queue and waited for it to drain. That works when no
			# keystrokes are dropped — but as we've seen, occasionally a
			# digit press or case-toggle key gets lost between the plugin
			# and the IT-100, producing a corrupted label. The user has
			# no way to know without reading the label back afterwards.
			#
			# Per-char verification catches drops in real time. After
			# typing each character we snapshot the LCD and compare
			# position `i` against the expected char. If it matches we
			# advance the cursor; if it doesn't, we abort the whole
			# operation cleanly so the user can investigate and retry.
			#
			# We re-derive the per-char keystrokes (case toggle + digit
			# presses) from the label so this stage doesn't rely on the
			# `label_text` segment built earlier. The flat keystroke list
			# is still in `segments` for the dry-run preview and for
			# logging the full sequence at the start.
			charSequences, _normalised2, _preview2 = self._labelStringToPerCharSequences(
				preview  # `preview` is the padded 14-char target string
			)
			self._alwaysLog(
				f"  stage 4: typing {len(charSequences)} characters with "
				f"per-char LCD verification..."
			)

			abortedOnMismatch = False
			maxCharAttempts = 3  # 1 initial + 2 retries

			for i, charSeq in enumerate(charSequences):
				# Cancel check before each character — keeps cancel
				# responsive on long labels (a 14-char label can be in
				# this loop for 30+ seconds). Skips straight to the
				# emergency-backout exit at the bottom of this method.
				if self.bulkOperationCancelRequested:
					self._alwaysLog(
						f"  char {i+1:02d}: cancel honoured before typing — "
						f"aborting this zone's write."
					)
					abortedOnMismatch = True
					break

				expected     = charSeq['expected']
				caseToggle   = charSeq['caseToggle']
				digitPresses = charSeq['digitPresses']
				keysToSend   = caseToggle + digitPresses
				keysStr      = ''.join(keysToSend)

				charSucceeded = False
				for charAttempt in range(1, maxCharAttempts + 1):
					# Pre-attempt snapshot — what does the LCD look like
					# right before we start typing this char? Useful to
					# see if the cursor advance from the previous char
					# actually took effect, and to confirm we're still on
					# the label-edit screen.
					preAttemptSnap = self._snapshotKeypadLCD()
					for partition, (l1, _l2) in preAttemptSnap.items():
						self._alwaysLog(
							f"  char {i+1:02d}: pre-type LCD on p{partition}: "
							f"{l1!r}"
						)
						break  # one partition is enough

					if charAttempt > 1:
						# Recovery before retry. The most common observed
						# failure is "the whole burst of keystrokes for
						# this char silently vanished" — in which case the
						# cursor is still on position i and the panel is
						# still on the label-edit screen, so we just
						# re-send the same keystrokes.
						#
						# But it's possible only some keystrokes landed,
						# leaving the panel stuck in the *-menu. Check the
						# LCD: if it looks menu-ish, send # to back out
						# before retrying.
						self._alwaysLog(
							f"  char {i+1:02d}: retrying (attempt "
							f"{charAttempt}/{maxCharAttempts}) after "
							f"previous mismatch."
						)
						for partition, (l1, _l2) in preAttemptSnap.items():
							if l1 and any(w in l1.lower() for w in ('save', 'cycle case', 'change case')):
								self._alwaysLog(
									f"  char {i+1:02d}: panel appears to be "
									f"in a *-menu ({l1!r}) — sending # to "
									f"back out before retrying."
								)
								self._sendKeystrokeToIT100('#')
								time.sleep(0.8)
								break

					self._alwaysLog(
						f"  char {i+1:02d}: typing {keysStr!r} for expected "
						f"{expected!r}"
						+ (f" (attempt {charAttempt}/{maxCharAttempts})" if charAttempt > 1 else "")
					)

					# Send case toggle first (if any), then wait for the
					# panel to finish showing all its menu/confirmation
					# screens (Select Option / Save, Change Case, Lower
					# Case Now / Selected, etc.) before we send the digit
					# presses. Without this wait, the first digit press
					# arrives while the panel is still showing the
					# 'Lower Case Now Selected' confirmation screen, which
					# DISMISSES the screen but is consumed by it — so the
					# panel only sees the SUBSEQUENT digit presses, which
					# means the character cycle stops one or two short of
					# the target letter. (Observed: '*>*555' for lowercase
					# 'o' produced 'm' on the LCD, indicating one or two
					# of our '5' presses were eaten by the menu transition.)
					if caseToggle:
						for k in caseToggle:
							if not self._sendKeystrokeToIT100(k):
								self._alwaysLog(
									f"Zone label write ({devName}): IT-100 went away "
									f"while typing char {i+1} (case toggle). "
									f"Backing out.",
									isError=True,
								)
								_emergencyBackout()
								return
						# Wait for the case-toggle menu sequence to finish.
						# The panel goes through several screens before
						# returning to the label edit view.
						self._waitForLabelEditScreen(
							charIndex=i,
							maxWaitSeconds=4.0,
							pollIntervalSeconds=0.3,
						)

					# Now send the digit presses with the panel cleanly on
					# the label edit screen.
					for k in digitPresses:
						if not self._sendKeystrokeToIT100(k):
							self._alwaysLog(
								f"Zone label write ({devName}): IT-100 went away "
								f"while typing char {i+1}. Backing out.",
								isError=True,
							)
							_emergencyBackout()
							return

					# Wait long enough for the keystrokes to drain AND the
					# LCD to update. Worst case: 3 case-toggle keys + 4
					# digit presses = 7 keys × 0.2s throttle = 1.4s of TX,
					# plus ~300ms for the LCD broadcast to arrive. 2.5s
					# baseline gives comfortable headroom even if the main
					# loop is busy.
					#
					# Take MULTIPLE snapshots during the wait so we can see
					# whether the LCD changed transiently — useful for
					# diagnosing whether the *-menu opened briefly or the
					# keystrokes had any visible effect at all.
					totalWait = max(2.0, len(keysToSend) * 0.3 + 1.0)
					snapshotIntervals = 4
					perSnap = totalWait / snapshotIntervals
					transientLCDs = []
					for _ in range(snapshotIntervals):
						time.sleep(perSnap)
						snap = self._snapshotKeypadLCD()
						for partition, (l1, _l2) in snap.items():
							transientLCDs.append(l1)
							break

					# Log all the transient observations so we can see if
					# the LCD ever changed during the wait. If they're all
					# identical to the pre-attempt LCD, the keystrokes had
					# no effect at all (most likely cause: silent drop).
					uniqueTransients = []
					for l1 in transientLCDs:
						if not uniqueTransients or uniqueTransients[-1] != l1:
							uniqueTransients.append(l1)
					self._alwaysLog(
						f"  char {i+1:02d}: LCD during wait (deduped, "
						f"{len(transientLCDs)} samples): {uniqueTransients}"
					)

					# Use the final snapshot for verification.
					snapshot = self._snapshotKeypadLCD()
					lcdLine1 = None
					for partition, (l1, _l2) in snapshot.items():
						if l1:
							lcdLine1 = l1
							break

					if lcdLine1 is None or len(lcdLine1) <= i:
						self._alwaysLog(
							f"  char {i+1:02d}: couldn't read LCD position "
							f"{i} (LCD content was {lcdLine1!r})."
						)
						continue

					actual = lcdLine1[i]
					if actual == expected:
						self._alwaysLog(
							f"  char {i+1:02d}: VERIFIED — LCD position {i} "
							f"shows {actual!r} as expected"
							+ (f" (after {charAttempt} attempt{'s' if charAttempt > 1 else ''})" if charAttempt > 1 else "")
							+ "."
						)
						charSucceeded = True
						break  # out of charAttempt loop, proceed to next char

					self._alwaysLog(
						f"  char {i+1:02d}: MISMATCH — expected {expected!r} "
						f"at LCD position {i}, but got {actual!r}. Full "
						f"LCD line 1: {lcdLine1!r}."
					)

				if not charSucceeded:
					self._alwaysLog(
						f"  char {i+1:02d}: ABORT — couldn't get position "
						f"{i} to show {expected!r} after {maxCharAttempts} "
						f"attempts. Aborting the write.",
						isError=True,
					)
					abortedOnMismatch = True
					break

				# Advance the cursor — unless this is the last char in
				# which case the cursor doesn't need to move (we'll send
				# Save next).
				if i < len(charSequences) - 1:
					if not self._sendKeystrokeToIT100('>'):
						self._alwaysLog(
							f"Zone label write ({devName}): IT-100 went away "
							f"while advancing cursor after char {i+1}. "
							f"Backing out.",
							isError=True,
						)
						_emergencyBackout()
						return
					# Brief wait for the cursor advance to register before
					# typing the next character. Without this the next
					# char's first keypress may land while the cursor is
					# still on position i.
					time.sleep(0.3)

			if abortedOnMismatch:
				# Clean up: send #### to exit installer mode without saving.
				# Without this the panel sits in installer mode at the
				# label-edit screen with our partially-typed label, which
				# would be confusing.
				self._alwaysLog(
					"  cleaning up after mismatch: sending #### to exit "
					"installer mode WITHOUT saving (the partial label is "
					"discarded)."
				)
				# Capture the LCD state for the note — useful for
				# diagnosis. The partial-edit state is what was on the
				# panel at abort time, but since we send #### without
				# saving, the panel's stored label is UNCHANGED. So the
				# 'Label currently' value in the note is "what was there
				# before the write attempt" — pull that from
				# lastSyncedLabelText if we have it, else 'unknown'.
				panelStillHas = '<unknown>'
				try:
					devForNote = indigo.devices[devId]
					panelStillHas = (
						devForNote.pluginProps.get('lastSyncedLabelText')
						or '<unknown>'
					)
				except Exception:
					pass
				self._recordZoneOutcome(
					devId, 'Write FAIL',
					panelLabel=panelStillHas,
					details=f"stage 4 - per-char mismatch on char {i+1:02d} (expected {expected!r}); panel label unchanged (write aborted before save)",
				)
				_emergencyBackout()
				return

			# Log the final state before save
			finalLCD = self._snapshotKeypadLCD()
			for partition, (l1, l2) in finalLCD.items():
				self._alwaysLog(
					f"  pre-save LCD on p{partition}: {l1!r} / {l2!r}"
				)

			# --- Stage 5: save (**) ---
			# Success: returns to 'Enter LCD / Section ---' (ready for the
			# next section number).
			if not _sendSegment('save'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['enter lcd', 'section ---'],
				timeout_seconds=10,
				prefix="stage 5 (save)",
			)
			if result != 'success':
				self._alwaysLog(
					f"Zone label write ({devName}): WARNING — couldn't "
					f"verify save returned to the LCD-label menu within "
					f"10 seconds. The label MAY still have been saved; "
					f"check the keypad manually. Continuing with exit.",
					isError=True,
				)

			# --- Stage 6: exit section (#) ---
			# Success: 'Enter Section ---' (outer installer menu)
			if not _sendSegment('exit_section'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['enter section', 'section ---'],
				timeout_seconds=10,
				prefix="stage 6 (exit section)",
			)
			# (Soft fail — continue to final exit regardless)

			# --- Stage 7: exit installer mode (#) ---
			# Success: idle Date/Time/Ready display
			if not _sendSegment('exit_installer'):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['date', 'ready', 'disarmed'],
				timeout_seconds=10,
				prefix="stage 7 (exit installer)",
			)
			if result != 'success':
				self._alwaysLog(
					f"Zone label write ({devName}): exited installer mode "
					f"but couldn't verify return to idle within 10 seconds. "
					f"The panel may still be in some menu state — manually "
					f"press # on a keypad if needed.",
					isError=True,
				)
				return

			# Persist labelText (the stripped, normalised version actually
			# written to the panel) and lastSyncedLabelText to pluginProps.
			# lastSyncedLabelText is the snapshot used by 'Write Changed
			# Zone Labels' to detect zones the user has since edited
			# locally and needs to push again.
			#
			# We use the user's input labelText if available (so what they
			# typed is what gets remembered), else fall back to the
			# normalised preview with trailing spaces stripped (which is
			# what the panel actually has stored, modulo the 14-char pad).
			syncedLabel = preview.rstrip()
			try:
				devForProps = indigo.devices[devId]
				newProps = dict(devForProps.pluginProps)
				newProps['labelText'] = syncedLabel
				newProps['lastSyncedLabelText'] = syncedLabel
				devForProps.replacePluginPropsOnServer(newProps)
			except Exception as err:
				self._alwaysLog(
					f"Zone label write ({devName}): SUCCESS on panel, but "
					f"couldn't update the device's labelText / "
					f"lastSyncedLabelText props: {err}. The label is on the "
					f"panel correctly; next 'Write Changed' may unnecessarily "
					f"include this zone.",
					isError=True,
				)

			self._alwaysLog(
				f"Zone label write ({devName}): SUCCESS — label written for "
				f"zone {zoneNumber:03d} ({preview!r}). To broadcast this "
				f"label to other PK5500 keypads on the system, enter "
				f"installer programming and run section 998."
			)
			self._recordZoneOutcome(
				devId, 'Write SUCCESS', panelLabel=syncedLabel
			)
		except Exception as err:
			self._alwaysLog(
				f"Zone label write ({devName}): unexpected error in "
				f"background thread: {err}. The panel may still be in "
				f"installer mode — sending #### to back out.",
				isError=True,
			)
			try:
				self._recordZoneOutcome(
					devId, 'Write FAIL',
					panelLabel=None,
					details=f"unexpected error: {err}",
				)
			except Exception:
				pass
			try:
				_emergencyBackout()
			except Exception:
				pass
		finally:
			# If we opened a fresh outcomes list at the top, dump the
			# summary and clear it. Bulk callers pass manageOutcomes=False
			# and own the lifecycle themselves.
			if manageOutcomes:
				try:
					self._logZoneOutcomes(f"Zone Label Write ({devName})")
				except Exception:
					pass

	def stopZoneLabelProgramming(self, valuesDict, typeId, devId):
		"""Device-config button callback. Best-effort attempt to back out of
		installer mode if a label-write got stuck or the user wants to abort
		mid-sequence.

		Strategy: flush whatever is left in the IT-100 outbound queue, then
		queue '##' (exit section, exit installer mode) so it's the next thing
		the panel sees. There's no guarantee this works — once the panel is
		well and truly stuck in installer mode, only a power cycle will help.
		"""
		try:
			dev = indigo.devices[devId]
			devName = dev.name
		except Exception:
			devName = f"<device {devId}>"

		if not self.it100Connected:
			self._alwaysLog(
				f"STOP ({devName}): IT-100 is not connected — nothing to do here. "
				f"If the panel is stuck in installer mode you'll need to press '##' "
				f"on a physical keypad, or power-cycle the panel.",
				isError=True,
			)
			return valuesDict

		# Atomic-replace the queue rather than .clear()ing it — clear() can race
		# with processIT100CommandQueue, which reads [0] then deletes [0] in two
		# separate ops, and would raise IndexError if our clear lands between
		# them. Replacing the binding is safer: the drainer re-reads
		# self.it100TxCmdList each call so it sees the new empty list on its
		# next pass. The existing try/except in the drainer would have caught
		# the IndexError, but it's cleaner to just avoid the race entirely.
		oldQueue = self.it100TxCmdList
		self.it100TxCmdList = []
		flushed = len(oldQueue)
		self._alwaysLog(f"STOP ({devName}): flushed {flushed} queued IT-100 keystrokes.")

		# Queue '##' — exit section, exit installer mode.
		for keyChar in ['#', '#']:
			self._sendKeystrokeToIT100(keyChar)
		self._alwaysLog(
			f"STOP ({devName}): queued '##' to back out of installer mode. "
			f"Watch the keypad device's LCDLine1/LCDLine2 states — they should "
			f"return to the normal idle display within ~10 seconds. If they "
			f"don't, you'll need to press '##' on a physical keypad or "
			f"power-cycle the panel."
		)
		return valuesDict


	def methodBypassZone(self, action, dev):
		key = dev.pluginProps["zoneNumber"]
		keyp = dev.pluginProps["zonePartition"]
		zone = indigo.devices[self.zoneList[int(key)]]
		dev = indigo.devices[self.keypadList[int(keyp)]]
		keypname = f" '{str(dev.pluginProps['partitionName'])}'"
		keypstate = str(dev.states['state'])
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		if keypstate in (kAlarmStateArmedStay, kAlarmStateArmedAway):
			self.logger.warning(f"The Selected Zone '{zone.name}' is Armed and Cannot be Bypassed.")
			return
		self.logger.info(f"Received Zone Bypass Action for Zone '{zone.name}' in Partition {keyp}{keypname}.")
		key = str(key).zfill(2)
		tx = f"071{keyp}*1{key}#"
		self.sendCommand(tx, force_interface='envisalink')
		#This is a toggle action, i.e. already bypassed zones will turn to non-bypassed state.
		#Zones in partition 2-8 do not report bypass status, therefore there will be no zone state update.


	def methodBypassZoneCancel(self, action, dev):
		keyp = dev.pluginProps["partitionNumber"]
		dev = indigo.devices[self.keypadList[int(keyp)]]
		keypname = f" '{str(dev.pluginProps['partitionName'])}'"
		keypstate = str(dev.states['state'])
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		if keypstate in (kAlarmStateArmedStay, kAlarmStateArmedAway):
			self.logger.warning(f"The Selected Partition {keyp}{keypname} is Armed and Cannot Accept Bypass Cancel.")
			return
		self.logger.info(f"Received All Zones Bypass Cancel for Partition {keyp}{keypname}.")
		tx = f"071{keyp}*100#"
		self.sendCommand(tx, force_interface='envisalink')


	def methodBypassZoneRecall(self, action, dev):
		keyp = dev.pluginProps["partitionNumber"]
		dev = indigo.devices[self.keypadList[int(keyp)]]
		keypname = f" '{str(dev.pluginProps['partitionName'])}'"
		keypstate = str(dev.states['state'])
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		if keypstate in (kAlarmStateArmedStay, kAlarmStateArmedAway):
			self.logger.warning(f"The Selected Partition {keyp}{keypname} is Armed and Cannot Accept Bypass Recall.")
			return
		self.logger.info(f"Received Zone(s) Bypass Recall for Partition {keyp}{keypname}.")
		tx = f"071{keyp}*199#"
		self.sendCommand(tx, force_interface='envisalink')


	def methodDoorChimeEnable(self, action, dev):
		keyp = dev.pluginProps["partitionNumber"]
		dev = indigo.devices[self.keypadList[int(keyp)]]
		keypname = f" '{str(dev.pluginProps['partitionName'])}'"
		keypstate = str(dev.states['KeypadChime'])
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		self.logger.info(f"Received Keypad Chime Enable for Partition {keyp}{keypname}.")
		if keypstate != kKeypadStateChimeEnabled:
			tx = f"071{keyp}*4"
			self.sendCommand(tx, force_interface='envisalink')
			return


	def methodDoorChimeDisable(self, action, dev):
		keyp = dev.pluginProps["partitionNumber"]
		dev = indigo.devices[self.keypadList[int(keyp)]]
		keypname = f" '{str(dev.pluginProps['partitionName'])}'"
		keypstate = str(dev.states['KeypadChime'])
		if not self.envisalinkConnected:
			self.logger.error("This Action requires Envisalink connection.")
			return
		self.logger.info(f"Received Keypad Chime Disable for Partition {keyp}{keypname}.")
		if keypstate == kKeypadStateChimeEnabled:
			tx = f"071{keyp}*4"
			self.sendCommand(tx, force_interface='envisalink')
			return


	def methodSyncTime(self, action):
		d = datetime.now()
		self.logger.info("Setting alarm panel time and date.")
		self.sendCommand(
			f"010{d.strftime('%H%M%m%d%y')}",
			force_interface=self._resolveCommandInterface(action),
		)


    # Queue a command to set DSC Thermostat Setpoints
	#
	def methodAdjustThermostat(self, action):
		# Thermostat setpoint adjustment is a three-step synchronous protocol
		# (095 -> wait for 563, then 096..., then 097...) which doesn't fit the
		# async queue model used by 'sendCommand'. Just call setThermostat()
		# directly on the action thread — it uses sendPacket which talks to
		# self.port synchronously. This blocks the Indigo action for a couple
		# of seconds which is fine for a one-shot user action.
		self.logger.debug(f"Adjusting thermostat for action: {action}")
		self.setThermostat(action)


	# The command queued above calls this routine to create the packet
	#
	def setThermostat(self, action):
		#find this thermostat in our list to get the number
		for sensorNum in self.tempList.keys():
			if self.tempList[sensorNum].id == action.deviceId:
				break

		self.logger.debug(f"SensorNum = {sensorNum}")

		#send 095 for thermostat in question, wait for 563 response
		#self.logger.debug('095' + str(sensorNum))
		rx = self.sendPacket('095' + str(sensorNum), waitFor='563')
		if not rx:
			self.logger.error("Error getting current thermostat setpoints, aborting adjustment.")
			return

		if (action.props['thermoAdjustmentType'] == '+') or (action.props['thermoAdjustmentType'] == '-'):
			sp = 0
		else:
			sp = int(action.props['thermoSetPoint'])

		# then 096TC+000 to inc cool,
		#      096Th-000 to dec heat
		#      096Th=### to set setpoint
		# wait for 563 response
		#self.logger.debug('096%u%c%c%03u' % (sensorNum, action.props['thermoAdjustWhich'], action.props['thermoAdjustmentType'],sp) )
		rx = self.sendPacket('096%u%c%c%03u' % (sensorNum, action.props['thermoAdjustWhich'], action.props['thermoAdjustmentType'], sp), waitFor='563')
		if not rx:
			self.logger.error("Error changing thermostat setpoints, aborting adjustment.")
			return

		# send 097T
		#send 097 for thermostat in question to save setting, wait for 563 response
		rx = self.sendPacket('097' + str(sensorNum), waitFor='563')
		if not rx:
			self.logger.error("Error saving thermostat setpoints, aborting adjustment.")
			return


	# Reset an Alarm Zone Group's timer to 0
	#
	def methodResetZoneGroupTimer(self, action):
		if action.deviceId in indigo.devices:
			zoneGrp = indigo.devices[action.deviceId]
			self.logger.debug(f"Manual timer reset for alarm zone group \"{zoneGrp.name}\"")
			zoneGrp.updateStateOnServer(key="AnyMemberLastChangedTimer", value=0)
			zoneGrp.updateStateOnServer(key="EntireGroupLastChangedTimer", value=0)
			zoneGrp.updateStateOnServer(key="AnyMemberLastChangedShort", value="0m")
			zoneGrp.updateStateOnServer(key="EntireGroupLastChangedShort", value="0m")


	######################################################################################
	# Indigo Pref UI Methods
	######################################################################################

	def menuShowStoredCredentials(self):
		"""Plugins-menu callback that prints currently-stored credentials
		to the Indigo event log so the user can recover them if they've
		forgotten what they set. Useful when the Envisalink web UI password
		needs recovering before a factory reset.

		The plugin stores these in plain text in pluginPrefs (Indigo doesn't
		encrypt prefs), so this is just dumping back what was already there.
		Anyone with access to ~/Library/Application Support/Perceptive
		Automation/.../Plugins/...prefs can already read them.

		Prints to log:
		  - Envisalink TPI password (TwoDS_Password)
		  - Envisalink address and port
		  - Installer code (configured for label-programming, sensitive)
		  - Serial port (IT-100)
		"""
		self._alwaysLog("--- Stored credentials (from plugin config) ---")

		envPass = self.pluginPrefs.get('TwoDS_Password', '')
		envAddr = self.pluginPrefs.get('TwoDS_Address', '')
		envPort = self.pluginPrefs.get('TwoDS_Port', '')
		serial  = self.pluginPrefs.get('serialPort', '')
		instCode = self.pluginPrefs.get('installerCode', '')

		if envPass:
			self._alwaysLog(f"  Envisalink TPI password : {envPass!r}")
		else:
			self._alwaysLog("  Envisalink TPI password : (not set)")
		if envAddr or envPort:
			self._alwaysLog(f"  Envisalink address      : {envAddr}:{envPort}")
		if serial:
			self._alwaysLog(f"  IT-100 serial port      : {serial}")
		if instCode:
			self._alwaysLog(f"  Installer code          : {instCode!r}")
		else:
			self._alwaysLog("  Installer code          : (not set)")

		self._alwaysLog(
			"NOTE: the Envisalink TPI password is the same password used on "
			"the Envisalink web UI (default 'user'). If you've lost the web-UI "
			"password too, use this one — they're the same field. Save these "
			"somewhere safe (1Password, Keychain, etc.) and consider deleting "
			"this log entry afterwards since the event log is plain text."
		)

	def menuPrintZoneStates(self):
		"""Plugins-menu callback that dumps every configured zone's current
		Indigo state to the event log. This is just a snapshot of what the
		plugin currently believes — it does NOT ask the panel for fresh
		data. Useful for diagnosing 'this zone shows the wrong state' issues
		without any side effects.

		Run 'Refresh Zone States from Panel' if you want the panel to
		re-broadcast its actual current state, which will correct any stuck
		zones as a side effect."""
		if not self.zoneList:
			self._alwaysLog("No zone devices are configured.")
			return

		self._alwaysLog(f"--- Indigo zone states ({len(self.zoneList)} zones configured) ---")
		# Header line — fixed-width so things line up in the event log viewer
		self._alwaysLog(f"  {'Zone':<6} {'State':<12} {'Bypass':<8} {'Type':<18} Name")
		self._alwaysLog(f"  {'-' * 4:<6} {'-' * 10:<12} {'-' * 6:<8} {'-' * 16:<18} {'-' * 30}")

		stateCounts = {}
		for zoneNum in sorted(self.zoneList.keys()):
			try:
				zone = indigo.devices[self.zoneList[zoneNum]]
			except Exception:
				self._alwaysLog(f"  {zoneNum:<6} (device unavailable — possible server communication error)")
				continue
			state = zone.states.get('state', '?')
			bypass = zone.states.get('bypass', '?')
			zoneType = zone.pluginProps.get('zoneType', '?').replace('zoneType', '')
			stateCounts[state] = stateCounts.get(state, 0) + 1
			self._alwaysLog(
				f"  {zoneNum:<6} {state:<12} {bypass:<8} {zoneType:<18} {zone.name}"
			)

		# Summary
		summary = ', '.join(f"{count} {state}" for state, count in sorted(stateCounts.items()))
		self._alwaysLog(f"--- Summary: {summary} ---")

	def menuRequestPanelStatus(self):
		"""Plugins-menu callback that sends cmd 001 (Status Report) to the
		panel. The panel will respond with a flood of state-update packets
		covering every zone, partition, and trouble condition. Those will
		be processed normally through the plugin's dispatch path, updating
		device states as they arrive.

		Useful for correcting zones that have drifted out of sync with the
		panel (e.g. a motion sensor stuck at 'open' because a previous
		cmd 610 went missing). Doesn't print anything itself — just kicks
		off the request. The state updates will show in the log as they
		arrive."""
		if not (self.it100Connected or self.envisalinkConnected):
			self._alwaysLog(
				"Cannot request panel status — neither interface is connected.",
				isError=True,
			)
			return

		# cmd 001 is the TPI Status Report command. The panel will respond
		# with a flurry of 6XX state packets for every zone and partition.
		self._alwaysLog(
			"Sending Status Report request to panel (cmd 001). The panel "
			"will reply with current state of every zone and partition over "
			"the next few seconds — those will be processed normally and "
			"will correct any zones that have drifted out of sync."
		)
		self.sendCommand('001')

	def menuRefreshAndDumpStates(self):
		"""Plugins-menu callback that combines menuRequestPanelStatus and
		menuPrintZoneStates with a wait in between. Asks the panel for a
		fresh dump, waits a few seconds for it to flow through, then prints
		the resulting Indigo zone states to the log. The combined output
		gives you a 'what does Indigo think NOW after asking the panel'
		snapshot in one go."""
		if not (self.it100Connected or self.envisalinkConnected):
			self._alwaysLog(
				"Cannot refresh — neither interface is connected.",
				isError=True,
			)
			return

		self._alwaysLog("--- Refresh zone states from panel ---")
		self._alwaysLog("Step 1: requesting status from panel...")
		self.sendCommand('001')

		# The panel's response takes a few seconds to fully arrive and be
		# processed. Sleep this thread to let the main loop's drain code
		# consume the responses. Run in a background thread so we don't
		# block the Indigo UI thread for 5 seconds.
		def _waitAndDump():
			try:
				time.sleep(5)
				self._alwaysLog("Step 2: response window elapsed. Indigo zone states are now:")
				self.menuPrintZoneStates()
			except Exception as err:
				self._alwaysLog(
					f"Refresh: unexpected error in background thread: {err}",
					isError=True,
				)

		t = threading.Thread(
			target=_waitAndDump,
			name="DSCAlarm-RefreshAndDump",
			daemon=True,
		)
		t.start()

	def menuPrintStoredZoneLabels(self):
		"""Plugins-menu callback that dumps the stored zone label for every
		configured alarmZone device (enabled or disabled) to the event log.

		Read-only. Does NOT talk to the panel — this is purely a diagnostic
		of what the plugin currently has in pluginProps. Useful for:
		  - Reviewing what 'Write Changed Zone Labels' would write
		    (anything marked '(modified)' below)
		  - Spotting zones that have never been read or written via the
		    plugin (marked '<never synced>')
		  - Quick visual inventory of zone -> name -> label mapping

		For each zone shows:
		  zone NNN (Indigo device name) : labelText='X'  synced='Y'  [markers]

		Where labelText is what's in the device's pluginProps (the value
		'Write Changed' compares against the panel) and synced is the
		lastSyncedLabelText pluginProp (what was last confirmed pushed to
		or read from the panel).

		Markers:
		  (modified)     — labelText differs from synced
		                   (would be picked up by 'Write Changed Zone Labels')
		  <never synced> — no lastSyncedLabelText set; the plugin has
		                   never successfully read or written this zone
		  [disabled]     — device is disabled in Indigo (enabled=False)
		"""
		# Enumerate ALL alarmZone devices, including disabled ones —
		# self.zoneList only has enabled devices because deviceStartComm
		# is only called for enabled devices.
		zoneDevs = []
		try:
			for dev in indigo.devices.iter("self.alarmZone"):
				zoneDevs.append(dev)
		except Exception as err:
			self._alwaysLog(
				f"Print Stored Zone Labels: failed to enumerate alarmZone "
				f"devices: {err}",
				isError=True,
			)
			return

		if not zoneDevs:
			self._alwaysLog("Print Stored Zone Labels: no alarmZone devices are configured.")
			return

		# Build per-zone display rows. Sort by zone number (ascending),
		# with un-numbered devices last.
		def _zoneNumOf(dev):
			try:
				return int(dev.pluginProps.get('zoneNumber', '0') or '0')
			except (TypeError, ValueError):
				return 0

		zoneDevs.sort(key=lambda d: (_zoneNumOf(d), d.name))

		# Collect rows + counts so we can show useful totals at the end.
		rows = []
		modifiedCount = 0
		neverSyncedCount = 0
		disabledCount = 0
		for dev in zoneDevs:
			zoneNum = _zoneNumOf(dev)
			labelText = dev.pluginProps.get('labelText', '') or ''
			synced = dev.pluginProps.get('lastSyncedLabelText', '') or ''
			isEnabled = True
			try:
				isEnabled = bool(getattr(dev, 'enabled', True))
			except Exception:
				pass

			markers = []
			if not synced:
				markers.append('<never synced>')
				neverSyncedCount += 1
			elif labelText != synced:
				markers.append('(modified)')
				modifiedCount += 1
			if not isEnabled:
				markers.append('[disabled]')
				disabledCount += 1

			rows.append({
				'zoneNum':  zoneNum,
				'devName':  dev.name,
				'label':    labelText,
				'synced':   synced,
				'markers':  markers,
			})

		# Compute column widths so the output lines up.
		headerWidth = max(
			len(f"zone {r['zoneNum']:03d} ({r['devName']})") for r in rows
		)
		labelColWidth = max(
			(len(f"labelText={r['label']!r}") for r in rows), default=0
		)

		self._alwaysLog(
			f"---- Configured Zone Labels ({len(rows)} zone(s)) ----"
		)
		for r in rows:
			header = f"zone {r['zoneNum']:03d} ({r['devName']})"
			labelField = f"labelText={r['label']!r}"
			if r['synced']:
				syncedField = f"synced={r['synced']!r}"
			else:
				syncedField = "synced=<empty>"
			markerText = ('  ' + '  '.join(r['markers'])) if r['markers'] else ''
			self._alwaysLog(
				f"  {header:<{headerWidth}} : "
				f"{labelField:<{labelColWidth}}  "
				f"{syncedField}"
				f"{markerText}"
			)

		# Footer summary.
		summaryParts = [f"{len(rows)} total"]
		if modifiedCount:
			summaryParts.append(f"{modifiedCount} modified (would be written by 'Write Changed')")
		if neverSyncedCount:
			summaryParts.append(f"{neverSyncedCount} never synced")
		if disabledCount:
			summaryParts.append(f"{disabledCount} disabled")
		self._alwaysLog(
			f"---- End Configured Zone Labels ({', '.join(summaryParts)}) ----"
		)

	# Validate the pluginConfig window after user hits OK
	# Returns False on failure, True on success
	#
	def validatePrefsConfigUi(self, valuesDict):
		self.logger.debug("validating Prefs called")
		errorMsgDict = indigo.Dict()
		wasError = False

		# Validate dual-interface configuration
		enableIT100 = valuesDict.get('enableIT100', False)
		enableEnvisalink = valuesDict.get('enableEnvisalink', False)
		
		# At least one interface must be enabled
		if not enableIT100 and not enableEnvisalink:
			errorMsgDict['enableIT100'] = "At least one interface must be enabled"
			errorMsgDict['enableEnvisalink'] = "At least one interface must be enabled"
			wasError = True
		
		# Validate IT-100 settings if enabled
		if enableIT100:
			if not valuesDict.get('serialPort'):
				errorMsgDict['serialPort'] = "Select a valid serial port."
				wasError = True
		
		# Connection-loss grace period: whole seconds, at least 5
		try:
			if int(str(valuesDict.get('unavailableGraceSeconds', '30')).strip()) < 5:
				raise ValueError
		except ValueError:
			errorMsgDict['unavailableGraceSeconds'] = "Enter a whole number of seconds, 5 or more."
			wasError = True

		# Validate Envisalink settings if enabled
		if enableEnvisalink:
			if not valuesDict.get('TwoDS_Address'):
				errorMsgDict['TwoDS_Address'] = "Enter a valid IP address or host name."
				wasError = True
			if not valuesDict.get('TwoDS_Port', '').isdigit():
				errorMsgDict['TwoDS_Port'] = "Enter a valid port number, default 4025."
				wasError = True
			elif not 0 < int(float(valuesDict['TwoDS_Port'])) < 65536:
				errorMsgDict['TwoDS_Port'] = "Enter a valid port number, default 4025."
				wasError = True
			if not valuesDict.get('TwoDS_Password'):
				errorMsgDict['TwoDS_Password'] = "Enter the password for the Envisalink."
				wasError = True

		if not (valuesDict['code'].isdigit()):
			errorMsgDict['code'] = "The access code must numerical."
			wasError = True
			
		if not 3 < len(valuesDict['code']) < 7:
			errorMsgDict['code'] = "The access code must be 4-6 digits."
			wasError = True

		if int(float(valuesDict['code'])) == 0:
			errorMsgDict['code'] = "The access code cannot be 0000."
			wasError = True

		if not (valuesDict['code']):
			errorMsgDict['code'] = "You must enter the alarm's arm/disarm code."
			wasError = True

		if (valuesDict['emailUrgent']):
			if not re.match(r"[^@]+@[^@]+\.[^@]+", valuesDict['emailUrgent']):
				errorMsgDict['emailUrgent'] = "Please enter a valid email address."
				wasError = True

		if (valuesDict['emailNotice']):
			if not re.match(r"[^@]+@[^@]+\.[^@]+", valuesDict['emailNotice']):
				errorMsgDict['emailNotice'] = "Please enter a valid email address."
				wasError = True

		if (valuesDict['EmailDisarm']):
			if not re.match(r"[^@]+@[^@]+\.[^@]+", valuesDict['EmailDisarm']):
				errorMsgDict['EmailDisarm'] = "Please enter a valid email address."
				wasError = True

		userCodeList = valuesDict['userCode'].split(",")
		userLabelList = valuesDict['userLabel'].split(",")
		newuserCodeList = [s for s in userCodeList if s.isdigit()]   #transfers only numbers to newUserCodeList
		length = len(userCodeList)

		if newuserCodeList and (any(len(lst) != length for lst in [newuserCodeList])):
			errorMsgDict['userCode'] = "Config Error: The user code contains invalid characters (2-digit numbers only)."
			wasError = True

		if any(len(lst) != length for lst in [userLabelList]):
			errorMsgDict['userCode'] = "Config Error: The user code and label lists are not of the same length."
			errorMsgDict['userLabel'] = "Config Error: The user code and label lists are not of the same length."
			wasError = True

		if len(userCodeList) != len(set(userCodeList)):
			errorMsgDict['userCode'] = "Config Error: The user code list contains duplicate codes."
			wasError = True

		for key in newuserCodeList:
			if int(key) < 1 or int(key) > 40:
				errorMsgDict['userCode'] = "Config Error: The user code is out of bound."
				wasError = True


		if wasError is True:
			return (False, valuesDict, errorMsgDict)

		# Tell DSC module to reread it's config
		self.configRead = False

		# User choices look good, so return True (client will then close the dialog window).
		return (True, valuesDict)

	def validateActionConfigUi(self, valuesDict, typeId, actionId):
		self.logger.debug("validating Action Config called")
		if typeId == 'actionSendKeypress':
			# Validate with the same parser the action uses at run time, so bracketed
			# tokens and whitespace (as documented in the dialog) are accepted, and
			# installer programming sequences such as *8 are allowed.
			keys = valuesDict.get('keys', '')
			errorMsgDict = indigo.Dict()
			if not keys.strip():
				errorMsgDict['keys'] = "Enter at least one key."
				return (False, valuesDict, errorMsgDict)
			parsedKeys, parseErrors = self._parseKeypressString(keys)
			if parseErrors:
				errorMsgDict['keys'] = "; ".join(parseErrors)
				return (False, valuesDict, errorMsgDict)
		return (True, valuesDict)

	def validateEventConfigUi(self, valuesDict, typeId, eventId):
		self.logger.debug("validating Event Config called")
		#self.logger.debug(f"Type: {typeID}, Id: {eventID}, Dict: {valuesDict}")
		if typeId == 'userArmed' or typeId == 'userDisarmed':
			code = valuesDict['userCode']
			if len(code) != 2:
				errorMsgDict = indigo.Dict()
				errorMsgDict['userCode'] = "The user code must be 2 digits in length."
				return (False, valuesDict, errorMsgDict)

			cleanCode = re.sub(r'[^0-9]+', '', code)
			if len(code) != len(cleanCode):
				errorMsgDict = indigo.Dict()
				errorMsgDict['userCode'] = "The code can only contain digits 0-9."
				return (False, valuesDict, errorMsgDict)
		return (True, valuesDict)

	def validateDeviceConfigUi(self, valuesDict, typeId, devId):
		self.logger.debug("validating Device Config called")
		#self.logger.debug(f"Type: {typeID}, Id: {eventID}, Dict: {valuesDict}")
		if typeId == 'alarmZone':
			zoneNum = int(valuesDict['zoneNumber'])
			if zoneNum in list(self.zoneList.keys()) and devId != indigo.devices[self.zoneList[zoneNum]].id:
				#self.logger.debug("ZONEID: {self.DSC.zoneList[zone].id}")
				errorMsgDict = indigo.Dict()
				errorMsgDict['zoneNumber'] = "This zone has already been assigned to a different device."
				return (False, valuesDict, errorMsgDict)
		if typeId == 'alarmKeypad':
			partitionNum = int(valuesDict['partitionNumber'])
			if partitionNum in list(self.keypadList.keys()) and devId != indigo.devices[self.keypadList[partitionNum]].id:
				errorMsgDict = indigo.Dict()
				errorMsgDict['partitionNumber'] = "This partition has already been assigned to a different device."
				return (False, valuesDict, errorMsgDict)
		return (True, valuesDict)

	def getZoneList(self, filter="", valuesDict=None, typeId="", targetId=0):
		myArray = []
		for i in range(1, 65):
			zoneName = str(i)
			if i in list(self.zoneList.keys()):
				zoneDev = indigo.devices[self.zoneList[i]]
				zoneName = f"{str(i)} - {zoneDev.name}"
			myArray.append((str(i), zoneName))
		return myArray

	def getZoneDevices(self, filter="", valuesDict=None, typeId="", targetId=0):
		myArray = []

		for dev in indigo.devices:
			try:
				if dev.deviceTypeId == 'alarmZone':
					myArray.append((dev.id, dev.name))
			except:
				pass
		return myArray

	def getKeypadList(self, filter="", valuesDict=None, typeId="", targetId=0):
		myArray = []
		for i in range(1, 9):
			keypadName = str(i)
			if i in list(self.keypadList.keys()):
				keypDev = indigo.devices[self.keypadList[i]]
				keypadName = str(keypDev.pluginProps['partitionName'])
				keypadName = f"{str(i)} - {keypadName}"
			myArray.append((str(i), keypadName))
		return myArray


	######################################################################################
	# Configuration Routines
	######################################################################################

	# Reads the plugins config file into our own variables. 
	# Section is read automatically at every startup of plugin.
	#
	def getConfiguration(self, valuesDict):
		
		try:

			# Tell our logging class to reread the config for logging level changes
			self.logLevel = int(self.pluginPrefs.get("logLevel",20))  #new routine
			self.indigo_log_handler.setLevel(self.logLevel)  #new routine
			self.logger.debug(f"getConfiguration start. Log Levels are set to \"{kLogLevelList[int(self.logLevel/10)]}\".")
			#self.logger.debug(f"getConfiguration start. Log Levels are set to \"{valuesDict.get('logLevel',20)}\".")
		
			# Setting log level of private plugin log handler to only log threaddebug events if this is the setting in logging preferences.
			# Normally, the private log handler always logs threaddebug, which can fill up the log quite a lot.
			if self.logLevel == 5:
				self.plugin_file_handler.setLevel(logging.THREADDEBUG)
				self.logger.debug("Private Log Handler set to THREADDEBUG")
			else:
				self.plugin_file_handler.setLevel(logging.DEBUG)
				self.logger.debug("Private Log Handler set to DEBUG")

			# Get setting of Create Variables checkbox
			if valuesDict.get('createVariables', False) is True:
				self.createVariables = True
			else:
				self.createVariables = False

			# If the variable folder doesn't exist, disable variables, we're done!
			if valuesDict.get('variableFolder') not in indigo.variables.folders:
				self.createVariables = False

			# ============================ DUAL-INTERFACE CONFIGURATION ============================
			# Migration from v1.0.0 single-interface config (only run once)
			if 'configInterface' in valuesDict:
				self.logger.info("Migrating from v1.0.0 single-interface configuration...")
				if valuesDict['configInterface'] == 'serial':
					valuesDict['enableIT100']     = True
					valuesDict['enableEnvisalink'] = False
					valuesDict['commandInterface'] = 'it100'
					self.logger.info("Migrated to IT-100 only configuration")
				elif valuesDict['configInterface'] == 'twods':
					valuesDict['enableIT100']     = False
					valuesDict['enableEnvisalink'] = True
					valuesDict['commandInterface'] = 'envisalink'
					self.logger.info("Migrated to Envisalink only configuration")
				del valuesDict['configInterface']
				self.pluginPrefs['enableIT100']     = valuesDict['enableIT100']
				self.pluginPrefs['enableEnvisalink'] = valuesDict['enableEnvisalink']
				self.pluginPrefs['commandInterface'] = valuesDict['commandInterface']

			# Read dual-interface configuration. Use _propIsTrue for the boolean
			# values — Indigo can hand back checkbox values as strings ('true',
			# 'false', '0', '1') or bools depending on context, and a naive
			# .get(key, default) treats the string 'false' as truthy.
			self.enableIT100          = self._propIsTrue(valuesDict, 'enableIT100', default=False)
			self.enableEnvisalink     = self._propIsTrue(valuesDict, 'enableEnvisalink', default=True)
			self.commandInterface     = valuesDict.get('commandInterface', 'envisalink')
			# Default Log Interface Source ON — when both interfaces are
			# active the source tag is the only way to actually see the
			# dual-interface race playing out, so it earns its keep.
			self.debugInterfaceSource = self._propIsTrue(valuesDict, 'debugInterfaceSource', default=True)

			# Validation - at least one interface must be enabled
			if not self.enableIT100 and not self.enableEnvisalink:
				self.logger.error("At least one interface must be enabled! Enabling Envisalink by default.")
				self.enableEnvisalink     = True
				self.commandInterface     = 'envisalink'

			# If only one interface is enabled, command interface must match
			if self.enableIT100 and not self.enableEnvisalink:
				self.commandInterface = 'it100'
				self.useSerial        = True
			elif self.enableEnvisalink and not self.enableIT100:
				self.commandInterface = 'envisalink'
				self.useSerial        = False
			else:
				self.useSerial = (self.commandInterface == 'it100')

			self.logger.info(f"Interface Configuration: IT-100={self.enableIT100}, Envisalink={self.enableEnvisalink}, Commands={self.commandInterface}, DebugSource={self.debugInterfaceSource}")
			# ==========================================================================================

			self.configKeepTimeSynced = valuesDict.get('syncTime', True)
			self.configUseCustomIcons = valuesDict.get('customStateIcons', True)

			# Historical-alarm handling. See _isHistoricalAlarmReplay() and
			# cmd 654 handler. Defaults match the PluginConfig.xml defaults.
			self.configStartupAlertOnHistoricalAlarm = valuesDict.get('startupAlertOnHistoricalAlarm', True)
			try:
				graceRaw = valuesDict.get('startupGracePeriodSeconds', '120')
				self.configStartupGracePeriodSeconds = max(0, min(600, int(graceRaw)))
			except (TypeError, ValueError):
				self.configStartupGracePeriodSeconds = 120
				self.logger.warning(
					f"startupGracePeriodSeconds value {graceRaw!r} is not a valid integer — using 120s default."
				)

			# Zone-latching during alarm. See cmd 610 handler. If the user
			# toggles this OFF while the plugin is running and there are
			# zones already deferred in closeTheseZonesList, drain the list
			# immediately so those zones reflect their real current state
			# (no point keeping them latched if the user just opted out of
			# latching). If the user toggles it ON, no migration needed —
			# the list just starts collecting on the next cmd 610.
			try:
				self.configUnavailableGrace = max(5, int(valuesDict.get('unavailableGraceSeconds', 30) or 30))
			except (TypeError, ValueError):
				self.configUnavailableGrace = 30
			previousLatchSetting = getattr(self, 'configLatchZonesDuringAlarm', False)
			self.configLatchZonesDuringAlarm = valuesDict.get('latchZonesDuringAlarm', False)
			if previousLatchSetting and not self.configLatchZonesDuringAlarm and self.closeTheseZonesList:
				self._alwaysLog(
					f"Zone latching turned OFF — draining "
					f"{len(self.closeTheseZonesList)} deferred zone-"
					f"close(s) immediately so zone states reflect reality."
				)
				for zone in self.closeTheseZonesList:
					try:
						self.updateZoneState(zone, kZoneStateClosed, source="config-toggle drain")
					except Exception as err:
						self.logger.error(f"Failed to drain deferred close for zone {zone}: {err}")
				self.closeTheseZonesList = []

			# Global override for per-device Log Changes. See _shouldLogZoneChanges.
			self.configLogAllZoneChanges = valuesDict.get('logAllZoneChanges', False)

			self.configSpeakVariable = None
			if 'speakToVariableEnabled' in valuesDict:
				if valuesDict['speakToVariableEnabled'] is True:
					self.configSpeakVariable = int(valuesDict['speakToVariableId'])
					if self.configSpeakVariable not in indigo.variables:
						self.logger.error("Speak variable not found in variable list")
						self.configSpeakVariable = None

			self.configEmailUrgent = valuesDict.get('emailUrgent', '')
			self.configEmailNotice = valuesDict.get('emailNotice', '')
			self.configEmailDisarm = valuesDict.get('EmailDisarm', '')
			self.configEmailUrgentSubject = valuesDict.get('emailUrgentSubject', 'Alarm Tripped')
			self.configEmailNoticeSubject = valuesDict.get('emailNoticeSubject', 'Alarm Trouble')
			self.configEmailDisarmSubject = valuesDict.get('DisarmEmailSubject', 'Who Disarmed')

			self.userCodeList = valuesDict.get('userCode', '').split(",")
			self.userLabelList = valuesDict.get('userLabel', '').split(",")
			self.userLabelDict = dict(list(zip(self.userCodeList, self.userLabelList)))

			self.logger.debug("Configuration read successfully")

			return True

		except:
			self.logger.warning("Error reading plugin configuration. Happens on very first launch! Open plugin configuration, review settings and save.")

			return False


	######################################################################################
	# Interface Test Methods
	######################################################################################

	def testIT100Connection(self, valuesDict):
		"""Report on the live IT-100 connection state.

		Called as a PluginConfig <Field type="button"> callback. These callbacks
		MUST return either nothing or the valuesDict itself — returning a tuple
		(as validatePrefsConfigUi does) triggers an Indigo C++ converter error:
		"No registered converter was able to extract a C++ reference to type
		CXmlDict from this Python object of type bool"

		Older versions of this button tried to open a second serial connection
		to test the port. That fought with the live connection the plugin was
		already maintaining (the IT-100 doesn't tolerate two readers any better
		than the Envisalink does), and usually either hung silently or returned
		a useless empty read. Instead, we just report on the live state the
		plugin already knows about — which is the real, honest answer to "is
		this working".

		Output goes through _alwaysLog (i.e. indigo.server.log directly) so the
		test result always appears in the event log even when Event Logging
		Level is set to "Error Messages" or stricter — pressing the button is
		an explicit, interactive request for feedback.
		"""
		port = valuesDict.get('serialPort', '<not set>')

		if not valuesDict.get('serialPort'):
			self._alwaysLog("IT-100 test: please select a serial port first", isError=True)
			return valuesDict

		# If the plugin currently has IT-100 enabled and a live port, report on it.
		liveConnected = getattr(self, 'it100Connected', False)
		livePort = getattr(self, 'it100Port', None)

		if liveConnected and livePort is not None:
			self._alwaysLog(f"IT-100 OK — live serial connection to {port} is up and reading.")
		else:
			# No live connection. Either the plugin hasn't been saved with this
			# port yet, or the connection has failed. We deliberately don't try
			# to open a competing serial port here — that's how the old test
			# code hung/produced no output.
			lastErr = getattr(self, '_lastIT100Error', None)
			if lastErr:
				self._alwaysLog(f"IT-100 NOT connected. Last error: {lastErr}", isError=True)
				self._alwaysLog("Save your config to apply changes, then check the log for connect messages.", isError=True)
			else:
				self._alwaysLog(
					f"IT-100 not yet connected on {port}. "
					f"Save the config to apply changes — the plugin will attempt to connect on its next pass "
					f"and any errors will appear in the log."
				)

		return valuesDict

	def testEnvisalinkConnection(self, valuesDict):
		"""Report on the live Envisalink connection state.

		Called as a PluginConfig <Field type="button"> callback. These callbacks
		MUST return either nothing or the valuesDict itself — returning a tuple
		(as validatePrefsConfigUi does) triggers an Indigo C++ converter error:
		"No registered converter was able to extract a C++ reference to type
		CXmlDict from this Python object of type bool"

		The Envisalink only accepts ONE TCP connection at a time. The old test
		button tried to open a second socket and login fresh — which forced the
		Envisalink to drop the live connection (errno 54: connection reset by
		peer). That was actively harmful: it took the plugin offline every time
		you pressed the button.

		Instead we report the live state. If the plugin already has a working
		login session, that's the real answer to "does this work". If not, we
		only then do a one-shot reachability check (TCP open + close, no login)
		so we can distinguish "wrong IP/port" from "wrong password / panel
		problem" without disrupting anything.

		Output goes through _alwaysLog (i.e. indigo.server.log directly) so the
		test result always appears in the event log even when Event Logging
		Level is set to "Error Messages" or stricter — pressing the button is
		an explicit, interactive request for feedback.
		"""
		addr = valuesDict.get('TwoDS_Address', '')
		port = valuesDict.get('TwoDS_Port', '')

		if not addr:
			self._alwaysLog("Envisalink test: please enter IP address first", isError=True)
			return valuesDict
		if not port:
			self._alwaysLog("Envisalink test: please enter port first", isError=True)
			return valuesDict
		if not valuesDict.get('TwoDS_Password'):
			self._alwaysLog("Envisalink test: please enter password first", isError=True)
			return valuesDict

		liveConnected = getattr(self, 'envisalinkConnected', False)
		liveSocket = getattr(self, 'envisalinkSocket', None)

		if liveConnected and liveSocket is not None:
			# Plugin already has a working session. The fact that login succeeded
			# AND we're getting periodic messages (kept tracked by the runtime)
			# is a much stronger test than anything we could do here.
			self._alwaysLog(f"Envisalink OK — live session to {addr}:{port} is up, logged in, and receiving panel messages.")
			self._alwaysLog("(Skipping the active connection test — Envisalink only allows one connection at a time, and the live one is healthy.)")
			return valuesDict

		# No live session. Do a lightweight TCP reachability check (no login),
		# so we can tell the user whether the address/port is at least correct.
		import socket
		try:
			testPort = int(float(port))
		except (ValueError, TypeError):
			self._alwaysLog(f"Envisalink test: port '{port}' is not a valid number", isError=True)
			return valuesDict

		try:
			with socket.create_connection((addr, testPort), timeout=3) as s:
				# Don't send anything — just confirm the TCP handshake worked.
				# Sending a 005 login here would race with the plugin's own
				# reconnect attempts and break things.
				pass
			self._alwaysLog(
				f"Envisalink reachable — TCP connection to {addr}:{testPort} succeeded. "
				f"Save the config to apply changes; the plugin will log in on its next pass and any password/login errors will appear in the log."
			)
		except socket.timeout:
			self._alwaysLog(f"Envisalink test: timed out connecting to {addr}:{testPort}. Check the IP address, port, and network.", isError=True)
		except OSError as err:
			self._alwaysLog(f"Envisalink test: connection to {addr}:{testPort} failed: {err}", isError=True)
		except Exception as err:
			self._alwaysLog(f"Envisalink test: unexpected error: {err}", isError=True)

		return valuesDict


	def testInstallerCode(self, valuesDict):
		"""PluginConfig button callback that runs a non-destructive
		end-to-end check of the installer code:
		  1. Send *8, wait for the panel's installer-code prompt
		  2. Send the code digits, wait for the installer menu
		  3. Send ## to exit, wait for return to idle

		The actual keystroke + wait sequence runs in a background thread
		because it can take up to ~25 seconds total and Indigo's UI callback
		thread has a ~10 second timeout. The UI callback returns immediately
		with the validation result; the user watches the event log for the
		test result.

		Reads the installer code from the freshly-edited valuesDict (NOT
		self.pluginPrefs) so the user can test a code before they save the
		config. Hard refuses Envisalink.
		"""
		code = (valuesDict.get('installerCode') or '').strip()
		if not code:
			self._alwaysLog(
				"Test installer code: please enter an installer code first.",
				isError=True,
			)
			return valuesDict
		if not code.isdigit() or not 4 <= len(code) <= 6:
			self._alwaysLog(
				f"Test installer code: '{code}' is not 4-6 digits.",
				isError=True,
			)
			return valuesDict

		if not getattr(self, 'enableIT100', False):
			self._alwaysLog(
				"Test installer code: IT-100 is not enabled in the live config. "
				"This test only runs via IT-100 (Envisalink locks out keypresses "
				"in installer mode). Enable IT-100, save the config, then try again.",
				isError=True,
			)
			return valuesDict
		if not self.it100Connected:
			self._alwaysLog(
				"Test installer code: IT-100 is not connected. Save the config "
				"so the connection comes up, then try again.",
				isError=True,
			)
			return valuesDict

		# Belt-and-braces: refuse if any partition is armed.
		notReady = []
		for partition, keypadId in self.keypadList.items():
			try:
				keypad = indigo.devices[keypadId]
			except Exception:
				continue
			state = keypad.states.get('state', 'unknown')
			if state != kAlarmStateDisarmed:
				notReady.append(f"partition {partition} is in state '{state}'")
		if notReady:
			self._alwaysLog(
				"Test installer code: cannot enter installer mode — "
				+ "; ".join(notReady) + ". Disarm all partitions first.",
				isError=True,
			)
			return valuesDict

		# Refuse to start a second test while one is already running.
		if getattr(self, '_installerCodeTestRunning', False):
			self._alwaysLog(
				"Test installer code: a test is already running. Wait for it "
				"to finish (or send ## on a physical keypad to abort).",
				isError=True,
			)
			return valuesDict

		# Flush any stale keystrokes from a previous operation. Atomic-replace
		# to avoid a race with the drainer.
		oldQueue = self.it100TxCmdList
		self.it100TxCmdList = []
		if oldQueue:
			self._alwaysLog(
				f"Test installer code: flushed {len(oldQueue)} stale keystrokes from queue."
			)

		# Kick off the actual test in a background thread so this UI callback
		# returns immediately and Indigo doesn't time us out at 10s.
		self._installerCodeTestRunning = True
		t = threading.Thread(
			target=self._runInstallerCodeTest,
			args=(code,),
			name="DSCAlarm-InstallerCodeTest",
			daemon=True,
		)
		t.start()
		self._alwaysLog(
			f"Test installer code: started in background. Watch the event log "
			f"for results (typically completes in ~6-15 seconds)."
		)
		return valuesDict

	def _runInstallerCodeTest(self, code):
		"""Background-thread body for testInstallerCode. Does the actual
		send/wait/send/wait/send/wait sequence and logs the result. Wrapped
		in try/finally so the 'test running' flag is always cleared."""
		try:
			self._alwaysLog(
				f"Test installer code: --- event-driven test with code '{code}' ---"
			)

			def _sendKeys(keys, label):
				self._alwaysLog(f"  {label}: queueing keystrokes {keys}")
				for k in keys:
					if not self._sendKeystrokeToIT100(k):
						self._alwaysLog(
							f"Test installer code: IT-100 went away mid-test "
							f"(during {label}). Check the IT-100 connection.",
							isError=True,
						)
						return False
				return True

			def _emergencyBackout():
				"""Send a generous burst of # keystrokes to back out of any
				depth of installer menu we might have got into. PowerSeries
				installer mode is hierarchical (section → data → ...), so a
				single ## isn't always enough to reach idle if we got further
				in than we expected. Four #s is enough for any reasonable
				depth; sending more than needed on an idle panel is harmless."""
				for _ in range(4):
					self._sendKeystrokeToIT100('#')

			# Pre-flight: ALWAYS send ## first to back out of any latent
			# installer-mode state from a previous test. The panel can be in
			# installer mode even when the LCD shows the idle date/time
			# display (we observed this in the wild: t=0 LCD showed 'Date'/
			# 'Time' but t=1.6s after sending *8 it showed 'Enter LCD /
			# Section ---' which is the section-8 menu — exactly what *8
			# means when ALREADY in installer mode). Sending ## from idle is
			# harmless (# does nothing on a disarmed Ready panel). Sending
			# ## from installer mode exits it. Either way, after this we're
			# at idle.
			self._alwaysLog(
				"Test installer code: pre-flight — sending ## to back out of "
				"any latent installer mode before starting (harmless if "
				"panel is already at idle)."
			)
			_emergencyBackout()
			# Give the panel ~1 second to process the ##s and any LCD updates
			# before we snapshot.
			time.sleep(1.5)

			# Now check the LCD: it should show some flavour of idle/ready
			# wording. If we still see installer-mode patterns, the ## didn't
			# fully back out and the test will go wrong if we proceed.
			postflight = self._snapshotKeypadLCD()
			for partition, (l1, l2) in postflight.items():
				combined = f"{l1 or ''} {l2 or ''}".lower()
				if any(w in combined for w in ('section', 'enter lcd', 'program', 'enter data')):
					self._alwaysLog(
						f"Test installer code: ABORTED — after pre-flight "
						f"backout, p{partition} still shows {l1!r} / {l2!r}. "
						f"The panel is in a menu state that #### didn't exit. "
						f"Press # on a physical keypad until the display shows "
						f"the date/time, then try again.",
						isError=True,
					)
					return

			# Stage 1: send *8 together, wait for the panel to display the
			# installer-code prompt. Match on "installer" (case-insensitive)
			# which appears in 'Enter Your Installers Code' on PowerSeries.
			# Generous timeout because keystrokes drain at the configured
			# throttle rate.
			if not _sendKeys(['*', '8'], "stage 1 (*8 → wait for installer code prompt)"):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['installer', 'enter code'],
				failure_substrings=['invalid access', 'invalid code'],
				timeout_seconds=10,
				prefix="stage 1",
			)
			if result == 'failure':
				self._alwaysLog(
					f"Test installer code: FAILED at stage 1 — panel rejected "
					f"the keystrokes with {matched}. This is unexpected: '*8' "
					f"should bring up the installer-code prompt, not reject. "
					f"The panel may have been in an unexpected state. Backing out.",
					isError=True,
				)
				_emergencyBackout()
				return
			if result != 'success':
				self._alwaysLog(
					"Test installer code: FAILED at stage 1 — panel didn't "
					"show the installer-code prompt within 10 seconds after "
					"*8. Possible causes: IT-100 virtual keypad disabled "
					"(panel section 058), keystrokes not landing, or panel "
					"busy. Backing out with ####.",
					isError=True,
				)
				_emergencyBackout()
				return

			# Stage 2: send the code digits and watch for either the success
			# pattern ('Enter Section') or the explicit failure pattern
			# ('Invalid Access Code'). The failure pattern lets us report a
			# bad code immediately rather than waiting for the success-match
			# to time out.
			if not _sendKeys(list(code), "stage 2 (code → wait for installer menu)"):
				return
			result, matched = self._waitForLCDPrompt(
				expected_substrings=['section', 'program'],
				failure_substrings=['invalid access', 'invalid code'],
				timeout_seconds=10,
				prefix="stage 2",
			)
			if result == 'failure':
				self._alwaysLog(
					f"Test installer code: FAILED at stage 2 — installer code "
					f"'{code}' is WRONG. Panel showed {matched}. Backing out "
					f"with ####.",
					isError=True,
				)
				_emergencyBackout()
				return
			if result != 'success':
				# Generic timeout. Check if the LCD has reverted to idle —
				# if so, the panel likely timed out waiting for more
				# keystrokes (a dropped digit). That's a more useful error
				# message than "didn't show the installer menu".
				if self._lcdLooksLikeIdle(self._snapshotKeypadLCD()):
					self._alwaysLog(
						"Test installer code: FAILED at stage 2 — panel "
						"reverted to idle. Likely a keystroke was dropped "
						"between the plugin and the IT-100, so the panel "
						"never received a complete code and gave up. Try "
						"again — this issue is intermittent.",
						isError=True,
					)
				else:
					self._alwaysLog(
						"Test installer code: FAILED at stage 2 — panel didn't "
						"show the installer menu OR a wrong-code rejection within "
						"10 seconds. The panel firmware may use different prompt "
						"wording, or keystrokes may not be landing reliably. "
						"Check the LCD transitions logged above. Backing out with ####.",
						isError=True,
					)
				_emergencyBackout()
				return

			self._alwaysLog(
				"Test installer code: SUCCESS — installer code accepted. Now exiting."
			)

			# Stage 3: exit with ##. Wait for LCD to return to idle. Match on
			# typical idle-display words.
			if not _sendKeys(['#', '#'], "stage 3 (## → exit to idle)"):
				return
			result, _ = self._waitForLCDPrompt(
				expected_substrings=['date', 'ready', 'disarmed'],
				timeout_seconds=10,
				prefix="stage 3",
			)
			if result != 'success':
				self._alwaysLog(
					"Test installer code: exited installer mode but couldn't "
					"verify return to idle within 10 seconds. The panel may "
					"still be in some menu state — manually press # on a "
					"keypad if needed. The installer code itself was confirmed "
					"working in stage 2 above.",
					isError=True,
				)
				return

			self._alwaysLog(
				"Test installer code: complete. Code accepted, exited installer mode cleanly."
			)
		except Exception as err:
			self._alwaysLog(
				f"Test installer code: unexpected error in background thread: {err}. "
				f"Attempting to back out of installer mode with ####.",
				isError=True,
			)
			try:
				_emergencyBackout()
			except Exception:
				pass
		finally:
			self._installerCodeTestRunning = False


	######################################################################################
	# Communication Routines
	######################################################################################

	def calcChecksum(self, s):
		calcSum = 0
		for c in s:
			calcSum += ord(c)
		calcSum %= 256
		return calcSum

	def sendCommand(self, tx, force_interface=None):
		"""
		Queue a command for the appropriate outbound interface.
		force_interface: 'it100' or 'envisalink' overrides the configured command interface.
		If the preferred interface is not connected, falls back to whichever is available.
		"""
		# Determine which interface to use
		target = force_interface if force_interface else self.commandInterface

		if target == 'it100':
			if self.it100Connected:
				self.it100TxCmdList.append((kCmdNormal, tx))
				self.logger.threaddebug(f"Command queued for IT-100: {tx}")
				return
			elif self.envisalinkConnected:
				# Fallback
				self.envisalinkTxCmdList.append((kCmdNormal, tx))
				self.logger.warning(f"IT-100 unavailable, sending via Envisalink (fallback): {tx}")
				return
		elif target == 'envisalink':
			if self.envisalinkConnected:
				self.envisalinkTxCmdList.append((kCmdNormal, tx))
				self.logger.threaddebug(f"Command queued for Envisalink: {tx}")
				return
			elif self.it100Connected:
				# Fallback
				self.it100TxCmdList.append((kCmdNormal, tx))
				self.logger.warning(f"Envisalink unavailable, sending via IT-100 (fallback): {tx}")
				return

		self.logger.error(f"No interface available to send command: {tx}")

	def closePort(self):
		if self.port is None:
			return
		if self.port.isOpen() is True:
			self.port.close()
			self.port = None

	def openPort(self):
		self.closePort()
		if self.useSerial is False:
			#adr = self.pluginPrefs['TwoDS_Address'] + ':4025'
			adr = f"{self.pluginPrefs['TwoDS_Address']}:{int(float(self.pluginPrefs['TwoDS_Port']))}"
			self.logger.info(f"Initializing communication at address: {adr}")
			try:
				self.port = serial.serial_for_url('socket://' + adr, baudrate=115200)
			except Exception as err:
				self.logger.error(f"Error opening socket: {str(err)}")
				return False
		else:
			self.logger.info(f"Initializing communication on port {self.pluginPrefs['serialPort']}")
			try:
				self.port = serial.Serial(self.pluginPrefs['serialPort'], 9600, writeTimeout=1)
			except Exception as err:
				self.logger.error(f"Error opening serial port: {str(err)}")
				return False

		if self.port.isOpen() is True:
			self.port.flushInput()
			self.port.timeout = 1
			self.logger.info("Communication established")
			return True

		return False


	def readPort(self):
		if self.port is None:
			return ""
		if self.port.isOpen() is False:
			self.state = self.States.BOTH_INIT
			return ""

		data = ""
		try:
			data = self.port.readline()
		except Exception as err:
			self.logger.error(f"Connection RX Error: {str(err)}")
			data = '-'.encode('utf-8')   #encode in bytes to be compatible with how data are received from serial port
		except:
			self.logger.error("Connection RX Problem, DSC plugin is quitting")
			exit()
		return data


	def writePort(self, data):
		self.port.write(data)


	def sendPacketOnly(self, data):
		pkt = "{}{:02X}\r\n".format(data, self.calcChecksum(data))
		self.logger.threaddebug(f"TX: {pkt}")
		try:
			#All data is send as two-digit hex ASCII codes.
			self.writePort(pkt.encode("utf-8"))
		except Exception as err:
			self.logger.error(f"Connection TX Error: {str(err)}")
			exit()
		except:
			self.logger.error("Connection TX Problem, DSC plugin is quitting")
			exit()


	def sendPacket(self, tx, waitFor='500', rxTimeout=3, txRetries=3):
		retries = txRetries
		txCmd = tx[:3]

		while txRetries > 0:
			self.sendPacketOnly(tx)
			ourTimeout = time.time() + rxTimeout
			txRetries -= 1
			while time.time() < ourTimeout:
				if self.shutdown is True:
					return ''
				(rxCmd, rxData) = self.readPacket()

				# If rxCmd == - then the socket closed, return for re-init
				if rxCmd == '-':
					return '-'

				if rxCmd == '502':
					self.logger.error("Received system error/warning after sending command, aborting.")
					return ''

				# If rxCmd is not 0 length then we received a response
				if rxCmd:
					if waitFor == '500':
						if (rxCmd == '500') and (rxData == txCmd):
							return rxData
					elif rxCmd == waitFor:
						return rxData
			if txCmd != '000':
				self.logger.error(f"Timed out after waiting for response to command {tx} for {rxTimeout} seconds, retrying.")
		self.logger.error(f"Resent command {tx} {retries} times with no success, aborting.")
		return ''


	def processCommand(self, cmd, dat, source='unknown'):
		"""
		Process commands from either interface with deduplication
		
		Args:
			cmd: Command code
			dat: Command data
			source: 'it100' or 'envisalink'
		"""
		import time
		
		# Deduplication - ignore if we've seen this command within 1 second
		# Only deduplicate zone/partition state updates, not ACKs or other responses
		if cmd in ['609', '610', '650', '651', '652', '654', '655', '656', '657']:
			key = f"{cmd}:{dat}"
			now = time.time()
			
			if key in self.lastStateUpdate:
				if now - self.lastStateUpdate[key] < 1.0:
					self.logger.threaddebug(f"Ignoring duplicate from {source}: {cmd} {dat}")
					return
			
			self.lastStateUpdate[key] = now
		
		# Log which interface provided the update
		self.logger.threaddebug(f"Processing command from {source}: {cmd} {dat}")
		
		# Process using inline logic (readPacket handles this currently)
		# For now, return the command for readPacket to process
		return (cmd, dat)

	def _readRawLine(self, port):
		"""Read one line from a serial/socket port. Returns decoded string, '-' on close, '' on timeout."""
		try:
			data = port.readline()
			if isinstance(data, bytes):
				data = data.decode('utf-8')
			if not data:
				return ''
			if port.closed if hasattr(port, 'closed') else False:
				return '-'
			return data
		except Exception:
			return '-'

	def _parseRawLine(self, data):
		"""Parse a raw line into (cmd, dat) or None if invalid."""
		if not data:
			return None
		data = data.strip()
		m = re.search(r'^(...)(.*)(..)$', data)
		if not m:
			return None
		try:
			cmd, dat, checksum = m.group(1), m.group(2), int(m.group(3), 16)
		except Exception:
			self.logger.error("Received packet with invalid characters")
			return None
		if checksum != self.calcChecksum(cmd + dat):
			self.logger.error("Checksum mismatch on received packet")
			return None
		return (cmd, dat)

	def _isDuplicate(self, cmd, dat):
		"""Return True if this cmd+dat was already processed within the dedup window."""
		now       = time.time()
		key       = cmd + dat
		# Expire old cache entries
		self.msgDedupCache = {k: v for k, v in self.msgDedupCache.items() if now - v < self.msgDedupWindowSecs * 2}
		if key in self.msgDedupCache and (now - self.msgDedupCache[key]) < self.msgDedupWindowSecs:
			return True
		self.msgDedupCache[key] = now
		return False

	def _pollInterface(self, port, source):
		"""
		Read one line from port, parse it, deduplicate, then dispatch via readPacket logic.
		source is 'IT-100' or 'Envisalink' (used for debug logging and per-zone change attribution).

		Returns:
		  '-'           if port closed
		  ''            if no data was available (buffer empty / read timeout)
		  'dispatched'  if a packet was read and processed (caller should keep draining)
		  'skipped'     if a packet was read but ignored (duplicate, parse failure, etc.) —
		                 there might be more behind it, so caller should keep draining
		"""
		raw = self._readRawLine(port)
		if raw == '-':
			return '-'
		if not raw or not raw.strip():
			return ''  # No data — buffer empty
		parsed = self._parseRawLine(raw)
		if parsed is None:
			return 'skipped'  # Garbage on the wire; might be more behind it
		cmd, dat = parsed
		if self._isDuplicate(cmd, dat):
			if self.debugInterfaceSource:
				self.logger.debug(f"[{source}] Duplicate suppressed: {cmd} {dat}")
			return 'skipped'  # Suppressed dupe; there might be more behind it
		if self.debugInterfaceSource:
			self.logger.debug(f"[{source}] Received: {cmd} {dat}")
		# Inject into legacy readPacket by pushing onto self.port's already-read buffer isn't possible,
		# so we call the dispatch method directly with the already-parsed data.
		#
		# Wrap in try/except as a safety net: if a single packet handler
		# crashes (e.g. KeyError on an unconfigured partition), we don't
		# want it to take down runConcurrentThread and trigger Indigo's
		# 10-second restart. Log and continue draining — the next packet
		# is probably fine.
		try:
			self._dispatchMessage(cmd, dat, source)
		except Exception as err:
			self.logger.error(
				f"[{source}] Error handling packet {cmd} {dat!r}: "
				f"{type(err).__name__}: {err}. Continuing to drain the "
				f"queue."
			)
		return 'dispatched'

	def readPacket(self):
		"""Legacy single-interface read — used during handshake/init states and sendPacket."""
		data = self.readPort()
		
		if isinstance(data, bytes):
			data = data.decode('utf-8')
		
		if not data:
			return ('', '')
		elif data == '-':
			self.logger.debug("Socket has closed")
			return ('-', '')

		data = data.strip()
		m = re.search(r'^(...)(.*)(..)$', data)
		if not m:
			return ('', '')

		try:
			self.logger.threaddebug(f"RX: {data}")
			(cmd, dat, checksum) = (m.group(1), m.group(2), int(m.group(3), 16))
		except Exception:
			self.logger.error("IT-100/Envisalink Error: Received a response with invalid characters")
			return ('', '')

		if checksum != self.calcChecksum(cmd + dat):
			self.logger.error("Checksum did not match on a received packet.")
			return ('', '')

		# Legacy path: this is only hit during handshake/init or as a fallback inside sendPacket,
		# which talks to whichever interface is configured for outgoing commands. Use that as the
		# source attribution so any zone changes seen here are still tagged sensibly.
		legacySource = 'IT-100' if getattr(self, 'commandInterface', 'envisalink') == 'it100' else 'Envisalink'
		self._dispatchMessage(cmd, dat, legacySource)
		return (cmd, dat)

	def _dispatchMessage(self, cmd, dat, source='Unknown'):
		"""Dispatch a parsed (cmd, dat) pair to the appropriate handler. Called from both readPacket
		and _pollInterface so both interfaces share identical message handling.

		`source` is 'IT-100', 'Envisalink' or 'Unknown' and is propagated to zone state changes
		so the log can attribute each change to the interface that delivered it."""
		##################################################################################
		# Parse responses based on cmd value received from panel
		##################################################################################

		if cmd == '500':
			self.logger.threaddebug(f"ACK for cmd {dat}.")
			self.cmdAck = dat

		elif cmd == '501':
			self.logger.error("IT-100/Envisalink Error: Received a command with a bad checksum")

		elif cmd == '502':
			errText = 'Unknown'

			if dat == '001':
				errText = 'Receive Buffer Overrun (a command is received while another is still being processed)'
			elif dat == '002':
				errText = 'Receive Buffer Overflow'
			elif dat == '003':
				errText = 'Transmit Buffer Overflow'

			elif dat == '010':
				errText = 'Keybus Transmit Buffer Overrun'
			elif dat == '011':
				errText = 'Keybus Transmit Time Timeout'
			elif dat == '012':
				errText = 'Keybus Transmit Mode Timeout'
			elif dat == '013':
				errText = 'Keybus Transmit Keystring Timeout'
				# this error is sometimes received after disarming a tripped partition with TPI disarm command 040
			elif dat == '014':
				errText = 'Keybus Interface Not Functioning (the TPI cannot communicate with the security system)'
			elif dat == '015':
				errText = 'Keybus Busy (Attempting to Disarm or Arm with user code)'
			elif dat == '016':
				errText = 'Keybus Busy – Lockout (The panel is currently in Keypad Lockout – too many disarm attempts)'
			elif dat == '017':
				errText = 'Keybus Busy – Installers Mode (Panel is in installers mode, most functions are unavailable)'
			elif dat == '018':
				errText = 'Keybus Busy – General Busy (The requested partition is busy)'

			elif dat == '020':
				errText = 'API Command Syntax Error'
			elif dat == '021':
				errText = 'API Command Partition Error (Requested Partition is out of bounds)'
			elif dat == '022':
				errText = 'API Command Not Supported'
			elif dat == '023':
				errText = 'API System Not Armed (sent in response to a disarm command)'
			elif dat == '024':
				errText = 'API System Not Ready to Arm (not secure, in delay, or already armed)'
				self.triggerEvent('eventFailToArm')
				self.speak('speakTextFailedToArm')
			elif dat == '025':
				errText = 'API Command Invalid Length'
			elif dat == '026':
				errText = 'API User Code not Required'
			elif dat == '027':
				errText = 'API Invalid Characters in Command'

			if dat in {'023', '024'}:
				self.logger.warning(f"IT-100/Envisalink Warning ({dat}): {errText}")
			else:
				self.logger.error(f"IT-100/Envisalink Error ({dat}): {errText}")

		elif cmd == '505':
			if dat == '3':
				self.logger.debug("Received login request")

		elif cmd == '510':
			# Keypad LED State Updates for Partition 1 only
			leds = int(dat, 16)

			if leds & 1 > 0:
				self.updateKeypad(1, 'LEDReady', 'on')
			else:
				self.updateKeypad(1, 'LEDReady', 'off')

			if leds & 2 > 0:
				self.updateKeypad(1, 'LEDArmed', 'on')
			else:
				self.updateKeypad(1, 'LEDArmed', 'off')

			if leds & 16 > 0:
				self.updateKeypad(1, 'LEDTrouble', 'on')
			else:
				self.updateKeypad(1, 'LEDTrouble', 'off')

			if leds & 4 > 0:
				self.updateKeypad(1, 'LEDMemory', 'on')
			else:
				self.updateKeypad(1, 'LEDMemory', 'off')

			if leds & 8 > 0:
				self.updateKeypad(1, 'LEDBypass', 'on')
			else:
				self.updateKeypad(1, 'LEDBypass', 'off')

		elif cmd == '511':
			# Keypad LED Flashing State Update
			# Same as 510 above but means an LED is flashing
			# We don't use this right now
			pass

		elif cmd == '550':
			# This command is send by DSC panel every 4 min
			m = re.search(r'^(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)$', dat)
			if m:
				tHour = m.group(1).zfill(2)   #padding str objects to two digits
				tMin = m.group(2).zfill(2)
				dMonth = m.group(3).zfill(2)
				dMonthDay = m.group(4).zfill(2)
				dYear = m.group(5)
				self.logger.debug(f"Received alarm panel time and date {tHour}:{tMin} {dMonth}-{dMonthDay}-{dYear}")

				if self.configKeepTimeSynced is True:
					# Is it around 3 am and the time has not recently been synced?
					d = datetime.now()
					if self.timesyncflag is True and (d.hour == 3) and (d.minute in range(0, 6)):
						self.logger.debug("Syncing alarm panel time and date.")
						# No action object available here (we're in cmd 550
						# handling, not an Indigo action callback), so let
						# sendCommand pick the interface from plugin prefs.
						self.sendCommand(f"010{d.strftime('%H%M%m%d%y')}")
						self.timesyncflag = False
					else:
						self.timesyncflag = True
						self.logger.debug("No time sync necessary.")

				# If this is a 2DS/Envisalink interface then lets insert the time in the virtual keypad 
				# Time is updated only every 4 minutes, so not very useful to use
				if self.useSerial is False:
					tAmPm = 'a'
					tHour = int(tHour)
					if tHour >= 12:
						tAmPm = 'p'

					if tHour > 12:
						tHour -= 12
					elif tHour == 0:
						tHour = 12
					str(tHour).zfill(2)
					self.updateKeypad(0, 'LCDLine1', '  Date     Time ')
					self.updateKeypad(0, 'LCDLine2', f"{kMonthList[int(dMonth)-1]} {dMonthDay}/{dYear} {tHour}:{tMin}{tAmPm}")
					self.logger.debug(f"{kMonthList[int(dMonth)-1]} {dMonthDay}/{dYear} {tHour}:{tMin}{tAmPm}")

		elif cmd == '560':
			self.logger.info("Telephone Ring Tone Has Been Detected.")
			for trig in self.triggerList:
				trigger = indigo.triggers[trig]
				if trigger.pluginTypeId == 'eventNoticeTelephone_Ring':
					indigo.trigger.execute(trigger.id)

		elif cmd == '561' or cmd == '562':
			m = re.search(r'^(.)(...)$', dat)
			if m:
				(sensor, temp) = (int(m.group(1)), int(m.group(2)))
				if cmd == '562':
					self.updateSensorTemp(sensor, 'outside', temp, source=source)
				else:
					self.updateSensorTemp(sensor, 'inside', temp, source=source)

		elif cmd == '563':
			m = re.search(r'^(.)(...)(...)$', dat)
			if m:
				(sensor, cool, heat) = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
				self.updateSensorTemp(sensor, 'cool', cool, source=source)
				self.updateSensorTemp(sensor, 'heat', heat, source=source)

		elif cmd == '601':
			# a zone goes into alarm/is tripped
			m = re.search(r'^(.)(...)$', dat)
			if m:
				(partition, zone) = (int(m.group(1)), int(m.group(2)))
				self._setZoneAlarmState(zone, kAlarmStateTripped, source=source)

				if not (self.trippedZoneList):
					if "DSC_Alarm_Memory" in indigo.variables:
						indigo.variable.updateValue("DSC_Alarm_Memory", value="")
				if zone not in self.trippedZoneList:
					self.trippedZoneList.append(zone)
					self.sendZoneTrippedEmail()
					indigoVar = ""
					for zoneNum in self.trippedZoneList:
						zone = indigo.devices[self.zoneList[zoneNum]]
						indigoVar += (zone.name + "; ")
					if "DSC_Alarm_Memory" in indigo.variables:
						indigo.variable.updateValue("DSC_Alarm_Memory", indigoVar)
					self.triggerEvent('eventZoneTripped')

		elif cmd == '602':
			m = re.search(r'^(.)(...)$', dat)
			if m:
				(partition, zone) = (int(m.group(1)), int(m.group(2)))
				dev = indigo.devices[self.keypadList[partition]]
				zonedev = indigo.devices[self.zoneList[zone]]
				keyp = str(dev.pluginProps['partitionName'])
				srcSuffix = f" (via {source})" if (self.debugInterfaceSource and source and source != 'Unknown') else ""
				self.logger.info(f"Zone '{zonedev.name}' Restored. (Partition {partition} '{keyp}'){srcSuffix}")
				if zonedev.states.get('alarmState') == kAlarmStateTripped:
					self._setZoneAlarmState(zone, kAlarmStateMemory, source=source)

		elif cmd == '603':
			m = re.search(r'^(.)(...)$', dat)
			if m:
				(partition, zone) = (int(m.group(1)), int(m.group(2)))
				self.logger.debug(f"Zone Number {zone} Has a Tamper Condition.")

		elif cmd == '604':
			m = re.search(r'^(.)(...)$', dat)
			if m:
				(partition, zone) = (int(m.group(1)), int(m.group(2)))
				self.logger.debug(f"Zone Number {zone} Tamper Condition has been Restored.")

		elif cmd == '605':
			zone = int(dat)
			self.logger.debug(f"Zone Number {zone} Has a Fault Condition.".format(zone))

		elif cmd == '606':
			zone = int(dat)
			self.logger.debug(f"Zone Number {zone} Fault Condition has been Restored.".format(zone))

		elif cmd == '609':
			zone = int(dat)
			self.logger.debug(f"Zone Number {zone} Open.".format(zone))
			if self.resyncCollecting:
				self.resyncOpenZones.add(zone)
			self.updateZoneState(zone, kZoneStateOpen, source=source)
			if self.repeatAlarmTripped is True:
				if zone in self.closeTheseZonesList:
					self.closeTheseZonesList.remove(zone)

		elif cmd == '610':
			zone = int(dat)
			self.logger.debug(f"Zone Number {zone} Closed.")
			# Default behaviour (v46+): always apply zone-close updates
			# immediately so downstream automation sees real sensor state
			# even during a trip.
			#
			# Legacy "latch zones during alarm" behaviour is now opt-in
			# via configLatchZonesDuringAlarm: when ON, zone-close events
			# during an active trip get deferred to closeTheseZonesList
			# and replayed on disarm. That preserves a snapshot of every
			# zone that opened during the break-in, which is useful for
			# control pages — but it breaks downstream automation that
			# expects accurate real-time state.
			# Zone closes always apply immediately; alarm involvement is kept
			# separately in alarmState, so the live state never has to lie.
			if True:
				self.updateZoneState(zone, kZoneStateClosed, source=source)
			else:
				self.closeTheseZonesList.append(zone)
				# Log every deferred close. If repeatAlarmTripped sticks at
				# True after a disarm (the bug we hunted in v45), this log
				# line will fire for every zone-close while the plugin is
				# stuck, making the symptom visible in the log instead of
				# silently losing zone updates.
				heldSecs = time.time() - self.repeatAlarmTrippedSetAt if self.repeatAlarmTrippedSetAt else 0
				self.logger.info(
					f"Zone {zone} close DEFERRED (deferred-list size now "
					f"{len(self.closeTheseZonesList)}) because "
					f"repeatAlarmTripped is True (set {heldSecs:.0f}s ago) "
					f"and 'Latch zones during alarm' is enabled in config. "
					f"This is expected for a few minutes after a real "
					f"trip+disarm; if you see this persisting, the disarm "
					f"cmd didn't clear the flag."
				)

		elif cmd == '616':
			# cmd is sent after a zone is bypassed or bypass is cancelled.
			# This dump can be forced with keypress command [1*1#] if partition is not armed. 
			# If partition is armed, it will switch armed state from stay to away and vice versa.
			# Routine that identifies bypassed zones and updates zone status kZoneBypassNo 
			# or kZoneBypassYes via newState (unfortunately for partition 1 only).
			BypassHexDump = str(dat)       #this is the 16-digit hex string for bypassed zones. Partition 1 only!
			self.logger.debug(f"Bypass Hex Dump ({BypassHexDump})")
			BBDump = format(int(BypassHexDump, 16), '0>64b')
			BypassBinaryDump = "".join(["".join([m[i:i+1] for i in range(8-1, -1, -1)]) for m in [BBDump[i:i+8] for i in range(0, len(BBDump), 8)]])
			# reordered in 8 bit words to get zones in 1-64 order
			self.logger.debug(f"Bypass Binary Dump ({BypassBinaryDump})")
			for i in range(64):
				if BypassBinaryDump[i-1] == '1':
					self.updateZoneBypass(i, kZoneBypassYes, source=source)
				elif BypassBinaryDump[i-1] == '0':
					self.updateZoneBypass(i, kZoneBypassNo, source=source)

		elif cmd == '620':
			self.logger.warning("Duress Alarm Detected")
			self.sendDuressEmail("Duress Alarm Detected")
			# This updates all keypads (partitions)
			self.updateKeypad(0, 'PanicState', kPanicStateDuress)

		elif cmd == '621':
			self.logger.warning("Fire Key Alarm Detected")
			self.sendPanicEmail("Fire Key Alarm Detected")
			# This updates all keypads (partitions)
			self.updateKeypad(0, 'PanicState', kPanicStateFire)

		elif cmd == '622':
			self.logger.info("Fire Key Alarm Restored")
			# This updates all keypads (partitions). Partitions that were Armed will stay Armed. 
			# Partitions that are "Ready for arming" will show up as Disarmed via cmd 650. 
			# Otherwise they will show up as Tripped until they become "Ready", e.g. all open windows are closed.
			self.updateKeypad(0, 'PanicState', kPanicStateNone)
			
			# After the fire alarm has been disarmed while it was tripped, update any zone states
			# that were closed during the fire alarm.  We don't update them during the event
			# so that Indigo's zone states will represent a zone as tripped during the entire event.
			if self.repeatAlarmTripped is True:
				self._setRepeatAlarmTripped(False, "cmd 622 (fire alarm restored)")
				for zone in self.closeTheseZonesList:
					self.updateZoneState(zone, kZoneStateClosed, source=source)

				self.closeTheseZonesList = []

		elif cmd == '623':
			self.logger.warning("Auxiliary/Medical Key Alarm Detected")
			self.sendPanicEmail("Ambulance/Medical Key Alarm Detected")
			# This updates all keypads (partitions)
			self.updateKeypad(0, 'PanicState', kPanicStateAmbulance)

		elif cmd == '624':
			self.logger.info("Auxiliary/Medical Key Alarm Restored")
			# This updates all keypads (partitions)
			self.updateKeypad(0, 'PanicState', kPanicStateNone)

		elif cmd == '625':
			self.logger.warning("Panic/Police Key Alarm Detected")
			self.sendPanicEmail("Panic/Police Key Alarm Detected")
			# This updates all keypads (partitions)
			self.updateKeypad(0, 'PanicState', kPanicStatePanic)

		elif cmd == '626':
			self.logger.info("Panic/Police Key Alarm Restored")
			# This updates all keypads (partitions). Partitions that were Armed will stay Armed. 
			# Partitions that are "Ready for arming" will show up as Disarmed via cmd 650. 
			# Otherwise they will show up as Tripped until they become "Ready", e.g. all open windows are closed.
			self.updateKeypad(0, 'PanicState', kPanicStateNone)

			# After the panic alarm has been disarmed while it was tripped, update any zone states
			# that were closed during the panic alarm.  We don't update them during the event
			# so that Indigo's zone states will represent a zone as tripped during the entire event.
			if self.repeatAlarmTripped is True:
				self._setRepeatAlarmTripped(False, "cmd 626 (panic alarm restored)")
				for zone in self.closeTheseZonesList:
					self.updateZoneState(zone, kZoneStateClosed, source=source)

				self.closeTheseZonesList = []
				
		elif cmd == '631':
			self.logger.warning("Auxiliary/Smoke Input Alarm Detected")
			self.updateKeypad(0, 'PanicState', kPanicStateSmoke)

		elif cmd == '632':
			self.logger.info("Auxiliary/Smoke Input Alarm Restored")
			# This updates all keypads (partitions)
			self.updateKeypad(0, 'PanicState', kPanicStateNone)

		elif cmd == '650':
			# This reports "Ready" state only for keypads i.e. partitions that are ready to arm.
			self.logger.debug(f"Partition {int(dat)} Ready")
			partition = int(dat)
			self.updateKeypad(partition, 'ReadyState', kReadyStateTrue)
			self.updateKeypad(partition, 'LEDReady', 'on')
			self.updateKeypad(partition, 'state', kAlarmStateDisarmed)
			# If we still think the alarm is tripped but the panel is now
			# reporting Ready, the alarm cycle is over and the user either
			# disarmed via a path we missed OR the panel's bell-cutoff
			# timer expired without sending a user-disarm cmd. Either way,
			# the flag needs to be cleared — otherwise cmd 610 (zone
			# closed) defers all future zone-closes to closeTheseZonesList
			# forever and motion sensors etc. get stuck showing 'open'.
			# Discovered via real-world report (v44): bell-cutoff timeout
			# leaves the plugin stuck on 'tripped' for hours until a
			# plugin restart, because no disarm cmd ever arrives.
			if self.repeatAlarmTripped:
				heldSecs = time.time() - self.repeatAlarmTrippedSetAt if self.repeatAlarmTrippedSetAt else 0
				self._alwaysLog(
					f"cmd 650 (partition {partition} Ready): panel reports "
					f"Ready while we still thought the alarm was tripped "
					f"(flag held for {heldSecs:.0f}s). Clearing the flag "
					f"and applying {len(self.closeTheseZonesList)} deferred "
					f"zone-close(s). This usually means either the user "
					f"disarmed via a path that didn't send cmd 750/655/751, "
					f"or the panel's bell-cutoff timer expired without a "
					f"user disarm.",
				)
				self.alarmAcknowledgedPartitions.add(partition)
				self._setRepeatAlarmTripped(False, f"cmd 650 (partition {partition} returned to Ready while flag was still set)")
				for zone in self.closeTheseZonesList:
					try:
						self.updateZoneState(zone, kZoneStateClosed, source=source)
					except Exception as err:
						self.logger.error(
							f"cmd 650: failed to apply deferred close "
							f"for zone {zone}: {err}"
						)
				self.closeTheseZonesList = []

		elif cmd == '651':
			self.logger.debug(f"Partition {int(dat)} Not Ready")
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			self.updateKeypad(partition, 'ReadyState', kReadyStateFalse)
			self.updateKeypad(partition, 'LEDReady', 'off')
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)   # green circle
				#dev.updateStateImageOnServer(indigo.kStateImageSel.Unlocked)  # red open padlock
			else:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

		elif cmd == '652':
			if len(dat) == 1:
				partition = int(dat)
				dev = indigo.devices[self.keypadList[partition]]
				keyp = str(dev.pluginProps['partitionName'])
				self.logger.debug(f"Alarm Panel Armed. (Partition {partition} '{keyp}')")
				# Fresh arming cycle — clear acknowledged flag.
				self.alarmAcknowledgedPartitions.discard(partition)
				self.updateKeypad(partition, 'ReadyState', kReadyStateFalse)
				self.updateKeypad(partition, 'LEDReady', 'off')
				self.updateKeypad(partition, 'LEDArmed', 'on')	  # updates LEDs for partitions 1-8
				self.speak('speakTextArmed')
				self.trippedZoneList = []
				self._clearAlarmMemory(partition)
				if self.configUseCustomIcons is True:
					dev.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)  # red circle
					#dev.updateStateImageOnServer(indigo.kStateImageSel.Locked)  # green locked padlock
				else:
					dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
				

			elif len(dat) == 2:
				m = re.search(r'^(.)(.)$', dat)
				if m:
					(partition, mode) = (int(m.group(1)), int(m.group(2)))
					dev = indigo.devices[self.keypadList[partition]]
					keyp = str(dev.pluginProps['partitionName'])
					self.logger.info(f"Alarm Panel Armed in {kArmedModeList[mode]} Mode. (Partition {partition} '{keyp}')")
					# Fresh arming cycle — clear acknowledged flag.
					self.alarmAcknowledgedPartitions.discard(partition)
					if (mode == 0) or (mode == 2):
						armedEvent = 'armedAway'
						self.updateKeypad(partition, 'state', kAlarmStateArmedAway)
						self.updateKeypad(partition, 'ArmedState', kAlarmArmedStateAway)
					else:
						armedEvent = 'armedStay'
						self.updateKeypad(partition, 'state', kAlarmStateArmedStay)
						self.updateKeypad(partition, 'ArmedState', kAlarmArmedStateStay)
						self.updateKeypad(partition, 'LEDBypass', 'on')   # LED is on since motion sensors are bypassed in Stay mode

					self.triggerEvent(armedEvent)
					for trig in self.triggerList:
						trigger = indigo.triggers[trig]
						if trigger.pluginTypeId == 'eventPartitionArmed':
							if trigger.pluginProps['partitionNum'] == str(partition):
								indigo.trigger.execute(trigger.id)

					self.updateKeypad(partition, 'ReadyState', kReadyStateFalse)
					self.updateKeypad(partition, 'LEDReady', 'off')
					self.updateKeypad(partition, 'LEDArmed', 'on')	  # updates LEDs for partitions 1-8
					self.speak('speakTextArmed')
					self.trippedZoneList = []
					self._clearAlarmMemory(partition)
					if self.configUseCustomIcons is True:
						dev.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)  # red circle
						#dev.updateStateImageOnServer(indigo.kStateImageSel.Locked)  # green locked padlock
					else:
						dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
	

		elif cmd == '653':
			# Partition Ready - Forced Arming Enabled
			# This reports ready state only for keypads i.e. partitions that are ready.
			self.logger.debug(f"Partition {int(dat)} Ready")
			partition = int(dat)
			self.updateKeypad(partition, 'ReadyState', kReadyStateTrue)
			self.updateKeypad(partition, 'LEDReady', 'on')
			self.updateKeypad(partition, 'state', kAlarmStateDisarmed)

		elif cmd == '654':
			# partition is in alarm due to zone violations or panic & fire alarm
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])

			# If the user has already disarmed this partition after the trip,
			# the panel may still broadcast 654 from its alarm-memory state
			# (PowerSeries does this for several minutes after the bell
			# cutoff). Don't revert the disarm in that case — log it and
			# move on.
			if partition in self.alarmAcknowledgedPartitions:
				self.logger.info(
					f"Alarm Tripped re-broadcast suppressed for partition {partition} "
					f"'{keyp}' — user already disarmed (panel is replaying alarm memory)."
				)
				return

			# Historical-alarm detection: if the plugin started recently AND
			# the partition is CURRENTLY showing ready/disarmed (i.e. the
			# alarm is no longer active right now), this cmd 654 broadcast
			# is almost certainly a replay of a trip that happened while
			# the plugin was offline. The panel keeps broadcasting cmd 654
			# from alarm memory for several minutes after a bell cutoff,
			# and on plugin start we won't have the partition in
			# alarmAcknowledgedPartitions yet so the normal suppression
			# doesn't fire.
			#
			# Without this check the user gets a false-positive "ALARM
			# TRIPPED!" notification (email + trigger) for an alarm that's
			# already over and possibly hours old. With it, we instead
			# notify with a clearly-different "HISTORICAL ALARM" message,
			# OR suppress entirely if the user has chosen that in config.
			if self._isHistoricalAlarmReplay(partition):
				self._handleHistoricalAlarmDetected(partition, keyp)
				return

			self.logger.warning(f"Alarm TRIPPED! (Partition {partition} '{keyp}')")
			self.updateKeypad(partition, 'state', kAlarmStateTripped)
			self.updateKeypad(partition, 'LEDMemory', 'on')	  # updates LED for partitions 1-8
			self.triggerEvent('eventAlarmTripped')
			self.repeatAlarmTrippedNext = time.time()
			self._setRepeatAlarmTripped(True, f"cmd 654 (partition {partition} in alarm)")

		elif cmd == '655':
			# This command is send after user disarms an alarm that was not tripped (also see cmd 750, 751).
			# This is only disarm cmd sent if arming is cancelled during exit delay (which also cancels zone bypass)
			# If the alarm has been disarmed while it was tripped, update any zone states
			# that were closed during the break in.  We don't update them during the event
			# so that Indigo's zone states will represent a zone as tripped during the entire event.
			if self.repeatAlarmTripped is True:
				# Mark partition as acknowledged so subsequent cmd 654
				# re-broadcasts from panel alarm memory don't revert us.
				try:
					self.alarmAcknowledgedPartitions.add(int(dat))
				except (ValueError, TypeError):
					pass
				self._setRepeatAlarmTripped(False, "cmd 655 (disarm from exit delay)")
				for zone in self.closeTheseZonesList:
					self.updateZoneState(zone, kZoneStateClosed, source=source)


				self.closeTheseZonesList = []

			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			keypstate = str(dev.states['state'])
			if keypstate == kAlarmStateExitDelay:
				self.logger.info(f"Alarm Disarmed during Exit Delay. (Partition {partition} '{keyp}')")

			self.logger.debug("Bypass Cancelled for all Zones by DSC")
			for i in range(64):
				self.updateZoneBypass(i, kZoneBypassNo, source=source)

			self.trippedZoneList = []
			self.updateKeypad(partition, 'state', kAlarmStateDisarmed)
			self.updateKeypad(partition, 'ArmedState', kAlarmArmedStateDisarmed)
			self.updateKeypad(partition, 'LEDArmed', 'off')
			self.updateKeypad(0, 'PanicState', kPanicStateNone)
			self.updateKeypad(partition, 'ReadyState', kReadyStateTrue)
			self.updateKeypad(partition, 'LEDReady', 'on')
			self.updateKeypad(partition, 'LEDBypass', 'off')
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)  # green circle
				#dev.updateStateImageOnServer(indigo.kStateImageSel.Unlocked)  # red open padlock

			#self.triggerEvent('eventAlarmDisarmed') #use cmd 750 & 751 triggers
			#self.speak('speakTextDisarmed')  #use cmd 750 & 751 triggers


		elif cmd == '656':
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.info(f"Exit Delay. (Partition {partition} '{keyp}')")
			# New arming cycle starting — clear the alarm-acknowledged flag
			# so that if the panel trips during this arming, the trip is
			# treated as a fresh event rather than a leftover re-broadcast.
			self.alarmAcknowledgedPartitions.discard(partition)
			self.updateKeypad(partition, 'state', kAlarmStateExitDelay)
			self.updateKeypad(partition, 'LEDArmed', 'on')
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.TimerOn)
			self.updateKeypad(partition, 'LEDMemory', 'off')
			if "DSC_Alarm_Memory" in indigo.variables:
				indigo.variable.updateValue("DSC_Alarm_Memory", value="no tripped zones")
			self.speak('speakTextArming')

		elif cmd == '657':
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.info(f"Entry Delay. (Partition {partition} '{keyp}')")
			self.updateKeypad(partition, 'state', kAlarmStateEntryDelay)
			self.speak('speakTextEntryDelay')
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.TimerOn)

		elif cmd == '663':
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.info(f"Keypad Chime Enabled. (Partition {partition} '{keyp}')")
			self.updateKeypad(partition, 'KeypadChime', kKeypadStateChimeEnabled)

		elif cmd == '664':
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.info(f"Keypad Chime Disabled. (Partition {partition} '{keyp}')")
			self.updateKeypad(partition, 'KeypadChime', kKeypadStateChimeDisabled)

		elif cmd == '672':
			partition = int(dat)
			if partition not in self.keypadList:
				self.logger.debug(
					f"cmd 672 (Failed to Arm) for partition {partition} — "
					f"no keypad device configured for this partition, "
					f"ignoring."
				)
				return
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.warning(f"Alarm Panel Failed to Arm. (Partition {partition} '{keyp}')")
			self.triggerEvent('eventFailToArm')
			self.speak('speakTextFailedToArm')

		elif cmd == '673':
			#sends busy reply for partitions that are not defined by the plugin
			partition = int(dat)
			self.logger.debug(f"Partition {partition} Busy/Not defined by plugin.")

		elif cmd == '700':
			# A partition has been armed by a user – sent at the end of exit delay
			# No info on whether stay armed or away armed, but cmd 652 is also triggered and has this info
			m = re.search(r'^(.)..(..)$', dat)
			if m:
				(partition, user) = (int(m.group(1)), m.group(2))
				dev = indigo.devices[self.keypadList[partition]]
				keyp = str(dev.pluginProps['partitionName'])
				keyu = self.userLabelDict.get(user, "")
				if keyu:
					keyu = f" '{keyu}'"
				self.logger.info(f"Alarm Panel Armed by User {user}{keyu}. (Partition {partition} '{keyp}')")
				self.updateKeypad(partition, 'ReadyState', kReadyStateFalse)
				self.updateKeypad(partition, 'LEDReady', 'off')
				self.updateKeypad(partition, 'LEDArmed', 'on')
				self.speak('speakTextArmed')
				if self.configUseCustomIcons is True:
					dev.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)  # red circle
					#dev.updateStateImageOnServer(indigo.kStateImageSel.Locked)  # green locked padlock
				for trig in self.triggerList:
					trigger = indigo.triggers[trig]
					if trigger.pluginTypeId == 'userArmed':
						if trigger.pluginProps['userCode'] == user:
							indigo.trigger.execute(trigger.id)

		elif cmd == '701':
			# Special arming, e.g. IndigoTouch on iPhone, Keyswitch, etc
			# No info on whether stay armed or away armed, but cmd 652 is also triggered and has this info
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.debug(f"Alarm Panel Specially Armed. (Partition {partition} '{keyp}')")
			self.updateKeypad(partition, 'ReadyState', kReadyStateFalse)
			self.updateKeypad(partition, 'LEDReady', 'off')
			self.updateKeypad(partition, 'LEDArmed', 'on')
			self.speak('speakTextArmed')
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)  # red circle
				#dev.updateStateImageOnServer(indigo.kStateImageSel.Locked)  # green locked padlock


		elif cmd == '702':
			# A partition has been armed but one or more zones have been bypassed
			# No info on whether stay armed or away armed, but cmd 652 is also triggered and has this info
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.info(f"Alarm Panel Armed. (Partition {partition} '{keyp}' with zone(s) bypass)")
			self.updateKeypad(partition, 'ReadyState', kReadyStateFalse)
			self.updateKeypad(partition, 'LEDReady', 'off')
			self.updateKeypad(partition, 'LEDArmed', 'on')
			self.updateKeypad(partition, 'LEDBypass', 'on')
			self.speak('speakTextArmed')
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)  # red circle
				#dev.updateStateImageOnServer(indigo.kStateImageSel.Locked)  # green locked padlock


		elif cmd == '750':
			# this command is send after user disarms an alarm that was tripping or not tripping (vs cmd 655).
			# this command is not sent after cancelling a fire or panic/police alarm. In fact no commands are sent at all in this case.
			#
			# Per the IT-100 docs, dat = partition(1) + user(4) = 5 chars total.
			# Some panels send slightly different formats though, so we
			# extract defensively: partition is always the first char, and
			# we try the documented 5-char format for the user but tolerate
			# variations rather than silently dropping the disarm entirely.
			partition = None
			user = None
			m = re.search(r'^(.)..(..)$', dat)
			if m:
				partition = int(m.group(1))
				user = m.group(2)
			elif len(dat) >= 1 and dat[0].isdigit():
				# Fallback: extract partition from first char, leave user blank.
				# Better to clear state with an unknown user than to silently
				# do nothing and leave the partition stuck at 'tripped'.
				try:
					partition = int(dat[0])
					user = '??'
					self.logger.warning(
						f"cmd 750 had unexpected data format dat={dat!r}; "
						f"extracted partition {partition} only. Disarm will "
						f"be processed but user code is unknown."
					)
				except (ValueError, TypeError):
					self.logger.error(
						f"cmd 750 received with unparseable data dat={dat!r}. "
						f"Disarm event NOT processed — state may stick at "
						f"'tripped'."
					)
					return

			if partition is not None:
				dev = indigo.devices[self.keypadList[partition]]
				keyp = str(dev.pluginProps['partitionName'])
				keyu = self.userLabelDict.get(user, "") if user else ""
				if keyu:
					keyu = f" '{keyu}'"
				self.logger.info(f"Alarm Panel Disarmed by User {user}{keyu}. (Partition {partition} '{keyp}')")
				self.sendEmailDisarm(f"Alarm Panel Disarmed by User {user}{keyu}. (Partition {partition} '{keyp}')")
				if "DSC_Last_User_Disarm" in indigo.variables:
					indigo.variable.updateValue("DSC_Last_User_Disarm", value="User "+str(user or '??')+keyu)

				# self.trippedZoneList = []    # We do not want to delete list of tripped zones here
				self.updateKeypad(partition, 'state', kAlarmStateDisarmed)
				self.updateKeypad(partition, 'ArmedState', kAlarmArmedStateDisarmed)
				self.updateKeypad(partition, 'LEDArmed', 'off')
				self.updateKeypad(0, 'PanicState', kPanicStateNone)
				self.updateKeypad(partition, 'ReadyState', kReadyStateTrue)
				self.updateKeypad(partition, 'LEDReady', 'on')
				self.updateKeypad(partition, 'LEDBypass', 'off')
				self.triggerEvent('eventAlarmDisarmed')
				self.speak('speakTextDisarmed')
				if self.configUseCustomIcons is True:
					dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)   # green circle
					#dev.updateStateImageOnServer(indigo.kStateImageSel.Unlocked)  # red open padlock

				for trig in self.triggerList:
					trigger = indigo.triggers[trig]
					if trigger.pluginTypeId == 'userDisarmed':
						if trigger.pluginProps['userCode'] == user:
							indigo.trigger.execute(trigger.id)

				for trig in self.triggerList:
					trigger = indigo.triggers[trig]
					if trigger.pluginTypeId == 'userDisarmedPartition':
						if trigger.pluginProps['userCode'] == user and trigger.pluginProps['partitionNum'] == str(partition):
							indigo.trigger.execute(trigger.id)

				for trig in self.triggerList:
					trigger = indigo.triggers[trig]
					if trigger.pluginTypeId == 'eventPartitionDisarmed':
						if trigger.pluginProps['partitionNum'] == str(partition):
							indigo.trigger.execute(trigger.id)

			# If the alarm has been disarmed while it was tripped, update any zone states
			# that were closed during the break in.  We don't update them during the event
			# so that Indigo's zone states will represent a zone as tripped during the entire event.
			if self.repeatAlarmTripped is True:
				# Mark partition as acknowledged so subsequent cmd 654
				# re-broadcasts from panel alarm memory don't revert us.
				if partition is not None:
					self.alarmAcknowledgedPartitions.add(partition)
				self._setRepeatAlarmTripped(False, f"cmd 750 (disarm by user{', partition=' + str(partition) if partition is not None else ', partition unknown'})")
				for zone in self.closeTheseZonesList:
					self.updateZoneState(zone, kZoneStateClosed, source=source)

				self.closeTheseZonesList = []

			#Disarming cancels all bypassed zones automatically by DSC. So just need to update plugin zone states.
			self.logger.debug("Bypass Cancelled for all Zones by DSC")
			for i in range(64):
				self.updateZoneBypass(i, kZoneBypassNo, source=source)


		elif cmd == '751':
			#special opening (triggered by keyswitch but not by Indigo Touch). A DSC/Envisalink bug seems to not send this cmd after key fob opening
			partition = int(dat)
			dev = indigo.devices[self.keypadList[partition]]
			keyp = str(dev.pluginProps['partitionName'])
			self.logger.info(f"Alarm Disarmed by Special Opening (Partition {partition} '{keyp}')")
			# self.trippedZoneList = []    #We do not want to delete list of tripped zones here

			# If the alarm was tripped, mark partition as acknowledged so
			# subsequent 654 re-broadcasts from alarm memory don't revert us.
			if self.repeatAlarmTripped is True:
				self.alarmAcknowledgedPartitions.add(partition)
				self._setRepeatAlarmTripped(False, f"cmd 751 (special opening, partition {partition})")

			self.updateKeypad(partition, 'state', kAlarmStateDisarmed)
			self.updateKeypad(partition, 'ArmedState', kAlarmArmedStateDisarmed)
			self.updateKeypad(partition, 'LEDArmed', 'off')
			self.updateKeypad(0, 'PanicState', kPanicStateNone)
			self.updateKeypad(partition, 'ReadyState', kReadyStateTrue)
			self.updateKeypad(partition, 'LEDReady', 'on')
			self.updateKeypad(partition, 'LEDBypass', 'off')
			if self.configUseCustomIcons is True:
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)   # green circle
				#dev.updateStateImageOnServer(indigo.kStateImageSel.Unlocked)  # red open padlock

			self.triggerEvent('eventAlarmDisarmed')
			self.speak('speakTextDisarmed')

			for trig in self.triggerList:
				trigger = indigo.triggers[trig]
				if trigger.pluginTypeId == 'eventPartitionDisarmed':
					if trigger.pluginProps['partitionNum'] == str(partition):
						indigo.trigger.execute(trigger.id)

			#Disarming cancels all bypassed zones automatically by DSC. So just need to update plugin zone states.
			self.logger.debug("Bypass Cancelled for all Zones by DSC")
			for i in range(64):
				self.updateZoneBypass(i, kZoneBypassNo, source=source)


		elif cmd == '800':
			self.logger.warning("Alarm Panel Battery is low.")
			self.sendTroubleEmail("Alarm panel battery is low.")

		elif cmd == '801':
			self.logger.info("Alarm Panel Battery is now ok.")
			self.sendTroubleEmail("Alarm panel battery is now ok.")

		elif cmd == '802':
			self.logger.warning("AC Power Lost.")
			self.sendTroubleEmail("AC Power Lost.")
			self.triggerEvent('eventNoticeAC_Trouble')

		elif cmd == '803':
			self.logger.info("AC Power Restored.")
			self.sendTroubleEmail("AC Power Restored.")
			self.triggerEvent('eventNoticeAC_Restore')

		elif cmd == '806':
			self.logger.warning("An open circuit has been detected across the bell terminals.")
			self.sendTroubleEmail("An open circuit has been detected across the bell terminals.")

		elif cmd == '807':
			self.logger.info("The bell circuit has been restored.")
			self.sendTroubleEmail("The bell circuit has been restored.")

		elif cmd == '814':
			self.logger.warning("FTC Trouble.")
			self.sendTroubleEmail("The panel has failed to communicate successfully to the monitoring station.")

		elif cmd == '815':
			self.logger.info("FTC Trouble Restore.")
			self.sendTroubleEmail("The panel has resumed communications.")

		elif cmd == '840':
			partition = int(dat)
			if partition in self.keypadList:
				dev = indigo.devices[self.keypadList[partition]]
				keyp = str(dev.pluginProps['partitionName'])
				self.logger.warning(f"Trouble Status (LED ON). (Partition {partition} '{keyp}')")
				self.updateKeypad(partition, 'LEDTrouble', 'on')   # this updates LED for partitions 1-8
				self.troubleClearedTimer = 0

		elif cmd == '841':
			#Sends Trouble off for all partitions, including undefined ones.
			partition = int(dat)
			self.logger.debug(f"Trouble Status Restore (LED OFF). (Partition {partition})")
			self.updateKeypad(partition, 'LEDTrouble', 'off')  # this updates LED for partitions 1-8
			if self.troubleCode > 0:
				# If the trouble light goes off, set a 10 second timer.
				# If the light is still off after 10 seconds we'll clear our status
				# This is required because the panel turns the light off/on quickly
				# when the light is actually on.
				self.troubleClearedTimer = 10

		elif cmd == '849':
			self.logger.debug(f"Received trouble code byte 0x{dat}")
			newCode = int(dat, 16)

			if newCode != self.troubleCode:
				self.troubleCode = newCode
				if self.troubleCode > 0:
					body = "Trouble Code Received:\n"
					if self.troubleCode & 1: body += "- Service is Required. Check Keypad for more Information.\n"
					if self.troubleCode & 2: body += "- AC Power Lost\n"
					if self.troubleCode & 4: body += "- Telephone Line Fault\n"
					if self.troubleCode & 8: body += "- Failure to Communicate\n"
					if self.troubleCode & 16: body += "- Sensor/Zone Fault\n"
					if self.troubleCode & 32: body += "- Sensor/Zone Tamper\n"
					if self.troubleCode & 64: body += "- Sensor/Zone Low Battery\n"
					if self.troubleCode & 128: body += "- Loss of Time\n"
					self.sendTroubleEmail(body)

		elif cmd == '851':
			self.logger.debug(f"Partition Busy Restore. (Partition {int(dat)})")
		elif cmd == '896':
			self.logger.debug("Keybus Fault")
		elif cmd == '897':
			self.logger.debug("Keybus Fault Restore")
		elif cmd == '900':
			self.logger.error("User Access Code Required")

		elif cmd == '901':
			#this updates the virtual keypad
			#for char in dat:
			#	self.logger.debug(f"LCD DEBUG: {ord(char)}")
			m = re.search(r'^...(..)(.*)$', dat)
			if m:
				lcdText = re.sub(r'[^ a-zA-Z0-9_/\:-]+', ' ', m.group(2))
				half = int(len(lcdText)/2)
				half1 = lcdText[:half]
				half2 = lcdText[half:]
				self.logger.debug(f"LCD Update, Line 1:'{half1}' Line 2:'{half2}'")
				self.updateKeypad(0, 'LCDLine1', half1)
				self.updateKeypad(0, 'LCDLine2', half2)

		elif cmd == '903':
			m = re.search(r'^(.)(.)$', dat)
			if m:
				(ledName, ledState) = (kLedIndexList[int(m.group(1))], kLedStateList[int(m.group(2))])
				self.logger.debug(f"LED '{ledName}' is '{ledState}'.")

				if ledState == 'flashing':
					ledState = 'on'

				if ledName == 'Ready':
					self.updateKeypad(1, 'LEDReady', ledState)
				elif ledName == 'Armed':
					self.updateKeypad(1, 'LEDArmed', ledState)
				elif ledName == 'Trouble':
					self.updateKeypad(1, 'LEDTrouble', ledState)
				elif ledName == 'Bypass':
					self.updateKeypad(1, 'LEDBypass', ledState)
				elif ledName == 'Memory':
					self.updateKeypad(1, 'LEDMemory', ledState)


		elif cmd == '904':
			self.logger.debug("Beep Status")

		elif cmd == '905':
			self.logger.debug("Tone Status")

		elif cmd == '906':
			self.logger.debug("Buzzer Status")

		elif cmd == '907':
			self.logger.debug("Door Chime Status")

		elif cmd == '908':
			m = re.search(r'^(..)(..)(..)$', dat)
			if m:
				self.logger.debug(f"DSC Software Version {m.group(1)}.{m.group(2)}")

		elif cmd == '912':
			self.logger.error("Command Output Pressed")

		elif cmd == '921':
			self.logger.error("Master Code Required")

		elif cmd == '922':
			self.logger.error("Installer Code Required")

		else:
			self.logger.debug(f"Unrecognized command received (Cmd:{cmd} Dat:{dat})")



	######################################################################################
	# Indigo Device State Updating
	######################################################################################

	# Updates temperature of DSC temperature sensor
	#
	def updateSensorTemp(self, sensorNum, key, temp, source=None):
		# Source attribution suffix is gated on the "Log Interface Source"
		# checkbox in plugin config so users who don't care about dual-interface
		# debugging get cleaner log lines.
		srcSuffix = f" (via {source})" if (self.debugInterfaceSource and source) else ""
		if temp > 127:
			temp = 127 - temp
		self.logger.debug(f"Temp sensor {sensorNum} {key} temp now {temp} degrees.")
		if sensorNum in list(self.tempList.keys()):
			if key == 'inside':
				self.tempList[sensorNum].updateStateOnServer(key="temperatureInside", value=temp)
			elif key == 'outside':
				self.tempList[sensorNum].updateStateOnServer(key="temperatureOutside", value=temp)
			elif key == 'cool':
				self.tempList[sensorNum].updateStateOnServer(key="setPointCool", value=temp)
			elif key == 'heat':
				self.tempList[sensorNum].updateStateOnServer(key="setPointHeat", value=temp)

			# Same checkbox-as-bool fix as in updateZoneState; also use
			# _alwaysLog so Log Changes always appears in the event log
			# regardless of Event Logging Level. Use _shouldLogZoneChanges
			# so the plugin-config global override is honoured.
			if self._shouldLogZoneChanges(self.tempList[sensorNum].pluginProps):
				self._alwaysLog(f"Temp sensor {sensorNum} {key} temp now {temp} degrees.{srcSuffix}")


	# Updates zone group
	#
	def updateZoneGroup(self, zoneGroupDevId):

		zoneGrp = indigo.devices[zoneGroupDevId]

		zoneGrp.updateStateOnServer(key="AnyMemberLastChangedTimer", value=0)
		zoneGrp.updateStateOnServer(key="AnyMemberLastChangedShort", value="0m")

		newState = kZoneGroupStateClosed
		for zoneId in self.zoneGroupList[zoneGroupDevId]:
			# Check if device still exists before accessing it
			try:
				zoneDevice = indigo.devices[int(zoneId)]
			except:
				# Device no longer exists, skip it
				self.logger.debug(f"Zone ID {zoneId} in zone group no longer exists, skipping")
				continue
			
			zoneState = zoneDevice.states['state']
			# A member that has set off the alarm (tripped now, or in alarm memory)
			# makes the group tripped; otherwise any open member makes it open.
			if (zoneDevice.states.get('alarmState') or kAlarmStateNormal) != kAlarmStateNormal:
				newState = kZoneGroupStateTripped
			elif zoneState == kZoneStateOpen and newState != kZoneGroupStateTripped:
				newState = kZoneGroupStateOpen

		if zoneGrp.states['state'] != newState:
			zoneGrp.updateStateOnServer(key="EntireGroupLastChangedTimer", value=0)
			zoneGrp.updateStateOnServer(key="EntireGroupLastChangedShort", value="0m")
			zoneGrp.updateStateOnServer(key="state", value=newState)


	# Updates indigo variable instance var with new value varValue
	#
	def updateZoneState(self, zoneKey, newState, source=None):
		"""Update a zone's state.

		`source` (optional) is the interface that delivered the panel message —
		'IT-100', 'Envisalink' or None. When the zone has Log Changes enabled
		AND `Log Interface Source` is ticked in plugin config, the source is
		appended to the log line so you can see which interface picked the
		change up first (e.g. 'Lounge Motion Opened. (via IT-100)'). Untick
		'Log Interface Source' for cleaner log lines without the suffix.
		"""

		# Pretty suffix for log lines. Empty when there's no source OR when
		# the user has unticked Log Interface Source — same gating as the
		# verbose per-message debug logging in _pollInterface.
		srcSuffix = f" (via {source})" if (self.debugInterfaceSource and source) else ""

		if zoneKey in list(self.zoneList.keys()):
			try:
				zone = indigo.devices[self.zoneList[zoneKey]]
			except:
				self.logger.warning("possible Server Communication Error")
				return
			
			zoneType = zone.pluginProps['zoneType']
			#zonePartition = zone.pluginProps['zonePartition']

			# If the new state is different from the old state
			# then lets update timers and set the new state
			if zone.states['state'] != newState:
				
				# Handle latch timer logic
				latchSeconds = int(zone.pluginProps.get('latchSeconds', 0))
				
				# Update sensor states for standard sensor compatibility.
				# onOffState is Indigo's built-in sensor state and drives the
				# state column display. The plugin's custom onState/offState
				# states (declared in Devices.xml) are mirrored alongside
				# so they accurately reflect activity — historically they
				# weren't being written by any code path, leaving them
				# stuck at False.
				if newState == kZoneStateClosed:
					# Zone is closing - check if we should latch
					if latchSeconds > 0:
						# Start latch timer - keep sensor on and change state to openLatched
						# Don't reset LastChanged timers here - only when latch expires
						import time
						self.latchTimers[zone.id] = time.time() + latchSeconds
						displayText = self._getDisplayText(zoneType, isActive=True)
						zone.updateStateOnServer(key='latchTimer',  value=latchSeconds)
						zone.updateStateOnServer(key='state',       value=kZoneStateOpenLatched)
						zone.updateStateOnServer(key='onOffState',  value=True, uiValue=displayText)
						zone.updateStateOnServer(key='onState',     value=True)
						zone.updateStateOnServer(key='offState',    value=False)
						zone.updateStateOnServer(key='displayText', value=displayText)
						self._refreshZoneDisplay(zone)
						self.logger.debug(f"Zone '{zone.name}': Latching ON for {latchSeconds} seconds")
					else:
						# No latching, turn off immediately and reset timers
						displayText = self._getDisplayText(zoneType, isActive=False)
						zone.updateStateOnServer(key='LastChangedShort', value='0m')
						zone.updateStateOnServer(key='LastChangedTimer', value=0)
						zone.updateStateOnServer(key='state',            value=kZoneStateClosed)
						zone.updateStateOnServer(key='latchTimer',       value=0)
						zone.updateStateOnServer(key='onOffState',       value=False, uiValue=displayText)
						zone.updateStateOnServer(key='onState',          value=False)
						zone.updateStateOnServer(key='offState',         value=True)
						zone.updateStateOnServer(key='displayText',  value=displayText)
						zone.updateStateOnServer(key='displayText',  value=displayText)
						self._refreshZoneDisplay(zone)
						self.logger.debug(f"Zone '{zone.name}': Closed, display='{displayText}'")
				else:  # open or tripped
					# Zone is opening - cancel any latch timer, turn on, and reset timers
					if zone.id in self.latchTimers:
						del self.latchTimers[zone.id]
					displayText = self._getDisplayText(zoneType, isActive=True)
					zone.updateStateOnServer(key='LastChangedShort', value='0m')
					zone.updateStateOnServer(key='LastChangedTimer', value=0)
					zone.updateStateOnServer(key='state',            value=newState)
					zone.updateStateOnServer(key='latchTimer',       value=0)
					zone.updateStateOnServer(key='onOffState',       value=True, uiValue=displayText)
					zone.updateStateOnServer(key='onState',          value=True)
					zone.updateStateOnServer(key='offState',         value=False)
					zone.updateStateOnServer(key='displayText',   value=displayText)
					self._refreshZoneDisplay(zone)
					self.logger.debug(f"Zone '{zone.name}': Active ({newState}), display='{displayText}'")
				
				timeNowFormatted = datetime.now().strftime("%H:%M:%S, %Y-%m-%d")

				# Check if this zone is assigned to a zone group so we can update it
				for devId in self.zoneGroupList:
					#self.logger.debug(f"Zone Group ID: {zone.id} contains {zoneGroupList[devId]}")
					if str(zone.id) in self.zoneGroupList[devId]:
						self.updateZoneGroup(devId)

				if 'var' in list(zone.pluginProps.keys()):
					self.updateVariable(zone.pluginProps['var'], newState)

				if newState == kZoneStateTripped:
					self.logger.warning(f"Alarm Zone '{zone.name}' TRIPPED!{srcSuffix}")

				# Indigo can store this checkbox value as bool, int, or string
				# (depending on version and how the device was saved). The
				# original `== 1` test failed for the string '1', so Log Changes
				# never fired. _propIsTrue normalises all the shapes.
				# _shouldLogZoneChanges layers on the global override.
				_logChangesRaw = zone.pluginProps.get('zoneLogChanges', '<missing>')
				_logChangesEnabled = self._shouldLogZoneChanges(zone.pluginProps)
				self.logger.debug(f"Zone '{zone.name}' zoneLogChanges raw={_logChangesRaw!r} parsed={_logChangesEnabled} (globalOverride={getattr(self, 'configLogAllZoneChanges', False)})")
				if _logChangesEnabled:
					zoneTypeLocal = zone.pluginProps['zoneType']
					# Choose a verb that fits the device type so motion sensors
					# read naturally ("Motion Detected") while doors/windows
					# keep the original "Opened/Closed" wording.
					if zoneTypeLocal == 'zoneTypeMotion':
						openVerb, closeVerb = 'Motion Detected', 'Motion Cleared'
					elif zoneTypeLocal == 'zoneTypeWater':
						openVerb, closeVerb = 'Water Detected', 'Water Cleared'
					elif zoneTypeLocal == 'zoneTypeFire':
						openVerb, closeVerb = 'Fire Detected', 'Fire Cleared'
					elif zoneTypeLocal == 'zoneTypeGas':
						openVerb, closeVerb = 'Gas Detected', 'Gas Cleared'
					elif zoneTypeLocal == 'zoneTypeCO':
						openVerb, closeVerb = 'CO Detected', 'CO Cleared'
					elif zoneTypeLocal == 'zoneTypeGlass':
						openVerb, closeVerb = 'Glass Break Detected', 'Glass Break Cleared'
					elif zoneTypeLocal == 'zoneTypeShock':
						openVerb, closeVerb = 'Shock Detected', 'Shock Cleared'
					elif zoneTypeLocal == 'zoneTypeLock':
						openVerb, closeVerb = 'Unlocked', 'Locked'
					else:
						openVerb, closeVerb = 'Opened', 'Closed'

					# Use _alwaysLog (indigo.server.log) here so the per-zone
					# Log Changes output appears even when the global Event
					# Logging Level is set to Warnings or Errors. The Log
					# Changes checkbox is itself a per-zone opt-in, so it would
					# be surprising for the global setting to silently hide it.
					if newState == kZoneStateOpen:
						self._alwaysLog(f"Alarm Zone '{zone.name}' {openVerb}.{srcSuffix}")
						if zoneTypeLocal != "zoneTypeMotion":
							indigo.variable.updateValue("DSC_Last_Zone_Active", value=f"{zone.name} Opened at {timeNowFormatted}.")
					elif newState == kZoneStateClosed:
						self._alwaysLog(f"Alarm Zone '{zone.name}' {closeVerb}.{srcSuffix}")
						if zoneTypeLocal != "zoneTypeMotion":
							indigo.variable.updateValue("DSC_Last_Zone_Active", value=f"{zone.name} Closed at {timeNowFormatted}.")

				if zoneType == "zoneTypeMotion":
					indigo.variable.updateValue("DSC_Last_Motion_Active", value=f"{zone.name} at {timeNowFormatted}.")
				
				# Broadcast device update to subscribing plugins
				indigo.server.broadcastToSubscribers(u"deviceUpdated", zone.id)
			else:
				# State already at the desired value — log at debug level
				# so we can diagnose 'stuck zone' issues. If a zone is stuck
				# at 'open' but you can see this log line saying a cmd 610
				# arrived for it, then the packet IS reaching the plugin
				# but the no-op guard is firing — which means the previous
				# cmd 610 was processed (so where did it come from?). If
				# you DON'T see this log line, the cmd 610 packet isn't
				# arriving at all — kernel buffer overflow or dropped on
				# the wire.
				self.logger.debug(
					f"Zone '{zone.name}' state already {newState!r} — "
					f"update no-op{srcSuffix}"
				)


	def updateZoneBypass(self, zoneKey, newState, source=None):
		"""Update a zone's bypass state. `source` (optional) is the interface
		that delivered the panel message — appended to Log Changes output when
		set AND when 'Log Interface Source' is ticked in plugin config."""

		srcSuffix = f" (via {source})" if (self.debugInterfaceSource and source) else ""

		if zoneKey in list(self.zoneList.keys()):
			try:
				zone = indigo.devices[self.zoneList[zoneKey]]
			except:
				self.logger.warning("possible Server Communication Error")   #catching servercommunicationerror
				pass
			#zoneType = zone.pluginProps['zoneType']

			# If the new bypass state is different from the old state
			# then lets set the new state
			if zone.states['bypass'] != newState:
				# This is a new bypass state, update all states
				zone.updateStateOnServer(key="bypass", value=newState)

				if 'var' in list(zone.pluginProps.keys()):
					self.updateVariable(zone.pluginProps['var'], newState)

				# Respect Log Changes here too. Same _alwaysLog reasoning as in
				# updateZoneState — Log Changes is a per-zone opt-in and the
				# global Event Logging Level shouldn't silently hide it.
				# _shouldLogZoneChanges layers on the global override.
				if self._shouldLogZoneChanges(zone.pluginProps):
					if newState == kZoneBypassYes:
						self._alwaysLog(f"Alarm Zone '{zone.name}' Bypassed.{srcSuffix}")
					elif newState == kZoneBypassNo:
						self._alwaysLog(f"Alarm Zone '{zone.name}' Bypass Cancelled.{srcSuffix}")


	def updateKeypad(self, partition, stateName, newState):

		self.logger.threaddebug(f"Updating Custom State {stateName} for Keypad on Partition {partition} to {newState}.")

		# If we're updating the main keypad state, update the variable too
		if stateName == 'state':
			self.updateVariable(self.pluginPrefs['variableState'], newState)

		if partition == 0:
			for keyk in self.keypadList.keys():
				try:
					keyp = indigo.devices[self.keypadList[keyk]]
				except:
					self.logger.warning("possible Server Communication Error")   #catching servercommunicationerror
					pass
				keyp.updateStateOnServer(key=stateName, value=newState)
			return

		if partition in list(self.keypadList.keys()):
			try:
				keyp = indigo.devices[self.keypadList[partition]]
			except:
				self.logger.warning("possible Server Communication Error")   #catching servercommunicationerror
				pass
			keyp.updateStateOnServer(key=stateName, value=newState)


	######################################################################################
	# Sending email of tripped zones
	######################################################################################

	def sendZoneTrippedEmail(self):

		if not self.configEmailUrgent or not self.trippedZoneList:
			return

		theBody = "The following zone(s) have been tripped:\n\n"

		for zoneNum in self.trippedZoneList:

			if zoneNum in self.closeTheseZonesList:
				stateNow = "closed"
			else:
				stateNow = "open"

			zone = indigo.devices[self.zoneList[zoneNum]]

			theBody += f"{zone.name} (currently {stateNow})\n"

		theBody += "\n--\nDSC Alarm Plugin\n\n"

		self.logger.info(f"Sending zone tripped email to {self.configEmailUrgent}.")

		contentPrefix = self.pluginPrefs.get('emailUrgentContent', '')
		if contentPrefix:
			theBody = contentPrefix + "\n\n" + theBody

		indigo.server.sendEmailTo(self.configEmailUrgent, subject=self.configEmailUrgentSubject, body=theBody)

##########################################################################################
############# Kidney514 ##########  Sending email of who is disarming
##########################################################################################

	def sendEmailDisarm(self, bodyText):

		if not (self.configEmailDisarm):
			return

		self.logger.info(f"Sending Disarmed by: email to {self.configEmailDisarm}.")

		contentPrefix = self.pluginPrefs.get('EmailDisarmContent', '')
		if contentPrefix:
			bodyText = contentPrefix + "\n\n" + bodyText

		indigo.server.sendEmailTo(self.configEmailDisarm, subject=self.configEmailDisarmSubject, body=bodyText)

##########################################################################################
# sending emails
##########################################################################################


	def sendTroubleEmail(self, bodyText):

		if not self.configEmailNotice:
			return

		self.logger.info(f"Sending trouble email to {self.configEmailNotice}.")

		contentPrefix = self.pluginPrefs.get('emailNoticeContent', '')
		if contentPrefix:
			bodyText = contentPrefix + "\n\n" + bodyText

		indigo.server.sendEmailTo(self.configEmailNotice, subject=self.configEmailNoticeSubject, body=bodyText)


	def sendPanicEmail(self, bodyText):

		if not self.configEmailUrgent:
			return

		self.logger.info(f"Sending panic alert email to {self.configEmailUrgent}.")

		contentPrefix = self.pluginPrefs.get('emailUrgentContent', '')
		if contentPrefix:
			bodyText = contentPrefix + "\n\n" + bodyText

		indigo.server.sendEmailTo(self.configEmailUrgent, subject=self.configEmailUrgentSubject, body=bodyText)
		# if no email address is specified the speak part does not trigger
		say = f"{self.pluginPrefs['speakTextPanic']} Keypad: {bodyText}."
		self.sayThis(say)



	def sendDuressEmail(self, bodyText):

		if not self.configEmailUrgent:
			return

		self.logger.info(f"Sending duress alarm email to {self.configEmailUrgent}.")

		contentPrefix = self.pluginPrefs.get('emailUrgentContent', '')
		if contentPrefix:
			bodyText = contentPrefix + "\n\n" + bodyText

		indigo.server.sendEmailTo(self.configEmailUrgent, subject=self.configEmailUrgentSubject, body=bodyText)
		# if no email address specified the speak part does not trigger
		say = f"{self.pluginPrefs['speakTextPanic']} Keypad: {bodyText}."
		self.sayThis(say)


	def sayThis(self, text):
		self.logger.debug(f"SAY: {text}")
		# The default variable is DSC_Alarm_Text
		if self.configSpeakVariable is not None:
			if self.configSpeakVariable in indigo.variables:
				indigo.variable.updateValue(self.configSpeakVariable, value=text)
		else:
			indigo.server.speak(text)


	def speak(self, textId):
		self.logger.debug(f"ID: {textId}")
		if self.pluginPrefs['speakingEnabled'] is False:
			return

		if not (self.pluginPrefs[textId]):
			return

		if textId == 'speakTextFailedToArm':
			zones = 0
			zoneText = ''
			for zoneNum in self.zoneList.keys():
				zone = indigo.devices[self.zoneList[zoneNum]]
				if zone.states['state.open'] is True:
					if zones > 0:
						zoneText += ', '
					zoneText += zone.name.replace("Alarm_", "")
					zones += 1

			if zones == 0:
				say = self.pluginPrefs[textId]
			if zones == 1:
				say = f"{self.pluginPrefs[textId]}  The {zoneText} is open."
			else:
				say = f"{self.pluginPrefs[textId]}  The following zones are open: {zoneText}."

			self.sayThis(say)

		elif textId == 'speakTextTripped':
			zones = 0
			zoneText = ''
			for zoneNum in self.trippedZoneList:
				zone = indigo.devices[self.zoneList[zoneNum]]
				if zones > 0:
					zoneText += ', '
				zoneText += zone.name.replace("Alarm_", "")
				zones += 1

			if zones == 0:
				return
				#if alarm is tripped due to panic, we want the panic alert to handle the talking
			if zones == 1:
				say = f"{self.pluginPrefs[textId]}  The {zoneText} has been tripped."
			else:
				say = f"{self.pluginPrefs[textId]}  The following zones have been tripped: {zoneText}."

			self.sayThis(say)


		else:
			self.sayThis(self.pluginPrefs[textId])


	def _setZoneSubType(self, dev, zoneType):
		"""Set the sensor subType so Indigo uses the correct native icon set for this zone type."""
		subTypeMap = {
			'zoneTypeMotion' : indigo.kSensorDeviceSubType.Motion,
			'zoneTypeDoor'   : indigo.kSensorDeviceSubType.DoorWindow,
			'zoneTypeWindow' : indigo.kSensorDeviceSubType.DoorWindow,
			'zoneTypeFire'   : indigo.kSensorDeviceSubType.Smoke,
			'zoneTypeWater'  : indigo.kSensorDeviceSubType.WaterLeak,
			'zoneTypeGas'    : indigo.kSensorDeviceSubType.GasLeak,
			'zoneTypeGlass'  : indigo.kSensorDeviceSubType.GlassBreak,
			'zoneTypeShock'  : indigo.kSensorDeviceSubType.Vibration,
			'zoneTypeCO'     : indigo.kSensorDeviceSubType.CO,
			'zoneTypeLock'   : indigo.kSensorDeviceSubType.DoorWindow,
		}
		subType = subTypeMap.get(zoneType, indigo.kSensorDeviceSubType.Zone)
		if dev.subType != subType:
			dev.subType = subType
			dev.replaceOnServer()

	def _refreshZoneDisplay(self, dev):
		"""Single place that turns a zone's live state, alarm state and availability
		into what is displayed: onOffState's text (the State column), displayText,
		displayState and the icon. onOffState's VALUE always stays the live state, so
		HomeKit, triggers and scripts only ever see the real open/closed reading."""
		try:
			dev = indigo.devices[dev.id]
		except Exception:
			return
		zoneType = dev.pluginProps.get('zoneType', 'zoneTypeOther')
		active   = dev.states.get('state') not in (kZoneStateClosed, '')
		alarm    = dev.states.get('alarmState') or kAlarmStateNormal
		text     = self._getDisplayText(zoneType, isActive=active)
		if alarm == kAlarmStateTripped:
			text, disp = f"{text} – ALARM", 'tripped'
		elif alarm == kAlarmStateMemory:
			text, disp = f"{text} – alarm memory", 'memory'
		else:
			disp = 'open' if active else 'closed'
		if self.devicesUnavailable:
			disp = 'unavailable'
		keep = self.devicesUnavailable   # don't let a refresh clear the "unavailable" error
		dev.updateStateOnServer(key='onOffState',   value=active, uiValue=text, clearErrorState=not keep)
		dev.updateStateOnServer(key='displayText',  value=text, clearErrorState=not keep)
		dev.updateStateOnServer(key='displayState', value=disp, clearErrorState=not keep)
		self._setZoneIcon(dev, zoneType, active, tripped=(alarm != kAlarmStateNormal))

	def _setZoneAlarmState(self, zoneNum, newAlarmState, source=None):
		if zoneNum not in self.zoneList:
			return
		try:
			dev = indigo.devices[self.zoneList[zoneNum]]
		except Exception:
			return
		if dev.states.get('alarmState') == newAlarmState:
			return
		keep = self.devicesUnavailable
		dev.updateStateOnServer(key='alarmState', value=newAlarmState, clearErrorState=not keep)
		if newAlarmState == kAlarmStateTripped:
			dev.updateStateOnServer(key='alarmMemorySince', value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
			                        clearErrorState=not keep)
			srcSuffix = f" (via {source})" if (self.debugInterfaceSource and source) else ""
			self.logger.warning(f"Alarm Zone '{dev.name}' TRIPPED!{srcSuffix}")
		elif newAlarmState == kAlarmStateNormal:
			dev.updateStateOnServer(key='alarmMemorySince', value="", clearErrorState=not keep)
		self._refreshZoneDisplay(dev)
		self._updateKeypadAlarmMemory()
		for groupId in list(self.zoneGroupList):
			if str(dev.id) in self.zoneGroupList[groupId]:
				self.updateZoneGroup(groupId)
		indigo.server.broadcastToSubscribers(u"deviceUpdated", dev.id)

	def _clearAlarmMemory(self, partition=None):
		"""Clear alarm memory for zones in `partition` (all zones if None)."""
		for zoneNum, devId in list(self.zoneList.items()):
			try:
				dev = indigo.devices[devId]
			except Exception:
				continue
			if partition is not None and str(dev.pluginProps.get('zonePartition', '1')) != str(partition):
				continue
			if (dev.states.get('alarmState') or kAlarmStateNormal) != kAlarmStateNormal:
				self._setZoneAlarmState(zoneNum, kAlarmStateNormal)
		self._updateKeypadAlarmMemory()

	def _updateKeypadAlarmMemory(self):
		"""Each keypad's alarmMemoryZones state lists its zones currently tripped or in memory."""
		byPartition = {}
		for zoneNum, devId in sorted(self.zoneList.items()):
			try:
				dev = indigo.devices[devId]
			except Exception:
				continue
			if (dev.states.get('alarmState') or kAlarmStateNormal) != kAlarmStateNormal:
				byPartition.setdefault(str(dev.pluginProps.get('zonePartition', '1')), []).append(dev.name)
		for partition, devId in self.keypadList.items():
			try:
				keyp = indigo.devices[devId]
				if 'alarmMemoryZones' not in keyp.states:
					continue
				names = byPartition.get(str(partition), [])
				keyp.updateStateOnServer(key='alarmMemoryZones', value=", ".join(names),
				                         clearErrorState=not self.devicesUnavailable)
			except Exception as e:
				self.logger.debug(f"Could not update alarm memory on keypad {partition}: {e}")

	def methodClearAlarmMemory(self, action):
		self._clearAlarmMemory(None)
		self.trippedZoneList = []
		if "DSC_Alarm_Memory" in indigo.variables:
			indigo.variable.updateValue("DSC_Alarm_Memory", value="")
		self.logger.info("Alarm memory cleared for all zones.")

	def buttonSetAllZonesLogChanges(self, valuesDict, typeId=None):
		return self._setAllZonesLogChanges(valuesDict, True)

	def buttonClearAllZonesLogChanges(self, valuesDict, typeId=None):
		return self._setAllZonesLogChanges(valuesDict, False)

	def _setAllZonesLogChanges(self, valuesDict, enable):
		changed = 0
		for dev in indigo.devices.iter("self.alarmZone"):
			props = dev.pluginProps
			if self._propIsTrue(props, 'zoneLogChanges', False) != enable:
				props['zoneLogChanges'] = enable
				dev.replacePluginPropsOnServer(props)
				changed += 1
		self._alwaysLog(f"'Log Changes' turned {'ON' if enable else 'OFF'} on {changed} zone device(s).")
		return valuesDict

	# Zone icons: set explicitly from zone type + active/inactive on every state change,
	# so they are consistent across all zones and can never be left stale (as the old
	# "red while in alarm" icon was). Indigo has no built-in fire icon, so fire zones
	# use the heating flame.
	ZONE_ICONS = {
		#                  clear                  active                 tripped (zone in alarm)
		'zoneTypeFire':   ('HvacHeatMode',        'HvacHeating',         'HvacHeating'),
		'zoneTypeMotion': ('MotionSensor',        'MotionSensorTripped', 'SensorTripped'),
		'zoneTypeDoor':   ('DoorSensorClosed',    'DoorSensorOpened',    'SensorTripped'),
		'zoneTypeWindow': ('WindowSensorClosed',  'WindowSensorOpened',  'SensorTripped'),
		'zoneTypeLock':   ('Locked',              'Unlocked',            'SensorTripped'),
		'zoneTypeWater':  ('SensorOff',           'SensorTripped',       'SensorTripped'),
		'zoneTypeGas':    ('SensorOff',           'SensorTripped',       'SensorTripped'),
		'zoneTypeCO':     ('SensorOff',           'SensorTripped',       'SensorTripped'),
		'zoneTypeGlass':  ('SensorOff',           'SensorTripped',       'SensorTripped'),
		'zoneTypeShock':  ('SensorOff',           'SensorTripped',       'SensorTripped'),
		'zoneTypeOther':  ('SensorOff',           'SensorOn',            'SensorTripped'),
	}
	# Note: Indigo currently draws the door/window selectors as plain grey/green dots.
	# They're used anyway so proper artwork appears automatically if Indigo adds it.

	def _setZoneIcon(self, dev, zoneType, isActive, tripped=False):
		clear, active, alarm = self.ZONE_ICONS.get(zoneType, ('SensorOff', 'SensorOn', 'SensorTripped'))
		name = alarm if tripped else (active if isActive else clear)
		sel = getattr(indigo.kStateImageSel, name, indigo.kStateImageSel.Auto)
		try:
			if dev.displayStateImageSel != sel:
				dev.updateStateImageOnServer(sel)
		except Exception as e:
			self.logger.debug(f"Could not set icon for '{dev.name}': {e}")

	def _getDisplayText(self, zoneType, isActive):
		"""Get display text for zone based on type and active state"""
		stateTextMap = {
			'zoneTypeDoor':     ('open',       'closed'),
			'zoneTypeWindow':   ('open',       'closed'),
			'zoneTypeMotion':   ('detected',   'clear'),
			'zoneTypeFire':     ('detected',   'clear'),
			'zoneTypeWater':    ('detected',   'clear'),
			'zoneTypeGas':      ('detected',   'clear'),
			'zoneTypeGlass':    ('detected',   'clear'),
			'zoneTypeShock':    ('detected',   'clear'),
			'zoneTypeCO':       ('detected',   'clear'),
			'zoneTypeLock':     ('unlocked',   'locked'),
			'zoneTypeOther':    ('open',       'closed')
		}
		activeText, inactiveText = stateTextMap.get(zoneType, ('open', 'closed'))
		return activeText if isActive else inactiveText


	# Updates indigo variable instance var with new value varValue
	#
	def updateVariable(self, varID, varValue):

		if self.createVariables is False:
			return

		#self.logger.debug(f"Variable: {varID}")

		if varID is None:
			return

		if varID in indigo.variables:
			indigo.variable.updateValue(varID, value=varValue)


	# Converts given time in minutes to a human format e.g. 3m, 5h, 2d, etc.
	#
	def getShortTime(self, minutes):

		# If time is less than 100 min then show XXm
		if minutes < 100:
			return str(minutes) + 'm'
		# If it's less than 49 hours then show XXh
		elif minutes < 2881:
			return str(int(minutes / 60)) + 'h'
		# If it's less than 100 days then show XXd
		elif minutes < 144000:
			return str(int(minutes / 1440)) + 'd'
		# If it's anything more than one hundred days then show nothing
		else:
			return ''

# 		############ Alternate Version #########################
# 		# If time is less than 100 min then show XXm
# 		if minutes < 100:
# 			return str(minutes) + 'm'
# 		# If it's less than 49 hours then show XXh
# 		elif minutes < 2881:
# 			return str(int(minutes / 60)) + 'h'
# 		# If it's less than 365 days then show XXd
# 		elif minutes < 525601:
# 			return str(int(minutes / 1440)) + 'd'
# 		# If it's anything more than 365 days then show XXmonths
# 		else:
# 			return str(int(minutes / 43800)) + 'months'
# 		########################################################



	######################################################################################
	# Concurrent Thread
	######################################################################################

	######################################################################################
	# Dual-Interface Connection Management
	######################################################################################

	def connectIT100(self):
		"""Open IT-100 serial connection"""
		if self.it100Port is not None and self.it100Connected:
			return True  # Already connected
		
		if self.it100Port is not None:
			try:
				self.it100Port.close()
			except:
				pass
			self.it100Port = None
		
		try:
			self.logger.info(f"Connecting to IT-100 on {self.pluginPrefs['serialPort']}")
			self.it100Port = serial.Serial(self.pluginPrefs['serialPort'], 9600, writeTimeout=1)
			self.it100Port.flushInput()
			# Short read timeout so the main loop ticks fast when there's no
			# inbound data. The previous 1-second timeout was the de-facto
			# pacing mechanism for the whole plugin but it made LCD updates
			# feel sluggish during interactive operations like keypad
			# programming. Pacing is now handled explicitly via
			# outboundPacketIntervalSecs and an explicit sleep at the bottom
			# of the main loop.
			self.it100Port.timeout = 0.1
			self.it100Connected = True
			self._lastIT100Error = None  # Clear error flag
			self.logger.info("IT-100 connection established")
			
			# Set self.port for backward compatibility with legacy readPacket/sendPacket code
			self.port = self.it100Port

			# Pull fresh state from the panel so we learn current zone /
			# partition / LED states (including LEDMemory, which tells us
			# whether the panel has an alarm in memory). Critical for
			# detecting trips that happened while the plugin was offline.
			# The response 6xx packets are processed normally by the main
			# loop — they'll correct any state drift and trigger the
			# historical-alarm detection path in cmd 654 if appropriate.
			self._postConnectStatusSent_IT100 = False  # Mark for re-pull after the connection is fully ready
			
			return True
		except Exception as err:
			# Only log detailed error if this is a new failure (not repeated)
			if not hasattr(self, '_lastIT100Error') or self._lastIT100Error != str(err):
				self.logger.error(f"Error connecting to IT-100: {str(err)}")
				self._lastIT100Error = str(err)
			else:
				self.logger.debug(f"IT-100 still unavailable: {str(err)}")
			self.it100Connected = False
			self.it100Port = None
			return False

	def connectEnvisalink(self):
		"""Open Envisalink network connection and perform login"""
		if self.envisalinkSocket is not None and self.envisalinkConnected:
			return True  # Already connected
		
		if self.envisalinkSocket is not None:
			try:
				self.envisalinkSocket.close()
			except:
				pass
			self.envisalinkSocket = None
		
		try:
			adr = f"{self.pluginPrefs['TwoDS_Address']}:{int(float(self.pluginPrefs['TwoDS_Port']))}"
			self.logger.debug(f"Attempting Envisalink connection to {adr}")
			self.envisalinkSocket = serial.serial_for_url('socket://' + adr, baudrate=115200)
			self.envisalinkSocket.flushInput()
			# Short read timeout — see the comment on it100Port.timeout for
			# the rationale. We use 1s during login since the initial 505
			# request can take a moment to arrive, and switch to 0.1s after
			# the handshake completes.
			self.envisalinkSocket.timeout = 1
			
			# Envisalink login sequence
			# Read initial 505 login request
			time.sleep(0.5)
			self.envisalinkSocket.flushInput()
			
			# Send login with password
			password = self.pluginPrefs['TwoDS_Password'].strip()
			self.logger.debug(f"Password length: {len(password)}")
			loginCmd = '005' + password
			loginChecksum = self.calcChecksum(loginCmd)
			loginPacket = f"{loginCmd}{loginChecksum:02X}\r\n"
			self.logger.debug(f"Sending login: {loginPacket.strip()}")
			self.envisalinkSocket.write(loginPacket.encode('utf-8'))
			
			# Wait for login response - need to read multiple lines
			# First comes 500 (ACK), then comes 505 (login response)
			time.sleep(0.5)
			loginSuccess = False
			for attempt in range(3):
				response = self.envisalinkSocket.readline().decode('utf-8').strip()
				self.logger.debug(f"Login response {attempt+1}: {response}")
				if '5051' in response:  # 505 command, 1 = success
					loginSuccess = True
					break
			
			if loginSuccess:
				self.envisalinkConnected = True
				self._lastEnvisalinkError = None  # Clear error flag
				self.logger.info("Envisalink connection and login successful")

				# Drop to short read timeout now that login is done — same
				# rationale as the IT-100 path.
				self.envisalinkSocket.timeout = 0.1

				# Enable time broadcast
				timeBroadcastCmd = '0561'
				timeBroadcastChecksum = self.calcChecksum(timeBroadcastCmd)
				timeBroadcastPacket = f"{timeBroadcastCmd}{timeBroadcastChecksum:02X}\r\n"
				self.envisalinkSocket.write(timeBroadcastPacket.encode('utf-8'))
				
				# Also set self.port for backward compatibility with legacy code
				self.port = self.envisalinkSocket

				# Pull fresh state from the panel — see comment in
				# connectIT100() for rationale. Same flag pattern.
				self._postConnectStatusSent_Envisalink = False
				
				return True
			else:
				self.logger.error(f"Envisalink login failed - no success response received")
				self.envisalinkConnected = False
				self.envisalinkSocket.close()
				self.envisalinkSocket = None
				return False
				
		except Exception as err:
			# Only log detailed error if this is a new failure (not repeated)
			if not hasattr(self, '_lastEnvisalinkError') or self._lastEnvisalinkError != str(err):
				self.logger.error(f"Error connecting to Envisalink: {str(err)}")
				self._lastEnvisalinkError = str(err)
			else:
				self.logger.debug(f"Envisalink still unavailable: {str(err)}")
			self.envisalinkConnected = False
			if self.envisalinkSocket:
				try:
					self.envisalinkSocket.close()
				except:
					pass
				self.envisalinkSocket = None
			return False

	def maintainIT100Connection(self):
		"""Drain all available inbound data from the IT-100 and dispatch
		messages. Marks as disconnected on error.

		The drain loop is important: when the panel sends a burst of LCD
		updates or zone state changes (e.g. during keypad menu navigation
		or following an alarm event), the kernel TTY buffer can hold
		multiple packets pending read. If we only consume one per main-loop
		iteration, the device states lag behind the panel's actual state by
		several ticks. Looping until readline returns empty consumes the
		backlog in one go.

		Capped at maxDrain iterations as a defensive safety net against the
		unlikely scenario that data keeps arriving faster than we can
		process it.

		IMPORTANT: a v12 bug meant the original drain loop bailed after the
		first packet because _pollInterface returned '' for both 'no data'
		AND 'dispatched a packet'. That was fixed in v19 — _pollInterface
		now returns distinct values so we can tell the difference.
		"""
		if not self.it100Connected or self.it100Port is None:
			return
		maxDrain = 20
		for _ in range(maxDrain):
			result = self._pollInterface(self.it100Port, 'IT-100')
			if result == '-':
				self.logger.error("IT-100 connection lost")
				self.it100Connected = False
				try:
					self.it100Port.close()
				except Exception:
					pass
				self.it100Port = None
				return
			if result == '':
				# Empty read — nothing more in the buffer. Stop draining.
				return
			# result was 'dispatched' or 'skipped' — keep draining in case
			# more packets are queued behind it.

	def maintainEnvisalinkConnection(self):
		"""Drain all available inbound data from the Envisalink and dispatch
		messages. Marks as disconnected on error. Same drain-loop pattern as
		maintainIT100Connection — see that method's docstring for details on
		the v19 fix for the drain loop bailing after one packet."""
		if not self.envisalinkConnected or self.envisalinkSocket is None:
			return
		maxDrain = 20
		for _ in range(maxDrain):
			result = self._pollInterface(self.envisalinkSocket, 'Envisalink')
			if result == '-':
				self.logger.error("Envisalink connection lost")
				self.envisalinkConnected = False
				try:
					self.envisalinkSocket.close()
				except Exception:
					pass
				self.envisalinkSocket = None
				return
			if result == '':
				# Empty read — nothing more in the buffer. Stop draining.
				return
			# 'dispatched' or 'skipped' — keep draining for queued packets.

	def processIT100CommandQueue(self):
		"""Drain outbound packets from the IT-100 send queue, respecting the
		per-press throttle. Sends up to maxPacketsPerCall packets in one go,
		sleeping between presses to enforce outboundPacketIntervalSecs.

		Why drain multiple per call: the main loop ticks at ~10Hz under
		ideal conditions, but during heavy inbound traffic (lots of LCD
		updates from the panel during installer mode) each tick can take
		a full second. If we only send one packet per tick, our actual TX
		rate can drop to 1 packet/sec — far slower than the throttle's
		nominal 5 packets/sec. The panel's installer-mode prompts have
		~5-second inter-keystroke timeouts, so any tick that takes too
		long causes the panel to give up and drop us back to idle.

		By draining multiple packets per call (with the throttle enforced
		via time.sleep within the call), we get predictable TX rate
		independent of main-loop timing. The main loop blocks for up to
		~2 seconds during heavy TX, but the alternative is intermittent
		failure of installer-mode operations.

		Break packets ('070^') don't count against the throttle and don't
		update the throttle timestamp — they follow their preceding press
		immediately.
		"""
		if not self.it100Connected or not self.it100Port:
			return

		# Cap on packets drained per call. Bounds the worst-case main-loop
		# block time: 4 × 0.2s throttle = ~0.8s maximum block, which is
		# roughly one slow-main-loop tick. Going higher would mean fewer
		# total ticks during a long TX sequence (label write = ~50 packets),
		# which means inbound LCD updates get dropped because the panel
		# can buffer them in its keybus only briefly.
		maxPacketsPerCall = 4

		for _ in range(maxPacketsPerCall):
			if not self.it100TxCmdList:
				return  # Queue drained

			(cmdType, data) = self.it100TxCmdList[0]
			isBreakKey = (data == '070^')
			now = time.time()

			if not isBreakKey:
				# Throttle: presses are spaced out. If we haven't waited
				# long enough since the last press, sleep until we have.
				elapsed = now - self._it100LastTxTime
				if elapsed < self.outboundPacketIntervalSecs:
					remaining = self.outboundPacketIntervalSecs - elapsed
					time.sleep(remaining)
					now = time.time()

			try:
				checksum = self.calcChecksum(data)
				packet = f"{data}{checksum:02X}\r\n"

				self.it100Port.write(packet.encode('utf-8'))
				self.logger.threaddebug(f"IT-100 TX: {data}")
				if not isBreakKey:
					self._it100LastTxTime = now

				del self.it100TxCmdList[0]
			except Exception as err:
				self.logger.error(f"IT-100 send error: {str(err)}")
				return

	def processEnvisalinkCommandQueue(self):
		"""Drain outbound packets from the Envisalink send queue, respecting
		the throttle. Same drain-multiple-per-call pattern as the IT-100
		queue — see processIT100CommandQueue docstring for the rationale.

		Note: Envisalink doesn't use the press/break packet pattern (cmd 070
		is IT-100 only), so every packet here is a normal throttled packet.
		"""
		if not self.envisalinkConnected or not self.envisalinkSocket:
			return

		maxPacketsPerCall = 4

		for _ in range(maxPacketsPerCall):
			if not self.envisalinkTxCmdList:
				return

			(cmdType, data) = self.envisalinkTxCmdList[0]
			now = time.time()

			# Throttle (Envisalink doesn't have a press/break concept so
			# all packets are throttled equally).
			elapsed = now - self._envisalinkLastTxTime
			if elapsed < self.outboundPacketIntervalSecs:
				remaining = self.outboundPacketIntervalSecs - elapsed
				time.sleep(remaining)
				now = time.time()

			try:
				checksum = self.calcChecksum(data)
				packet = f"{data}{checksum:02X}\r\n"

				self.envisalinkSocket.write(packet.encode('utf-8'))
				self.logger.threaddebug(f"Envisalink TX: {data}")
				self._envisalinkLastTxTime = now

				del self.envisalinkTxCmdList[0]
			except Exception as err:
				self.logger.error(f"Envisalink send error: {str(err)}")
				return

	def closeAllPorts(self):
		"""Close both interface connections"""
		if self.it100Port:
			try:
				self.it100Port.close()
				self.logger.info("IT-100 connection closed")
			except:
				pass
			self.it100Port = None
			self.it100Connected = False
		
		if self.envisalinkSocket:
			try:
				self.envisalinkSocket.close()
				self.logger.info("Envisalink connection closed")
			except:
				pass
			self.envisalinkSocket = None
			self.envisalinkConnected = False
		
		# Also close legacy port
		if self.port:
			try:
				self.port.close()
			except:
				pass
			self.port = None

	######################################################################################
	# Concurrent Thread
	######################################################################################

	def runConcurrentThread(self):
		self.logger.threaddebug("runConcurrentThread called")
		self.minuteTracker = time.time() + 60
		self.nextUpdateCheckTime = 0

		# While Indigo hasn't told us to shutdown
		while self.shutdown is False:

			self.timeNow = time.time()

			if self.state == self.States.STARTUP:
				self.logger.debug("STATE: Startup")

				if self.configRead is False:
					if self.getConfiguration(self.pluginPrefs) is True:
						self.configRead = True

				if self.configRead is True:
					self.state = self.States.BOTH_INIT

				self.sleep(1)

			elif self.state == self.States.HOLD:
				if self.configRead is False:
					self.state = self.States.STARTUP
				self.sleep(1)

			elif self.state == self.States.HOLD_RETRY:
				self.logger.warning(f"Plugin will attempt to re-initialize again in {self.currentHoldRetryTime} minutes.")
				self.nextRetryTime = self.timeNow + (kHoldRetryTimeMinutes*60)
				self.state = self.States.HOLD_RETRY_LOOP

			elif self.state == self.States.HOLD_RETRY_LOOP:
				if self.configRead is False:
					self.state = self.States.STARTUP
				if self.timeNow >= self.nextRetryTime:
					self.state = self.States.BOTH_INIT
				self.sleep(1)

			elif self.state == self.States.BOTH_INIT:
				# Check if we need to wait for backoff period
				if self.connectionBackoffTime > 0:
					self.logger.debug(f"Waiting {self.connectionBackoffTime} seconds before reconnecting...")
					time.sleep(self.connectionBackoffTime)
					self.connectionBackoffTime = 0
				
				# For dual-interface mode, close and reconnect interfaces
				if self.enableIT100 or self.enableEnvisalink:
					self.logger.info("Dual-interface mode: re-initializing connections")
					# Start recording zone-open reports now: the Envisalink login sends the
					# status request (001) before the connection is reported successful.
					self._beginResyncCollection()
					
					# Close existing connections
					if self.it100Port is not None:
						try:
							self.it100Port.close()
						except:
							pass
						self.it100Port = None
						self.it100Connected = False
					
					if self.envisalinkSocket is not None:
						try:
							self.envisalinkSocket.close()
						except:
							pass
						self.envisalinkSocket = None
						self.envisalinkConnected = False
					
					# Reconnect enabled interfaces (suppress repeated errors)
					if self.enableIT100:
						self.logger.info("Reconnecting IT-100...")
						try:
							self.connectIT100()
						except Exception as e:
							self.logger.error(f"IT-100 reconnection failed: {e}")
					
					if self.enableEnvisalink:
						self.logger.info("Reconnecting Envisalink...")
						try:
							self.connectEnvisalink()
						except Exception as e:
							self.logger.error(f"Envisalink reconnection failed: {e}")
					
					# If at least one connection succeeded, move to polling
					if self.it100Connected or self.envisalinkConnected:
						self.logger.info(f"Reconnection successful (IT-100: {self.it100Connected}, Envisalink: {self.envisalinkConnected})")
						self._startResync()
						if not self.it100Connected:
							self.logger.warning("IT-100 unavailable - continuing with Envisalink only")
						if not self.envisalinkConnected:
							self.logger.warning("Envisalink unavailable - continuing with IT-100 only")
						
						# For IT-100: send initial ping and state request via legacy sendPacket
						# (Envisalink does this in connectEnvisalink via the login sequence)
						if self.it100Connected and not self.envisalinkConnected:
							self.logger.debug("IT-100 only mode: pinging and requesting full state update")
							rx = self.sendPacket('000')  # ping
							if rx:
								rx = self.sendPacket('001')  # full state update request
								if not rx:
									self.logger.warning("IT-100 state update request failed - will continue anyway")
							else:
								self.logger.warning("IT-100 ping failed - will continue anyway")
						
						self.nextPingTime = self.timeNow + kPingInterval
						self.state = self.States.BOTH_POLL
						# Reset failure counter on successful reconnect
						self.connectionFailureCount = 0
					else:
						# Both failed, retry with backoff
						self.logger.error("Both interface connections failed, will retry...")
						self.state = self.States.HOLD_RETRY
				else:
					# Legacy single-interface initialization
					if self.openPort() is True:
						if self.useSerial is False:
							self.state = self.States.SO_CONNECT
							self.sleep(1)

							# Enable pinging every 5 minutes
							self.nextPingTime = self.timeNow + kPingInterval
						else:
							self.state = self.States.ENABLE_TIME_BROADCAST

					else:
						#self.logger.error(f"Error opening port, will retry in {self.currentHoldRetryTime} minutes.")
						self.state = self.States.HOLD_RETRY

			elif self.state == self.States.SO_CONNECT:
				err = True

				# Read packet to clear the port of the 5053 login request
				self.readPacket()

				attemptLogin = True
				while attemptLogin is True:
					attemptLogin = False
					rx = self.sendPacket('005' + self.pluginPrefs['TwoDS_Password'], waitFor='505')
					if not rx or rx == "-":
						self.logger.error("Timeout waiting for Envisalink to respond to login request.")
					else:
						rx = int(rx)
						if rx == 0:
							self.logger.error("Envisalink refused login request.")
						elif rx == 1:
							err = False
							self.logger.info("Connected to Envisalink.")
						elif rx == 3:
							# 2DS sent login request, retry (Happens when socket is first opened)
							self.logger.debug("Received login request, retrying login...")
							attemptLogin = True
						else:
							self.logger.error("Unknown response from Envisalink login request.")

				# This delay is required otherwise 2DS locks up
				self.sleep(1)

				if err is True:
					self.state = self.States.HOLD_RETRY
				else:
					self.state = self.States.ENABLE_TIME_BROADCAST


			elif self.state == self.States.ENABLE_TIME_BROADCAST:

				# Enable time broadcast
				self.logger.debug("Enabling Time Broadcast")
				rx = self.sendPacket('0561')
				if rx:
					self.logger.debug("Time Broadcast enabled.")
					self.state = self.States.BOTH_PING
				else:
					self.logger.error("Error enabling Time Broadcast.")
					self.state = self.States.HOLD_RETRY

			elif self.state == self.States.BOTH_PING:

				#Ping the panel to confirm we are in communication
				err = True
				self.logger.debug("Pinging the panel to test communication...")
				rx = self.sendPacket('000')
				if rx:
					self.logger.debug("Ping was successful.")
					err = False
				else:
					self.logger.error("Error pinging panel, aborting.")

				if err is True:
					self.state = self.States.HOLD_RETRY
				else:
					#Request a full state update
					self.logger.debug("Requesting a full state update.")
					self._beginResyncCollection()
					rx = self.sendPacket('001')
					if not rx:
						self.logger.error("Error getting state update.")
						self.state = self.States.HOLD_RETRY
					else:
						self.logger.debug("State update request successful, initialization complete, starting normal operation.")
						self._startResync()
						self.state = self.States.BOTH_POLL

			elif self.state == self.States.BOTH_POLL:
				if self.configRead is False:
					self.state = self.States.STARTUP
				else:
					# ========== DUAL-INTERFACE SUPPORT ==========
					# Manage IT-100 connection
					if self.enableIT100:
						if not self.it100Connected:
							self.connectIT100()
						else:
							self.maintainIT100Connection()
					
					# Manage Envisalink connection
					if self.enableEnvisalink:
						if not self.envisalinkConnected:
							self.connectEnvisalink()
						else:
							self.maintainEnvisalinkConnection()
						
						# Ping Envisalink if needed
						if self.envisalinkConnected and (self.timeNow > self.nextPingTime):
							self.sendCommand('000', force_interface='envisalink')
							self.nextPingTime = self.timeNow + kPingInterval
					
					# Ping IT-100 if it is the sole active interface
					if self.enableIT100 and self.it100Connected and not self.envisalinkConnected:
						if self.timeNow > self.nextPingTime:
							self.sendCommand('000', force_interface='it100')
							self.nextPingTime = self.timeNow + kPingInterval
					
					# Process command queues
					if self.enableIT100:
						self.processIT100CommandQueue()
					if self.enableEnvisalink:
						self.processEnvisalinkCommandQueue()
					# ============================================

					# Both interfaces are polled above via maintainIT100Connection / maintainEnvisalinkConnection.
					# If both drop, trigger re-init.
					if self.enableIT100 and not self.it100Connected and self.enableEnvisalink and not self.envisalinkConnected:
						self.logger.error("Both interfaces lost - re-initializing.")
						self.state = self.States.BOTH_INIT
					elif self.enableIT100 and not self.it100Connected and not self.enableEnvisalink:
						self.logger.error("IT-100 lost - re-initializing.")
						self.state = self.States.BOTH_INIT
					elif self.enableEnvisalink and not self.envisalinkConnected and not self.enableIT100:
						self.logger.error("Envisalink lost - re-initializing.")
						self.state = self.States.BOTH_INIT


			self._checkConnectionHealth()

			# Check if the trouble timer counter is timing
			# We need to know if the trouble light has remained off
			# for a few seconds before we assume the trouble is cleared
			if self.troubleClearedTimer > 0:
				self.troubleClearedTimer -= 1
				if self.troubleClearedTimer == 0:
					self.troubleCode = 0
					self.sendTroubleEmail("Trouble Code Cleared.")

			if self.repeatAlarmTripped is True:
				#timeNow = time.time()
				if self.timeNow >= self.repeatAlarmTrippedNext:
					self.repeatAlarmTrippedNext = self.timeNow + 12
					self.speak('speakTextTripped')

			# Failsafe: if the repeatAlarmTripped flag has been stuck True
			# for too long, force-clear it so zone updates resume. See
			# _watchdogRepeatAlarmTripped docstring for the rationale.
			self._watchdogRepeatAlarmTripped()

			# Post-connect status pull: when an interface comes up (or
			# reconnects), send cmd 001 once to refresh the panel state.
			# Critical for detecting trips that happened while the plugin
			# was offline — the panel's response includes LEDMemory and
			# alarm-memory cmd 654 broadcasts that the cmd 654 handler
			# will treat as a historical alarm if we're in the startup
			# grace window. Flag tracking lets us re-pull after a
			# disconnect+reconnect rather than only at process startup.
			# When a connection is down, reset its flag so the NEXT
			# successful connect triggers a fresh pull.
			if not self.it100Connected:
				self._postConnectStatusSent_IT100 = False
			elif not self._postConnectStatusSent_IT100:
				try:
					self._alwaysLog(
						"Requesting fresh panel state via cmd 001 (post-IT-100 "
						"connect). The panel will reply with current zone, "
						"partition, and LED states over the next few seconds."
					)
					self.sendCommand('001')
					self._postConnectStatusSent_IT100 = True
				except Exception as err:
					self.logger.error(f"Post-IT-100-connect status pull failed: {err}")
					# Don't set the flag — we'll retry on next tick
			if not self.envisalinkConnected:
				self._postConnectStatusSent_Envisalink = False
			elif not self._postConnectStatusSent_Envisalink:
				try:
					self._alwaysLog(
						"Requesting fresh panel state via cmd 001 (post-Envisalink "
						"connect). The panel will reply with current zone, "
						"partition, and LED states over the next few seconds."
					)
					self.sendCommand('001')
					self._postConnectStatusSent_Envisalink = True
				except Exception as err:
					self.logger.error(f"Post-Envisalink-connect status pull failed: {err}")
					# Don't set the flag — we'll retry on next tick

			# Process latch timers - check if any have expired
			expiredLatches = []
			for zoneId, expirationTime in list(self.latchTimers.items()):
				if self.timeNow >= expirationTime:
					expiredLatches.append(zoneId)
				else:
					# Update countdown timer
					if zoneId in self.zoneList.values():
						try:
							zone = indigo.devices[zoneId]
							remaining = int(expirationTime - self.timeNow)
							if zone.states.get('latchTimer', 0) != remaining:
								zone.updateStateOnServer(key='latchTimer', value=remaining)
						except:
							pass
			
			# Turn off sensors whose latch has expired
			for zoneId in expiredLatches:
				try:
					zone = indigo.devices[zoneId]
					# Only turn off if zone is in openLatched state (still actually closed)
					# Use latchTimer > 0 as the reliable indicator the zone is still latched
					if zone.states.get('latchTimer', 0) > 0:
						zoneType    = zone.pluginProps.get('zoneType', 'zoneTypeOther')
						displayText = self._getDisplayText(zoneType, isActive=False)
						zone.updateStateOnServer(key='state',            value=kZoneStateClosed)
						zone.updateStateOnServer(key='latchTimer',       value=0)
						zone.updateStateOnServer(key='onOffState',       value=False, uiValue=displayText)
						zone.updateStateOnServer(key='onState',          value=False)
						zone.updateStateOnServer(key='offState',         value=True)
						zone.updateStateOnServer(key='displayText',  value=displayText)
						zone.updateStateOnServer(key='LastChangedShort', value='0m')
						zone.updateStateOnServer(key='LastChangedTimer', value=0)
						self._refreshZoneDisplay(zone)
						self.logger.debug(f"Zone '{zone.name}': Latch expired, display='{displayText}'")
					del self.latchTimers[zoneId]
				except:
					# Zone might have been deleted
					if zoneId in self.latchTimers:
						del self.latchTimers[zoneId]

			# If a minute has elapsed
			if self.timeNow >= self.minuteTracker:

				# Increment all zone changed timers
				self.minuteTracker += 60
				for zoneKey in self.zoneList.keys():
					zone = indigo.devices[self.zoneList[zoneKey]]
					tmr = zone.states["LastChangedTimer"] + 1
					# clearErrorState=False: a timer tick must not clear an "unavailable" marking
					zone.updateStateOnServer(key="LastChangedTimer", value=tmr, clearErrorState=False)
					zone.updateStateOnServer(key="LastChangedShort", value=self.getShortTime(tmr), clearErrorState=False)

				for zoneGroupDeviceId in self.zoneGroupList:
					zoneGroupDevice = indigo.devices[zoneGroupDeviceId]
					tmr = zoneGroupDevice.states["AnyMemberLastChangedTimer"] + 1
					zoneGroupDevice.updateStateOnServer(key="AnyMemberLastChangedTimer", value=tmr, clearErrorState=False)
					zoneGroupDevice.updateStateOnServer(key="AnyMemberLastChangedShort", value=self.getShortTime(tmr), clearErrorState=False)
					tmr = zoneGroupDevice.states["EntireGroupLastChangedTimer"] + 1
					zoneGroupDevice.updateStateOnServer(key="EntireGroupLastChangedTimer", value=tmr, clearErrorState=False)
					zoneGroupDevice.updateStateOnServer(key="EntireGroupLastChangedShort", value=self.getShortTime(tmr), clearErrorState=False)

			# Main-loop pacing. Previously this was implicit — the 1-second
			# port read timeout in maintainIT100Connection/maintainEnvisalinkConnection
			# acted as the de-facto loop tick. We now use a 0.1s read timeout
			# and drain inbound packets in a loop, so the implicit pacing is
			# gone. Add an explicit small sleep here to cap loop rate at ~10Hz
			# when nothing else is happening, preventing CPU thrash without
			# being slow enough to feel laggy for interactive operations.
			# Note: when the port is busy delivering packets, the drain loop
			# inside maintain*Connection consumes them all before we get here,
			# so this sleep doesn't slow down inbound processing.
			self.sleep(0.1)

		self.closeAllPorts()
		self.logger.threaddebug("Exiting Concurrent Thread")


	def stopConcurrentThread(self):
		self.logger.threaddebug("stopConcurrentThread called")
		self.shutdown = True
		self.logger.threaddebug("Exiting stopConcurrentThread")
