#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise Max mode without root, D-Bus or real hardware.

Keeper takes its hardware, power-profiles-daemon, state store and clock as
parameters, so every scenario drives it with fakes and checks exactly what
it would have written. The CLI scenarios further down stub out systemctl,
systemd-run and D-Bus the same way.
"""

import argparse
import contextlib
import ctypes
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback

HERE = pathlib.Path(__file__).resolve().parent
HELPER = HERE.parent / "backend" / "ecomode-max"

spec = importlib.util.spec_from_loader(
    "ecomode_max",
    importlib.machinery.SourceFileLoader("ecomode_max", str(HELPER)))
mx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mx)
eco = mx.eco


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m {label}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label}   {detail}")


class FakeHw:
    def __init__(self, profile="balanced", pwm="2", ac=True, temp=None):
        self.profile = profile
        self.pwm = pwm            # the firmware's own "2" until something writes
        self.ac = ac
        self.temp = temp          # package °C, or None for no reading
        self.writes = []          # ("profile" | "pwm", value), in order
        self.all_writes = []      # the same, but never cleared by a scenario
        self.fail = set()         # {"profile", "pwm"}: make those writes fail
        self.ec_max = None        # what the EC says about full speed; None = no reading

    def _log(self, entry):
        self.writes.append(entry)
        self.all_writes.append(entry)

    def read_profile(self):
        return self.profile

    def write_profile(self, value):
        self._log(("profile", value))
        if "profile" in self.fail:
            return False, "[Errno 5] Input/output error"
        self.profile = value
        return True, None

    def write_pwm(self, value):
        self._log(("pwm", value))
        if "pwm" in self.fail:
            return False, "[Errno 22] Invalid argument"
        self.pwm = value
        return True, None

    def fans_at_max(self):
        return self.ec_max

    def on_ac(self):
        return self.ac

    def read_temp(self):
        return self.temp


class FakePpd:
    """Models power-profiles-daemon: a hold switches the profile to the held
    one, releasing the last hold goes back to the user's selected profile."""

    def __init__(self, hw, selected="balanced", refuse=None):
        self.hw = hw
        self.selected = selected
        self.refuse = refuse
        self.next_cookie = 100
        self.held = set()
        self.log = []

    def hold(self, profile, reason, app_id):
        if self.refuse:
            raise RuntimeError(self.refuse)
        self.next_cookie += 1
        self.held.add(self.next_cookie)
        self.hw.profile = profile
        self.log.append(("hold", profile, reason, app_id))
        return self.next_cookie

    def release(self, cookie):
        self.held.discard(cookie)
        if not self.held:
            self.hw.profile = self.selected
        self.log.append(("release", cookie))


class FakeStore:
    def __init__(self):
        self.state = None
        self.last = None
        self.reason = None

    def save(self, state):
        self.state = dict(state)

    def clear(self):
        self.state = None
        self.reason = None

    def save_last(self, entry):
        self.last = dict(entry)

    def stop_reason(self):
        return self.reason


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


class FakeSession:
    """logind as the keeper sees it: True while the user is logged in, False
    once they are not, None when logind could not be asked."""

    def __init__(self, alive=True):
        self.state = alive

    def alive(self):
        return self.state


def make(fans="max", settle=0, refuse=None, session=True, **hw_kw):
    hw = FakeHw(**hw_kw)
    ppd = FakePpd(hw, refuse=refuse)
    store, clock = FakeStore(), FakeClock()
    keeper = mx.Keeper(fans, hw, ppd, store, clock, FakeSession(session),
                       settle=settle)
    return keeper, hw, ppd, store, clock


def holds(ppd):
    return [e for e in ppd.log if e[0] == "hold"]


def pwm_writes(hw):
    return [w for w in hw.writes if w[0] == "pwm"]


def never_auto(hw):
    """pwm1_enable=2 makes hp-wmi send 0x2E {0, 0}, which stops the fans on
    8BAB; full speed is the only fan value the keeper may ever write."""
    check("pwm1_enable only ever set to 0, never 2",
          ("pwm", "2") not in hw.all_writes
          and all(v == "0" for kind, v in hw.all_writes if kind == "pwm"),
          hw.all_writes)


def poll(k, hw, temp):
    """One run of cmd_run's boost timer: read the package, hand it in."""
    hw.temp = temp
    k.on_temp(hw.read_temp())


def hold_temp(k, hw, clock, temp, until):
    """Keep the package at `temp` up to and including `until`, with both of
    cmd_run's timers firing: the temperature every 5 s, the keep-alive
    every 30 s."""
    while clock.now < until:
        clock.now += 5
        poll(k, hw, temp)
        if clock.now % 30 == 0:
            k.tick()


# ---------------------------------------------------------------------------
# 1. start on AC with fans at maximum
# ---------------------------------------------------------------------------
print("\n\033[1m1. start on AC, fans max\033[0m")
k, hw, ppd, store, clock = make()
alive = k.start(pid=4242)
check("start keeps running", alive is True)
check("performance is held with our reason and app id",
      holds(ppd) == [("hold", "performance", mx.HOLD_REASON, mx.HOLD_APP_ID)],
      ppd.log)
check("profile rewritten with its current value",
      ("profile", "performance") in hw.writes, hw.writes)
check("fans set to full speed", hw.pwm == "0", hw.pwm)
check("fan mode and what it commands are published",
      store.state["fans"] == "max" and store.state["fans_state"] == "max", store.state)
check("the EC holds full speed 125 s past this round",
      store.state["fans_armed_until"] == 1125, store.state)
check("state published as active",
      store.state["state"] == "active" and store.state["pid"] == 4242, store.state)
check("tick marked ok", store.state["last_tick_ok"] is True, store.state)
never_auto(hw)

# ---------------------------------------------------------------------------
# 2. every tick rewrites, the hold is taken once
# ---------------------------------------------------------------------------
print("\n\033[1m2. repeated ticks\033[0m")
hw.writes.clear()
clock.now += 30
k.tick()
check("second tick rewrites the profile again",
      hw.writes.count(("profile", "performance")) == 1, hw.writes)
check("fans rewritten every tick", hw.writes.count(("pwm", "0")) == 1, hw.writes)
check("hold taken once, not per tick", len(holds(ppd)) == 1, ppd.log)
check("last_tick advances", store.state["last_tick"] == 1030, store.state)
check("every round at full speed pushes the hand-back out",
      store.state["fans_armed_until"] == 1155, store.state)
never_auto(hw)

# ---------------------------------------------------------------------------
# 3. battery pauses, AC resumes
# ---------------------------------------------------------------------------
print("\n\033[1m3. battery pause and AC resume\033[0m")
hw.ac = False
hw.writes.clear()
clock.now += 30
alive = k.tick()
check("battery does not end the mode", alive is True)
check("paused with reason battery",
      store.state["state"] == "paused" and store.state["pause_reason"] == "battery",
      store.state)
check("hold released on battery", not ppd.held, ppd.held)
check("no fan write on pause", pwm_writes(hw) == [], hw.writes)
check("no profile rewrite while paused",
      not any(w[0] == "profile" for w in hw.writes), hw.writes)

released = ppd.log[-1][1]
k.on_released(released)          # the daemon echoes our own release back
check("our own release echo does not stop the mode", k.running is True)

hw.writes.clear()
k.tick()
check("second battery tick writes nothing", hw.writes == [], hw.writes)

hw.ac = True
k.tick()
check("AC back: active again", store.state["state"] == "active", store.state)
check("hold re-acquired", len(ppd.held) == 1, ppd.held)
check("fans back to full speed", ("pwm", "0") in hw.writes, hw.writes)
never_auto(hw)

# ---------------------------------------------------------------------------
# 4. a hand-picked profile ends Max
# ---------------------------------------------------------------------------
print("\n\033[1m4. user switches the profile\033[0m")
cookie = next(iter(ppd.held))
ppd.held.discard(cookie)         # the daemon drops every hold on a manual set
hw.writes.clear()
clock.now += 10
k.on_released(cookie)
check("manual profile change ends max", k.running is False)
check("state file cleared", store.state is None)
check("end reason recorded", store.last["reason"] == "profile changed", store.last)
check("no fan write on stop", pwm_writes(hw) == [], hw.writes)
check("the daemon's switch to the picked profile counts as the last 0x10",
      store.last["fans_armed_until"] == 1195, store.last)
check("tick after stop reports stopped", k.tick() is False)
never_auto(hw)

# ---------------------------------------------------------------------------
# 5. profile changed while paused
# ---------------------------------------------------------------------------
print("\n\033[1m5. profile changed while paused on battery\033[0m")
k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.ac = False
k.tick()                          # pause: the daemon goes back to "balanced"
hw.profile = "low-power"          # Fn+P while on battery
hw.ac = True
k.tick()
check("max ends instead of re-holding performance", k.running is False)
check("no second hold was taken", len(holds(ppd)) == 1, ppd.log)
check("reason is profile changed",
      store.last and store.last["reason"] == "profile changed", store.last)
never_auto(hw)

# ---------------------------------------------------------------------------
# 6. hold lost without a signal
# ---------------------------------------------------------------------------
print("\n\033[1m6. hold lost without a signal\033[0m")
k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.profile = "balanced"           # e.g. the daemon restarted and forgot us
k.tick()
check("max ends when the profile drifted away", k.running is False)
check("reason is profile changed",
      store.last and store.last["reason"] == "profile changed", store.last)
never_auto(hw)

k, hw, ppd, store, clock = make()
ppd.hold = (lambda real: lambda *a: (real(*a), setattr(hw, "profile", "balanced"))[0])(ppd.hold)
alive = k.start(pid=1)            # the daemon has not switched the profile yet
check("a fresh hold is not judged on its first tick", alive is True)
never_auto(hw)

# ---------------------------------------------------------------------------
# 7. termination reasons
# ---------------------------------------------------------------------------
print("\n\033[1m7. terminate\033[0m")
k, hw, ppd, store, clock = make()
k.start(pid=1)
clock.now = 1030
k.tick()
clock.now = 1040
hw.writes.clear()
store.reason = "off"
k.on_terminate()
check("terminate after `off` records reason off", store.last["reason"] == "off", store.last)
check("hold released on terminate", not ppd.held, ppd.held)
check("no fan write on terminate", pwm_writes(hw) == [], hw.writes)
check("fans=max: the EC keeps full speed 125 s past the stop",
      store.last["fans_armed_until"] == 1165, store.last)
never_auto(hw)

k, hw, ppd, store, clock = make()
k.start(pid=1)
k.on_terminate()
check("terminate without a reason is logout or shutdown",
      store.last["reason"] == "logout or shutdown", store.last)
never_auto(hw)

# ---------------------------------------------------------------------------
# 8. the daemon refuses the hold
# ---------------------------------------------------------------------------
print("\n\033[1m8. hold refused\033[0m")
k, hw, ppd, store, clock = make(refuse="org.freedesktop.DBus.Error.AccessDenied")
check("start reports failure", k.start(pid=1) is False)
check("reason names the hold",
      store.last["reason"].startswith("hold failed"), store.last)
check("no state file left behind", store.state is None)
check("no fan write when the hold is refused", pwm_writes(hw) == [], hw.writes)
check("nothing reached the EC, so nothing to wait for",
      store.last["fans_armed_until"] is None, store.last)
never_auto(hw)

# ---------------------------------------------------------------------------
# 9. a failed write does not stop the mode
# ---------------------------------------------------------------------------
print("\n\033[1m9. write failure\033[0m")
k, hw, ppd, store, clock = make()
hw.fail = {"profile"}
k.start(pid=1)
check("write failure does not stop the mode", k.running is True)
check("tick marked not ok", store.state["last_tick_ok"] is False, store.state)
check("error names the file", "platform_profile" in store.state["last_error"], store.state)
hw.fail = set()
k.tick()
check("next good tick clears the error",
      store.state["last_tick_ok"] is True and store.state["last_error"] is None,
      store.state)
never_auto(hw)

# ---------------------------------------------------------------------------
# 10. fans=auto never touches pwm1_enable
# ---------------------------------------------------------------------------
print("\n\033[1m10. fans auto\033[0m")
k, hw, ppd, store, clock = make(fans="auto")
k.start(pid=1)
check("fans=auto is published",
      store.state["fans"] == "auto" and store.state["fans_state"] == "auto", store.state)
k.tick()
hw.ac = False
k.tick()
k.on_terminate()
check("fans=auto never writes pwm1_enable", pwm_writes(hw) == [], hw.writes)
check("fans=auto leaves the next session nothing to wait for",
      store.last["fans_armed_until"] is None, store.last)
never_auto(hw)

# ---------------------------------------------------------------------------
# 11. resume from suspend and an unknown power source
# ---------------------------------------------------------------------------
print("\n\033[1m11. resume and unknown power source\033[0m")
k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.writes.clear()
k.on_resume()
check("resume from suspend rewrites at once",
      ("profile", "performance") in hw.writes, hw.writes)
never_auto(hw)

k, hw, ppd, store, clock = make(ac=None)
k.start(pid=1)
check("unknown power source is treated as AC",
      store.state["state"] == "active", store.state)
never_auto(hw)

# ---------------------------------------------------------------------------
# 12. a stop while paused writes no fans
# ---------------------------------------------------------------------------
print("\n\033[1m12. stop while paused writes no fans\033[0m")
k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.writes.clear()
hw.ac = False
clock.now = 1030
k.tick()                          # pause
check("releasing the hold on pause is one more 0x10 at full speed",
      store.state["fans_armed_until"] == 1155, store.state)
clock.now = 1060
k.on_terminate()
check("stop while paused writes no fans", pwm_writes(hw) == [], hw.writes)
check("the end record says how long the EC still keeps full speed",
      store.last["fans_armed_until"] == 1155, store.last)
never_auto(hw)

k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.ac = False
clock.now = 1030
k.tick()                          # pause; the EC lets go at 1155
clock.now = 1200
k.on_terminate()
check("once the EC has taken the fans back there is nothing to wait for",
      store.last["fans_armed_until"] is None, store.last)
never_auto(hw)

# ---------------------------------------------------------------------------
# 13. a start that has to wait for the EC to take the fans back
# ---------------------------------------------------------------------------
print("\n\033[1m13. start with a settle\033[0m")
k, hw, ppd, store, clock = make(fans="auto", settle=125)
alive = k.start(pid=1)
check("a settling start keeps running", alive is True)
check("published as paused while the fans settle",
      store.state["state"] == "paused" and store.state["pause_reason"] == "fans settling",
      store.state)
check("the deadline is published", store.state["settle_until"] == 1125, store.state)
check("the earlier session's hand-back is carried",
      store.state["fans_armed_until"] == 1125, store.state)
check("no hold and no writes while settling",
      holds(ppd) == [] and hw.writes == [], (ppd.log, hw.writes))
for t in (1030, 1060, 1090, 1120, 1124):
    clock.now = t
    k.tick()
check("ticks before the deadline stay silent",
      holds(ppd) == [] and hw.writes == [] and store.state["state"] == "paused",
      (ppd.log, hw.writes, store.state))
clock.now = 1125
k.tick()
check("the first tick at the deadline holds performance",
      len(holds(ppd)) == 1 and store.state["state"] == "active", (ppd.log, store.state))
check("... and rewrites the profile", ("profile", "performance") in hw.writes, hw.writes)
check("... and leaves the fans to the firmware",
      pwm_writes(hw) == [] and store.state["fans_state"] == "auto", (hw.writes, store.state))
check("the deadline is cleared", store.state["settle_until"] is None, store.state)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="auto", settle=100)
k.start(pid=1)
clock.now = 1040
k.on_terminate()
check("a stop while settling hands the rest of the wait on",
      store.last["fans_armed_until"] == 1100, store.last)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="boost", settle=125)
k.start(pid=1)
hw.profile = "low-power"          # picked by hand while Max was silent
clock.now = 1150
k.tick()
check("a profile picked while settling ends Max instead of taking performance",
      k.running is False and holds(ppd) == [] and store.last["reason"] == "profile changed",
      (ppd.log, store.last))
never_auto(hw)

# ---------------------------------------------------------------------------
# 14. boost: hot, cool, hand back, hot again
# ---------------------------------------------------------------------------
print("\n\033[1m14. boost\033[0m")
k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)                    # 1000: hold and profile rewrite
poll(k, hw, 70)
check("70 °C: the fans are left alone",
      pwm_writes(hw) == [] and store.state["fans_state"] == "auto", (hw.writes, store.state))
clock.now = 1005
poll(k, hw, 86)
check("86 °C: full speed at once, without waiting for a tick",
      hw.writes[-1] == ("pwm", "0") and store.state["fans_state"] == "max",
      (hw.writes, store.state))
check("the EC holds full speed 125 s past that write",
      store.state["fans_armed_until"] == 1130, store.state)
hw.writes.clear()
clock.now = 1020
k.tick()
check("a tick while hot rewrites full speed", ("pwm", "0") in hw.writes, hw.writes)

hold_temp(k, hw, clock, 60, until=1195)       # cool since 1025
check("60 °C for 170 s: still full speed",
      store.state["state"] == "active" and store.state["fans_state"] == "max",
      store.state)
hold_temp(k, hw, clock, 60, until=1200)
hw.writes.clear()
clock.now = 1205
poll(k, hw, 60)                               # 180 s since 1025
check("180 s at or below 65 °C: Max pauses while the fans settle",
      store.state["state"] == "paused" and store.state["pause_reason"] == "fans settling",
      store.state)
check("the hold is released for the hand-back", not ppd.held, ppd.held)
check("no fan write on the way back", pwm_writes(hw) == [], hw.writes)
check("settling lasts 125 s", store.state["settle_until"] == 1330, store.state)
check("fans_state is auto from here",
      store.state["fans_state"] == "auto" and store.state["fans_armed_until"] == 1330,
      store.state)
k.on_released(ppd.log[-1][1])                 # the echo of our own release
check("the hand-back release echo does not stop Max", k.running is True)

hold_temp(k, hw, clock, 70, until=1245)       # a tick at 1230 falls in here
check("while settling: no hold and no writes",
      not ppd.held and hw.writes == [] and store.state["pause_reason"] == "fans settling",
      (ppd.held, hw.writes, store.state))
clock.now = 1250
poll(k, hw, 90)
check("90 °C while settling: the settle is called off",
      store.state["state"] == "active" and store.state["settle_until"] is None,
      store.state)
check("... the hold is back", len(ppd.held) == 1, ppd.log)
check("... the profile rewritten and the fans at full speed",
      ("profile", "performance") in hw.writes and ("pwm", "0") in hw.writes
      and store.state["fans_state"] == "max", (hw.writes, store.state))

hold_temp(k, hw, clock, 60, until=1300)       # cool since 1255 ...
clock.now = 1305
poll(k, hw, 66)                               # ... broken by one warmer reading
hold_temp(k, hw, clock, 65, until=1485)       # cool again since 1310
check("a reading above 65 °C starts the 180 s over",
      store.state["state"] == "active" and store.state["fans_state"] == "max",
      store.state)
hw.writes.clear()
clock.now = 1490
poll(k, hw, 65)                               # 180 s since 1310, 65 °C counts
check("65 °C counts as cool: settling again",
      store.state["pause_reason"] == "fans settling" and store.state["settle_until"] == 1615,
      store.state)
hold_temp(k, hw, clock, 60, until=1610)       # ticks up to 1590 stay silent
check("no writes until the deadline", hw.writes == [] and not ppd.held, (hw.writes, ppd.held))
clock.now = 1620
k.tick()
check("past the deadline: active again with the fans automatic",
      store.state["state"] == "active" and store.state["fans_state"] == "auto",
      store.state)
check("... holding and rewriting, with no fan write",
      len(ppd.held) == 1 and ("profile", "performance") in hw.writes
      and pwm_writes(hw) == [], (ppd.held, hw.writes))
never_auto(hw)

k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)
poll(k, hw, 84)
check("84 °C is not hot yet", store.state["fans_state"] == "auto" and pwm_writes(hw) == [],
      (store.state, hw.writes))
poll(k, hw, 85)
check("85 °C is", store.state["fans_state"] == "max" and pwm_writes(hw) == [("pwm", "0")],
      (store.state, hw.writes))
never_auto(hw)

# ---------------------------------------------------------------------------
# 15. boost: what it does not act on
# ---------------------------------------------------------------------------
print("\n\033[1m15. boost: no reading, battery, stop and other fan modes\033[0m")
k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)
poll(k, hw, 90)                               # hot at 1000
hold_temp(k, hw, clock, 60, until=1100)       # cool since 1005
before = dict(store.state)
hw.writes.clear()
k.on_temp(None)
check("no reading changes nothing", store.state == before and hw.writes == [],
      (store.state, hw.writes))
hold_temp(k, hw, clock, 60, until=1185)
check("... nor breaks the cool spell: the hand-back comes 180 s after it began",
      store.state["pause_reason"] == "fans settling", store.state)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)
poll(k, hw, 90)                               # hot at 1000
hold_temp(k, hw, clock, 60, until=1100)       # cool since 1005
hw.ac = False
clock.now = 1110
k.tick()                                      # battery pause
hw.writes.clear()
poll(k, hw, 40)
poll(k, hw, 95)
check("on battery boost does not act on the temperature",
      hw.writes == [] and store.state["pause_reason"] == "battery", (hw.writes, store.state))
hw.ac = True
clock.now = 1400
k.tick()
check("AC back: full speed again", store.state["state"] == "active"
      and ("pwm", "0") in hw.writes, (store.state, hw.writes))
hold_temp(k, hw, clock, 60, until=1580)
check("the cool spell starts over after the pause",
      store.state["state"] == "active" and store.state["fans_state"] == "max", store.state)
clock.now = 1585
poll(k, hw, 60)
check("... and hands back 180 s after AC returned",
      store.state["pause_reason"] == "fans settling", store.state)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)
poll(k, hw, 90)
hold_temp(k, hw, clock, 60, until=1185)       # settling until 1310
hw.ac = False
clock.now = 1200
k.tick()
check("battery while settling shows as a battery pause",
      store.state["pause_reason"] == "battery", store.state)
hw.writes.clear()
poll(k, hw, 95)
check("... and heat is not acted on there", hw.writes == [], hw.writes)
hw.ac = True
clock.now = 1230
k.tick()
check("AC back before the deadline: still settling",
      store.state["pause_reason"] == "fans settling" and not ppd.held,
      (store.state, ppd.held))
clock.now = 1320
k.tick()
check("after the deadline: active with the fans automatic",
      store.state["state"] == "active" and store.state["fans_state"] == "auto"
      and pwm_writes(hw) == [], (store.state, hw.writes))
never_auto(hw)

k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)
poll(k, hw, 90)
hold_temp(k, hw, clock, 60, until=1185)       # settling until 1310
hw.writes.clear()
clock.now = 1200
store.reason = "off"
k.on_terminate()
check("a stop while settling writes nothing and takes no hold",
      hw.writes == [] and len(holds(ppd)) == 1, (hw.writes, ppd.log))
check("... and hands the rest of the wait on",
      store.last["fans_armed_until"] == 1310, store.last)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="max")
k.start(pid=1)
hold_temp(k, hw, clock, 40, until=1300)
check("fans=max ignores the temperature: still full speed after 300 s at 40 °C",
      store.state["state"] == "active" and store.state["fans_state"] == "max", store.state)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="auto")
k.start(pid=1)
poll(k, hw, 99)
check("fans=auto never runs the fans, however hot",
      pwm_writes(hw) == [] and store.state["fans_state"] == "auto", (hw.writes, store.state))
never_auto(hw)

k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)
hw.fail = {"pwm"}
poll(k, hw, 90)
check("a failed full-speed write when it gets hot is reported",
      store.state["last_tick_ok"] is False and "pwm1_enable" in store.state["last_error"]
      and store.state["fans_state"] == "max", store.state)
hw.fail = set()
hw.writes.clear()
clock.now = 1030
k.tick()
check("... and the next tick tries again",
      ("pwm", "0") in hw.writes and store.state["last_tick_ok"] is True,
      (hw.writes, store.state))
never_auto(hw)


# ---------------------------------------------------------------------------
# CLI harness
# ---------------------------------------------------------------------------

def cli_args(**kw):
    base = dict(json=True, fans="auto", settle=0, uid=1000)
    base.update(kw)
    return argparse.Namespace(**base)


def capture(fn, a):
    # stderr is swallowed too: the refusals explain themselves there, and
    # that text would only clutter the test output.
    buf = io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, io.StringIO()
    try:
        code = fn(a)
    finally:
        sys.stdout, sys.stderr = old
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out else None)


TMP = None


def temp_run():
    global TMP
    TMP = pathlib.Path(tempfile.mkdtemp(prefix="ecomode-max-test-"))
    run = TMP / "run"
    run.mkdir(parents=True)
    eco.STATE_DIR = str(run)
    eco.STATE_FILE = str(run / "state.json")
    eco.MAX_STATE_FILE = str(run / "max.json")
    eco.MAX_LAST_FILE = str(run / "max-last.json")
    eco.MAX_STOP_FLAG = str(run / "max-stop-reason")
    eco.WATTS_FILE = str(run / "watts")
    eco.POWERD_FLAG = str(run / "powerd-started")
    return run


class Recorder:
    """Stands in for sh(): records argv, fails commands starting with fail_on."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.fail_on and cmd[0] == self.fail_on:
            return subprocess.CompletedProcess(cmd, 1, stdout='{"ok": false}', stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")


ORIG = {
    "sh": mx.sh, "invoking_uid": mx.invoking_uid,
    "ppd_available": mx.ppd_available,
    "user_manager_active": mx.user_manager_active,
    "unit_active": mx.unit_active, "wait_for_state": mx.wait_for_state,
    "max_support": eco.max_support, "fans_controllable": eco.fans_controllable,
    "max_state": eco.max_state, "find_hp_hwmon": eco.find_hp_hwmon,
    "watts_active": mx.watts_active, "RAPL": eco.RAPL,
    "battery_draw_w": eco.battery_draw_w, "pkg_temp_c": eco.pkg_temp_c,
    "write_path": eco.write_path, "_gio": mx._gio,
    "_signal_add": mx._signal_add, "time": mx.time, "Keeper": mx.Keeper,
    "uid_state_native": mx.uid_state_native,
    "PLATFORM_PROFILE": eco.PLATFORM_PROFILE, "on_ac": eco.on_ac,
}


ORIG_IS_ON = eco.is_on


def stub_env(fail_on=None):
    rec = Recorder(fail_on)
    mx.sh = rec
    mx.invoking_uid = lambda: 1000
    mx.ppd_available = lambda: True
    mx.user_manager_active = lambda uid: True
    mx.unit_active = lambda: False
    mx.wait_for_state = lambda timeout=3.0, step=0.1: {
        "pid": 1, "state": "active", "fans": "auto"}
    eco.max_support = lambda: (True, None)
    eco.fans_controllable = lambda: True
    eco.pkg_temp_c = lambda: 60
    return rec


def reset():
    for name in ("sh", "invoking_uid", "ppd_available", "user_manager_active",
                 "unit_active", "wait_for_state", "_gio", "_signal_add", "time",
                 "Keeper", "uid_state_native"):
        setattr(mx, name, ORIG[name])
    for name in ("max_support", "fans_controllable", "max_state", "find_hp_hwmon",
                 "pkg_temp_c", "write_path", "PLATFORM_PROFILE", "on_ac"):
        setattr(eco, name, ORIG[name])
    mx.watts_active = ORIG["watts_active"]
    eco.RAPL = ORIG["RAPL"]
    eco.battery_draw_w = ORIG["battery_draw_w"]
    if TMP:
        shutil.rmtree(TMP, ignore_errors=True)


def started(rec):
    """argv of the one systemd-run the recorder saw, or []."""
    runs = [c for c in rec.calls if c[0] == "systemd-run"]
    return runs[0] if len(runs) == 1 else []


def last_written(**entry):
    pathlib.Path(eco.MAX_LAST_FILE).write_text(json.dumps(entry))


class ReachedDbus(Exception):
    pass


def no_dbus():
    # Past its refusals `run` would connect to the real system bus and take
    # a real power-profiles-daemon hold; a refusal test must never get there.
    raise ReachedDbus()


def run_code(**kw):
    try:
        return capture(mx.cmd_run, cli_args(**kw))[0]
    except ReachedDbus:
        return "went on to D-Bus"


# ---------------------------------------------------------------------------
# 16. run and cleanup belong to systemd
# ---------------------------------------------------------------------------
print("\n\033[1m16. run/cleanup only under systemd\033[0m")
mx._gio = no_dbus
os.environ.pop("INVOCATION_ID", None)
code = run_code()
check("run refuses without INVOCATION_ID", code == 2, code)
code, _ = capture(mx.cmd_cleanup, cli_args())
check("cleanup refuses without INVOCATION_ID", code == 2, code)
reset()

# ---------------------------------------------------------------------------
# 17. run: refusals before anything starts
# ---------------------------------------------------------------------------
print("\n\033[1m17. run refuses what it cannot do\033[0m")
temp_run()
mx._gio = no_dbus
os.environ["INVOCATION_ID"] = "test"
code = run_code(settle=-1)
check("a negative settle is refused", code == 2, code)
code = run_code(settle=126)
check("a settle longer than one hand-back is refused", code == 2, code)
eco.find_hp_hwmon = lambda: None
code = run_code(fans="boost", settle=40)
last = eco.read_json(eco.MAX_LAST_FILE) or {}
check("boost without hp fan control ends as unsupported",
      code == 1 and last.get("reason") == "unsupported: no hp fan control", (code, last))
check("... and still passes the inherited wait on",
      abs((last.get("fans_armed_until") or 0) - (int(time.time()) + 40)) <= 1, last)
os.environ.pop("INVOCATION_ID")
reset()

# ---------------------------------------------------------------------------
# 18. on: refusals
# ---------------------------------------------------------------------------
print("\n\033[1m18. on refuses what it cannot do\033[0m")
temp_run()
stub_env()
eco.max_support = lambda: (False, "board 8C78 is not a known HP OMEN with the 120 s EC timeout")
code, res = capture(mx.cmd_on, cli_args())
check("unsupported board -> ok false with the reason",
      code == 1 and "8C78" in res["reason"], res)
reset()

temp_run()
stub_env()
eco.fans_controllable = lambda: False
code, res = capture(mx.cmd_on, cli_args(fans="max"))
check("fans max without hp hwmon is refused", code == 1 and "fan" in res["reason"], res)
reset()

temp_run()
rec = stub_env()
eco.fans_controllable = lambda: False
code, res = capture(mx.cmd_on, cli_args(fans="boost"))
check("boost without hp hwmon is refused",
      code == 1 and "fan" in res["reason"] and started(rec) == [], (res, rec.calls))
reset()

temp_run()
rec = stub_env()
eco.pkg_temp_c = lambda: None
code, res = capture(mx.cmd_on, cli_args(fans="boost"))
check("boost without a package temperature is refused",
      code == 1 and res["reason"] == "no CPU package temperature sensor"
      and started(rec) == [], (res, rec.calls))
reset()

temp_run()
rec = stub_env()
eco.fans_controllable = lambda: False
eco.pkg_temp_c = lambda: None
code, res = capture(mx.cmd_on, cli_args(fans="auto"))
check("auto needs neither", code == 0 and len(started(rec)) > 0, (res, rec.calls))
reset()

temp_run()
stub_env()
mx.invoking_uid = lambda: None
code, res = capture(mx.cmd_on, cli_args())
check("no PKEXEC_UID/SUDO_UID is refused", code == 1 and "pkexec" in res["reason"], res)
reset()

temp_run()
stub_env()
mx.ppd_available = lambda: False
code, res = capture(mx.cmd_on, cli_args())
check("no power-profiles-daemon is refused",
      code == 1 and "power-profiles-daemon" in res["reason"], res)
reset()

# ---------------------------------------------------------------------------
# 19. on: the happy path
# ---------------------------------------------------------------------------
print("\n\033[1m19. on starts one session-bound unit\033[0m")
temp_run()
rec = stub_env()
code, res = capture(mx.cmd_on, cli_args(fans="max"))
runs = [c for c in rec.calls if c[0] == "systemd-run"]
check("on reports ok", code == 0 and res["ok"] is True, res)
check("exactly one transient unit", len(runs) == 1, rec.calls)
line = " ".join(runs[0])
check("bound to the invoking user's manager", "BindsTo=user@1000.service" in line, line)
check("cleanup runs after every stop",
      f"ExecStopPost={mx.SELF_PATH} cleanup" in line, line)
check("state dir writable under ProtectSystem=strict",
      f"ReadWritePaths={eco.STATE_DIR}" in line and "ProtectSystem=strict" in line, line)
check("keeper gets the user, the fan choice and no wait",
      runs[0][-8:] == [mx.SELF_PATH, "run", "--uid", "1000",
                       "--fans", "max", "--settle", "0"], runs[0])
reset()

# ---------------------------------------------------------------------------
# 20. on: a failed start is explained only by its own end record
# ---------------------------------------------------------------------------
print("\n\033[1m20. on when the keep-alive does not start\033[0m")
temp_run()
stub_env()
mx.wait_for_state = lambda timeout=3.0, step=0.1: None
pathlib.Path(eco.MAX_LAST_FILE).write_text(json.dumps({"ended_at": 1, "reason": "off"}))
code, res = capture(mx.cmd_on, cli_args())
check("a stale end record is not blamed for a failed start",
      code == 1 and res["reason"] == "keep-alive did not start", res)
reset()

temp_run()
stub_env()


def failed_start(timeout=3.0, step=0.1):
    mx.FileStore().save_last({"ended_at": int(time.time()) + 1, "reason": "hold failed: denied"})
    return None


mx.wait_for_state = failed_start
code, res = capture(mx.cmd_on, cli_args())
check("the keeper's own failure reason is passed on",
      code == 1 and res["reason"] == "hold failed: denied", res)
reset()

temp_run()
rec = stub_env()
eco.max_state = lambda: {"pid": 1, "state": "active", "fans": "auto"}
mx.wait_for_state = lambda timeout=3.0, step=0.1: None


def rec_stop_records_restart(cmd):
    if cmd[:2] == ["systemctl", "stop"]:
        # The stopped keeper records why it ended, within the same second.
        mx.FileStore().save_last({"ended_at": int(time.time()), "reason": "restart"})
    return rec(cmd)


mx.sh = rec_stop_records_restart
code, res = capture(mx.cmd_on, cli_args(fans="max"))
check("the stopped unit's restart record is not blamed for a failed restart",
      code == 1 and res["reason"] == "keep-alive did not start", res)
reset()

# ---------------------------------------------------------------------------
# 21. on: eco is restored first, and a failed restore aborts
# ---------------------------------------------------------------------------
print("\n\033[1m21. eco -> max\033[0m")
temp_run()
rec = stub_env()
pathlib.Path(eco.STATE_FILE).write_text(json.dumps({"level": "gentle", "knobs": {}}))
code, res = capture(mx.cmd_on, cli_args())
order = [c[0] for c in rec.calls if c[0] in (mx.ECO_PATH, "systemd-run")]
check("eco is turned off before max starts", order == [mx.ECO_PATH, "systemd-run"], rec.calls)
reset()

temp_run()
rec = stub_env(fail_on=mx.ECO_PATH)
pathlib.Path(eco.STATE_FILE).write_text(json.dumps({"level": "gentle", "knobs": {}}))
code, res = capture(mx.cmd_on, cli_args())
check("failed eco restore aborts max", code == 1 and "eco" in res["reason"], res)
check("no unit started after the failed restore",
      not any(c[0] == "systemd-run" for c in rec.calls), rec.calls)
reset()

# ---------------------------------------------------------------------------
# 22. on while running: no-op or restart
# ---------------------------------------------------------------------------
print("\n\033[1m22. on while running\033[0m")
temp_run()
rec = stub_env()
eco.max_state = lambda: {"pid": 1, "state": "active", "fans": "auto"}
code, res = capture(mx.cmd_on, cli_args(fans="auto"))
check("same settings while running is a no-op",
      code == 0 and res.get("noop") is True and rec.calls == [], (res, rec.calls))

stop_reason = {}


def rec_restart(cmd):
    if cmd[:2] == ["systemctl", "stop"]:
        stop_reason["seen"] = eco.read_path(eco.MAX_STOP_FLAG)
    return rec(cmd)


mx.sh = rec_restart
code, res = capture(mx.cmd_on, cli_args(fans="max"))
stops = [i for i, c in enumerate(rec.calls) if c[:2] == ["systemctl", "stop"]]
starts = [i for i, c in enumerate(rec.calls) if c[0] == "systemd-run"]
check("new fan choice stops the old unit first",
      stops and starts and stops[0] < starts[0], rec.calls)
check("the keeper is told it is a restart", stop_reason.get("seen") == "restart", stop_reason)
reset()

# ---------------------------------------------------------------------------
# 23. on: a new session first waits for the EC to take the fans back
# ---------------------------------------------------------------------------
print("\n\033[1m23. on: settle for the new session\033[0m")


def settle_of(argv):
    """N from an argv ending in `--settle N`, else None."""
    return int(argv[-1]) if argv[-2:-1] == ["--settle"] else None


def settles_about(argv, secs):
    n = settle_of(argv)
    return n is not None and abs(n - secs) <= 2


temp_run()
rec = stub_env()
last_written(ended_at=int(time.time()), reason="off",
             fans_armed_until=int(time.time()) + 60)
code, res = capture(mx.cmd_on, cli_args(fans="auto"))
argv = started(rec)
check("auto after a session at full speed waits out the rest",
      code == 0 and argv[-4:-1] == ["--fans", "auto", "--settle"]
      and settles_about(argv, 60), (res, argv))
check("the end record is still gone before the start",
      not pathlib.Path(eco.MAX_LAST_FILE).exists())
reset()

temp_run()
rec = stub_env()
last_written(ended_at=int(time.time()), reason="off",
             fans_armed_until=int(time.time()) + 60)
capture(mx.cmd_on, cli_args(fans="boost"))
argv = started(rec)
check("so does boost",
      argv[-4:-1] == ["--fans", "boost", "--settle"]
      and settles_about(argv, 60), argv)
reset()

temp_run()
rec = stub_env()
last_written(ended_at=int(time.time()), reason="off",
             fans_armed_until=int(time.time()) + 60)
capture(mx.cmd_on, cli_args(fans="max"))
check("max has nothing to wait for",
      started(rec)[-4:] == ["--fans", "max", "--settle", "0"], rec.calls)
reset()

temp_run()
rec = stub_env()
last_written(ended_at=int(time.time()) - 300, reason="off",
             fans_armed_until=int(time.time()) - 175)
capture(mx.cmd_on, cli_args(fans="auto"))
check("a hand-back that is over means no wait", settle_of(started(rec)) == 0, rec.calls)
reset()

temp_run()
rec = stub_env()
last_written(ended_at=int(time.time()), reason="off",
             fans_armed_until=int(time.time()) + 100000)
capture(mx.cmd_on, cli_args(fans="auto"))
check("never longer than one hand-back, whatever the record says",
      settle_of(started(rec)) == 125, rec.calls)
reset()

temp_run()
rec = stub_env()
last_written(ended_at=int(time.time()), reason="off", fans_armed_until="soon")
code, res = capture(mx.cmd_on, cli_args(fans="auto"))
check("a malformed record does not block the start",
      code == 0 and settle_of(started(rec)) == 0, (res, rec.calls))
reset()

temp_run()
rec = stub_env()
eco.max_state = lambda: {"pid": 1, "state": "active", "fans": "max"}


def rec_stop_leaves_fans(cmd):
    if cmd[:2] == ["systemctl", "stop"]:
        # The keeper stopped for the switch records its own hand-back.
        t = int(time.time())
        mx.FileStore().save_last({"ended_at": t, "reason": "restart",
                                  "fans_armed_until": t + 125})
    return rec(cmd)


mx.sh = rec_stop_leaves_fans
capture(mx.cmd_on, cli_args(fans="auto"))
check("the session stopped for the switch is waited out too",
      settles_about(started(rec), 125), rec.calls)
reset()

# ---------------------------------------------------------------------------
# 24. off
# ---------------------------------------------------------------------------
print("\n\033[1m24. off\033[0m")
temp_run()
rec = stub_env()
eco.max_state = lambda: {"pid": 1, "state": "active", "fans": "max"}
seen = {}


def rec_off(cmd):
    if cmd[:2] == ["systemctl", "stop"]:
        seen["reason"] = eco.read_path(eco.MAX_STOP_FLAG)
        eco.max_state = lambda: None      # the unit is gone after stop
    return rec(cmd)


mx.sh = rec_off
code, res = capture(mx.cmd_off, cli_args())
check("off tells the keeper why before stopping", seen.get("reason") == "off", seen)
check("off reports ok", code == 0 and res["ok"] is True, res)
reset()

temp_run()
stub_env()
code, res = capture(mx.cmd_off, cli_args())
check("off when nothing runs is a no-op", code == 0 and res.get("noop") is True, res)
reset()

# ---------------------------------------------------------------------------
# 25. cleanup after a crash
# ---------------------------------------------------------------------------
print("\n\033[1m25. cleanup after a crash\033[0m")


def crash_cleanup(state):
    """Run cleanup as systemd does after the keeper died with `state` still
    published. Sysfs writes are recorded instead of made."""
    writes = []
    eco.write_path = lambda path, value: (writes.append((path, value)), (True, None))[1]
    pathlib.Path(eco.MAX_STATE_FILE).write_text(json.dumps(state))
    os.environ["INVOCATION_ID"] = "test"
    os.environ["SERVICE_RESULT"] = "signal"
    try:
        capture(mx.cmd_cleanup, cli_args())
    finally:
        os.environ.pop("INVOCATION_ID")
        os.environ.pop("SERVICE_RESULT")
    return writes, eco.read_json(eco.MAX_LAST_FILE) or {}


def fake_hwmon(pwm):
    hw_dir = TMP / "hwmon6"
    hw_dir.mkdir()
    (hw_dir / "pwm1_enable").write_text(pwm)
    eco.find_hp_hwmon = lambda: str(hw_dir)
    return hw_dir / "pwm1_enable"


temp_run()
stub_env()
pwm_file = fake_hwmon("0")
armed = int(time.time()) + 90
writes, last = crash_cleanup(
    {"pid": 1, "fans": "boost", "fans_state": "auto", "state": "paused",
     "pause_reason": "fans settling", "settle_until": armed,
     "fans_armed_until": armed})
check("cleanup writes no fans", writes == [] and pwm_file.read_text() == "0", writes)
check("crash is recorded as the end reason",
      last.get("reason") == "stopped unexpectedly (signal)", last)
check("the crash record carries the hand-back time from the state",
      last.get("fans_armed_until") == armed, last)
check("stale state removed", not pathlib.Path(eco.MAX_STATE_FILE).exists())
reset()

temp_run()
stub_env()
pwm_file = fake_hwmon("0")
writes, last = crash_cleanup(
    {"pid": 1, "fans": "max", "fans_state": "max", "fans_prior": "2",
     "state": "active", "fans_armed_until": int(time.time()) + 100})
check("not even a recorded prior fan value is written back",
      writes == [] and pwm_file.read_text() == "0", writes)
check("a crash while holding at full speed: the daemon's revert was the last 0x10",
      abs((last.get("fans_armed_until") or 0) - (int(time.time()) + 125)) <= 1, last)
reset()

temp_run()
stub_env()
fake_hwmon("2")
writes, last = crash_cleanup(
    {"pid": 1, "fans": "boost", "fans_state": "auto", "state": "active",
     "fans_armed_until": int(time.time()) - 600})
check("a crash long after the hand-back leaves nothing to wait for",
      writes == [] and "fans_armed_until" in last and last["fans_armed_until"] is None,
      (writes, last))
reset()

temp_run()
os.environ["INVOCATION_ID"] = "test"
capture(mx.cmd_cleanup, cli_args())
os.environ.pop("INVOCATION_ID")
check("cleanup after a clean stop writes no crash record",
      eco.read_json(eco.MAX_LAST_FILE) is None)
reset()

# ---------------------------------------------------------------------------
# 26. FileStore
# ---------------------------------------------------------------------------
print("\n\033[1m26. state files\033[0m")
temp_run()
store = mx.FileStore()
store.save({"pid": 1})
check("state file is world-readable",
      os.stat(eco.MAX_STATE_FILE).st_mode & 0o777 == 0o644,
      oct(os.stat(eco.MAX_STATE_FILE).st_mode))
pathlib.Path(eco.MAX_STOP_FLAG).write_text("off")
check("stop reason read back", store.stop_reason() == "off")
store.clear()
check("clear removes state and flag",
      not os.path.exists(eco.MAX_STATE_FILE) and not os.path.exists(eco.MAX_STOP_FLAG))
store.save_last({"ended_at": 1, "reason": "off"})
check("last entry written", eco.read_json(eco.MAX_LAST_FILE)["reason"] == "off")
reset()

# ---------------------------------------------------------------------------
# 27. the one place that writes pwm1_enable
# ---------------------------------------------------------------------------
print("\n\033[1m27. sysfs: full speed and nothing else\033[0m")
temp_run()
pwm_file = fake_hwmon("2")
sysfs = mx.SysfsHw(str(pwm_file.parent))
ok, _ = sysfs.write_pwm("2")
check("pwm1_enable=2 is refused where sysfs is written",
      ok is False and pwm_file.read_text() == "2", pwm_file.read_text())
ok, _ = sysfs.write_pwm("0")
check("full speed is written", ok is True and pwm_file.read_text() == "0", pwm_file.read_text())
check("no hp hwmon: no fan write", mx.SysfsHw(None).write_pwm("0")[0] is False)
eco.pkg_temp_c = lambda: 77
check("the package temperature is read for boost", mx.SysfsHw(None).read_temp() == 77)
reset()

# ---------------------------------------------------------------------------
# 28. watt meter
# ---------------------------------------------------------------------------
print("\n\033[1m28. watt meter\033[0m")
m = mx.WattMeter(range_uj=262143328850, max_gap=9)
check("first reading has nothing to compare", m.feed(1_000_000, 10.0) is None)
check("2 J over 1 s is 2 W", m.feed(3_000_000, 11.0) == 2.0)
check("95 J over 1 s is 95 W", m.feed(98_000_000, 12.0) == 95.0)

m = mx.WattMeter(range_uj=262143328850, max_gap=9)
m.feed(262143328000, 20.0)
check("a wrapped counter is folded over its range", m.feed(1000850, 21.0) == 1.0)

m = mx.WattMeter(range_uj=None, max_gap=9)
m.feed(5_000_000, 1.0)
check("a backwards counter with no known range is dropped", m.feed(1_000_000, 2.0) is None)

m = mx.WattMeter(range_uj=262143328850, max_gap=9)
m.feed(1_000_000, 1.0)
check("a gap longer than max_gap (suspend) is dropped",
      m.feed(900_000_000, 3600.0) is None)
check("the reading after a dropped gap is good again",
      m.feed(930_000_000, 3601.0) == 30.0)

m = mx.WattMeter(range_uj=262143328850, max_gap=9)
m.feed(1_000_000, 1.0)
check("a clock that did not advance gives nothing", m.feed(2_000_000, 1.0) is None)

m = mx.WattMeter(range_uj=262143328850, max_gap=9)
m.feed(0, 1.0)
check("an impossible reading (>1000 W) is dropped", m.feed(5_000_000_000, 2.0) is None)

# ---------------------------------------------------------------------------
# 29. one sampler round
# ---------------------------------------------------------------------------
print("\n\033[1m29. sampler round\033[0m")
temp_run()
rapl = TMP / "rapl"
rapl.mkdir()
(rapl / "energy_uj").write_text("1000000")
eco.RAPL = str(rapl)
eco.battery_draw_w = lambda: 63.0
meter = mx.WattMeter(range_uj=262143328850, max_gap=9)
check("first round publishes nothing",
      mx.watts_entry(meter, 3, 100.0, 1790000000.0) is None)
(rapl / "energy_uj").write_text("283000000")
entry = mx.watts_entry(meter, 3, 103.0, 1790000003.0)
check("second round publishes the average",
      entry is not None and entry["cpu_w"] == 94.0, entry)
check("battery draw rides along", entry and entry["battery_w"] == 63.0, entry)
check("wall-clock stamp and interval are published",
      entry and entry["at"] == 1790000003 and entry["interval"] == 3, entry)
(rapl / "energy_uj").write_text("")
check("an unreadable counter publishes nothing",
      mx.watts_entry(meter, 3, 106.0, 1790000006.0) is None)
reset()

# ---------------------------------------------------------------------------
# 30. watts on / off
# ---------------------------------------------------------------------------
print("\n\033[1m30. watts on/off\033[0m")


def watts_args(action, interval=3):
    return argparse.Namespace(json=True, action=action, interval=interval)


def fake_rapl():
    rapl = TMP / "rapl"
    rapl.mkdir()
    (rapl / "energy_uj").write_text("1")
    eco.RAPL = str(rapl)


temp_run()
rec = stub_env()
fake_rapl()
mx.watts_active = lambda: False
code, res = capture(mx.cmd_watts, watts_args("on"))
runs = [c for c in rec.calls if c[0] == "systemd-run"]
check("watts on starts one unit",
      code == 0 and res["ok"] is True and len(runs) == 1, (res, rec.calls))
line = " ".join(runs[0]) if runs else ""
check("the sampler is its own unit", f"--unit={mx.WATTS_UNIT}" in line, line)
check("the sampler is bound to the user's session",
      "BindsTo=user@1000.service" in line, line)
check("its file is removed after every stop",
      f"ExecStopPost=-/usr/bin/rm -f {eco.WATTS_FILE}" in line, line)
check("the sampler gets the user and the interval",
      bool(runs) and runs[0][-5:] == ["watts-run", "--uid", "1000", "--interval", "3"],
      runs)
reset()

temp_run()
stub_env()
code, res = capture(mx.cmd_watts, watts_args("on", interval=0))
check("interval below 1 s is refused", code == 1 and "interval" in res["reason"], res)
code, res = capture(mx.cmd_watts, watts_args("on", interval=31))
check("interval above 30 s is refused", code == 1 and "interval" in res["reason"], res)
reset()

temp_run()
stub_env()
eco.RAPL = str(TMP / "no-rapl")
mx.watts_active = lambda: False
code, res = capture(mx.cmd_watts, watts_args("on"))
check("no RAPL counter is refused", code == 1 and "RAPL" in res["reason"], res)
reset()

temp_run()
rec = stub_env()
fake_rapl()
mx.watts_active = lambda: True
pathlib.Path(eco.WATTS_FILE).write_text(json.dumps({"cpu_w": 9.0, "interval": 3, "at": 1}))
code, res = capture(mx.cmd_watts, watts_args("on", interval=3))
check("the same interval while running is a no-op",
      code == 0 and res.get("noop") is True and rec.calls == [], (res, rec.calls))
code, res = capture(mx.cmd_watts, watts_args("on", interval=5))
stops = [i for i, c in enumerate(rec.calls) if c[:2] == ["systemctl", "stop"]]
starts = [i for i, c in enumerate(rec.calls) if c[0] == "systemd-run"]
check("a new interval restarts the sampler",
      bool(stops) and bool(starts) and stops[0] < starts[0], rec.calls)
reset()

temp_run()
rec = stub_env()
sampler = {"active": True}


def rec_watts_off(cmd):
    if cmd[:2] == ["systemctl", "stop"]:
        sampler["active"] = False
    return rec(cmd)


mx.sh = rec_watts_off
mx.watts_active = lambda: sampler["active"]
code, res = capture(mx.cmd_watts, watts_args("off"))
check("watts off stops the sampler",
      code == 0 and res["ok"] is True
      and ["systemctl", "stop", mx.WATTS_UNIT] in rec.calls, (res, rec.calls))
code, res = capture(mx.cmd_watts, watts_args("off"))
check("watts off when stopped is a no-op", code == 0 and res.get("noop") is True, res)
reset()

# ---------------------------------------------------------------------------
# 31. the sampler loop belongs to systemd
# ---------------------------------------------------------------------------
print("\n\033[1m31. watts-run only under systemd\033[0m")
os.environ.pop("INVOCATION_ID", None)
code, _ = capture(mx.cmd_watts_run, argparse.Namespace(json=True, interval=3, uid=1000))
check("watts-run refuses without INVOCATION_ID", code == 2, code)

# ---------------------------------------------------------------------------
# 32. logout ends Max, whatever the user manager does
# ---------------------------------------------------------------------------
# With lingering on, user@UID.service outlives the logout, so BindsTo= never
# stops the unit; the keeper learns it from logind (a FakeSession here).
print("\n\033[1m32. logout ends Max, even with lingering on\033[0m")
k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.writes.clear()
k.session.state = False
clock.now = 1030
alive = k.tick()
check("an active tick after logout ends Max", alive is False and k.running is False)
check("... as logout or shutdown", store.last["reason"] == "logout or shutdown", store.last)
check("... releasing the hold", not ppd.held, ppd.held)
check("... writing nothing on the way out", hw.writes == [], hw.writes)
check("... leaving no state behind", store.state is None, store.state)
never_auto(hw)

k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.ac = False
clock.now = 1030
k.tick()                          # paused on battery
hw.writes.clear()
k.session.state = False
clock.now = 1060
k.tick()
check("a paused tick after logout ends Max",
      k.running is False and store.last["reason"] == "logout or shutdown", store.last)
check("... writing nothing", hw.writes == [], hw.writes)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="auto", settle=125)
k.start(pid=1)                    # silent until 1125
k.session.state = False
clock.now = 1030
k.tick()
check("a settling tick after logout ends Max without a hold",
      k.running is False and store.last["reason"] == "logout or shutdown"
      and holds(ppd) == [], (store.last, ppd.log))
check("... and passes the rest of the wait on",
      store.last["fans_armed_until"] == 1125, store.last)
never_auto(hw)

k, hw, ppd, store, clock = make(fans="boost")
k.start(pid=1)
k.session.state = False
hw.writes.clear()
clock.now = 1005
poll(k, hw, 90)
check("boost does not run the fans up for a session that has ended",
      pwm_writes(hw) == [] and k.running is False
      and store.last["reason"] == "logout or shutdown", (hw.writes, store.last))
never_auto(hw)

k, hw, ppd, store, clock = make(session=False)
check("a start after the session ended holds nothing",
      k.start(pid=1) is False and holds(ppd) == []
      and store.last["reason"] == "logout or shutdown", (ppd.log, store.last))
never_auto(hw)

k, hw, ppd, store, clock = make(session=None)
alive = k.start(pid=1)
clock.now = 1030
k.tick()
check("logind not answering (None) does not end Max",
      alive is True and k.running is True and store.state["state"] == "active",
      store.state)
never_auto(hw)

# ---------------------------------------------------------------------------
# D-Bus and GLib fakes: the real GLib for variants and errors, a bus with only
# power-profiles-daemon and logind on it, and a loop the test drives.
# ---------------------------------------------------------------------------
REAL_GIO, REAL_GLIB = ORIG["_gio"]()


class FakeBus:
    def __init__(self, profile_path=None, user_state="active"):
        self.profile_path = profile_path
        self.user_state = user_state      # logind's User.State
        self.error = None                 # raised by GetUser when set
        self.calls = []                   # (method, object path, args)
        self.subscriptions = []           # (sender, iface, member, path)
        self.handlers = {}                # member -> callback

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.subscriptions.append((sender, iface, member, path))
        self.handlers[member] = callback
        return len(self.subscriptions)

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        args = params.unpack() if params is not None else ()
        self.calls.append((method, path, args))
        v = REAL_GLIB.Variant
        if method == "HoldProfile":
            pathlib.Path(self.profile_path).write_text(args[0])   # the daemon switches
            return v("(u)", (7,))
        if method == "ReleaseProfile":
            pathlib.Path(self.profile_path).write_text("balanced")
            return None
        if method == "GetUser":
            if self.error:
                raise self.error
            return v("(o)", (f"/org/freedesktop/login1/user/_{args[0]}",))
        if method == "Get":
            return v("(v)", (v("s", self.user_state),))
        raise AssertionError(f"unexpected D-Bus call {method}")

    def asked_uids(self):
        return {args[0] for method, _, args in self.calls if method == "GetUser"}


class FakeGio:
    def __init__(self, bus):
        self.bus = bus

    def bus_get_sync(self, bus_type, cancellable):
        return self.bus

    def __getattr__(self, name):
        return getattr(REAL_GIO, name)


class FakeLoop:
    def __init__(self, drive):
        self.drive = drive
        self.quit_called = False

    def run(self):
        self.drive()

    def quit(self):
        self.quit_called = True


class FakeGLib:
    def __init__(self, drive):
        self.loop = FakeLoop(drive)
        self.timeouts = {}                # seconds -> callback

    def MainLoop(self):
        return self.loop

    def timeout_add_seconds(self, seconds, callback):
        self.timeouts[seconds] = callback
        return len(self.timeouts)

    def __getattr__(self, name):
        return getattr(REAL_GLIB, name)


def dbus_error(name):
    return REAL_GLIB.Error.new_literal(
        REAL_GIO.io_error_quark(), f"GDBus.Error:{name}: test",
        REAL_GIO.IOErrorEnum.DBUS_ERROR)


# ---------------------------------------------------------------------------
# 33. asking logind
# ---------------------------------------------------------------------------
print("\n\033[1m33. asking logind about the session\033[0m")
for state in ("active", "online"):
    check(f"{state}: logged in", mx.LogindSession(FakeBus(user_state=state), 1000).alive() is True)
for state in ("lingering", "closing", "offline"):
    check(f"{state}: logged out",
          mx.LogindSession(FakeBus(user_state=state), 1000).alive() is False)

bus = FakeBus()
mx.LogindSession(bus, 1000).alive()
check("the user is looked up by uid, then asked for its state",
      bus.calls == [("GetUser", "/org/freedesktop/login1", (1000,)),
                    ("Get", "/org/freedesktop/login1/user/_1000",
                     ("org.freedesktop.login1.User", "State"))], bus.calls)

bus = FakeBus()
bus.error = dbus_error("org.freedesktop.login1.NoSuchUser")
check("no such user: logged out, and not lingering either",
      mx.LogindSession(bus, 1000).alive() is False)

old_err, sys.stderr = sys.stderr, io.StringIO()
try:
    bus = FakeBus()
    bus.error = dbus_error("org.freedesktop.DBus.Error.NoReply")
    probe = mx.LogindSession(bus, 1000)
    answers = [probe.alive(), probe.alive()]
    bus.error = RuntimeError("the bus went away")
    answers.append(probe.alive())
    logged = sys.stderr.getvalue()
    sys.stderr = io.StringIO()
    opening = mx.LogindSession(FakeBus(user_state="opening"), 1000).alive()
    logged_state = sys.stderr.getvalue()
finally:
    sys.stderr = old_err
check("any other failure is unknown (None), so Max carries on",
      answers == [None, None, None], answers)
check("... and it is logged once", logged.count("ecomode-max:") == 1, logged)
check("a state logind may add later is unknown too, and logged",
      opening is None and "'opening'" in logged_state, (opening, logged_state))

# ---------------------------------------------------------------------------
# 34. run and watts-run belong to one user
# ---------------------------------------------------------------------------
print("\n\033[1m34. run and watts-run take the user's uid\033[0m")


def parsed(*argv):
    """ecomode-max's own parser on argv, or None when it refuses them."""
    old_err, sys.stderr = sys.stderr, io.StringIO()   # argparse explains there
    try:
        return mx.build_parser().parse_args(list(argv))
    except SystemExit:
        return None
    finally:
        sys.stderr = old_err


for cmd in ("run", "watts-run"):
    got = parsed(cmd, "--uid", "1000")
    check(f"{cmd} takes --uid 1000", got is not None and got.uid == 1000, got)
    check(f"{cmd} without --uid is refused", parsed(cmd) is None)
    for bad in ("0", "-5", "abc", "1.5", "4294967295"):
        check(f"{cmd} refuses --uid {bad}", parsed(cmd, "--uid", bad) is None)

# ---------------------------------------------------------------------------
# 35. run: the keep-alive loop ends with the session
# ---------------------------------------------------------------------------
print("\n\033[1m35. run: the loop ends with the session\033[0m")
temp_run()
profile = TMP / "platform_profile"
profile.write_text("balanced")
eco.PLATFORM_PROFILE = str(profile)
eco.find_hp_hwmon = lambda: None          # fans auto needs no fan control
eco.on_ac = lambda: True
bus = FakeBus(str(profile))
ticks = []


def drive():
    tick = glib.timeouts[mx.KEEPALIVE_SECS]
    ticks.append(tick())                  # logged in
    bus.user_state = "lingering"          # logged out; user@1000 lingers on
    ticks.append(tick())


glib = FakeGLib(drive)
mx._gio = lambda: (FakeGio(bus), glib)
mx._signal_add = lambda: (lambda priority, signum, handler: None)
os.environ["INVOCATION_ID"] = "test"
code = run_code(fans="auto", uid=1000)
os.environ.pop("INVOCATION_ID")
last = eco.read_json(eco.MAX_LAST_FILE) or {}
check("logind is asked about the user who started Max",
      bus.asked_uids() == {1000}, bus.calls)
check("the keep-alive goes on while they are logged in", ticks[:1] == [True], ticks)
check("... and stops at the first round after they log out",
      ticks[1:] == [False] and glib.loop.quit_called, ticks)
check("... as logout or shutdown",
      code == 0 and last.get("reason") == "logout or shutdown", (code, last))
check("... releasing the hold",
      any(c[0] == "ReleaseProfile" for c in bus.calls), bus.calls)
check("... and leaving no state behind", not pathlib.Path(eco.MAX_STATE_FILE).exists())
reset()

# ---------------------------------------------------------------------------
# 36. watts-run: the sampler ends with the session
# ---------------------------------------------------------------------------
print("\n\033[1m36. watts-run: the sampler ends with the session\033[0m")


class FakeTime:
    """time as the sampler uses it. Each sleep is one round."""
    CLOCK_BOOTTIME = time.CLOCK_BOOTTIME

    def __init__(self):
        self.now = 100.0
        self.rounds = 0

    def clock_gettime(self, _clock):
        return self.now

    def time(self):
        return 1790000000.0 + self.now

    def sleep(self, seconds):
        self.rounds += 1
        if self.rounds > 20:
            raise RuntimeError("the sampler never stopped")
        self.now += seconds


class FakeLogind:
    """logind's per-user state as sd_uid_get_state() reads it, stubbed in as
    mx.uid_state_native: one scripted answer per question, the last one
    repeating."""

    def __init__(self, *states):
        self.states = list(states)
        self.asked = []

    def state_of(self, uid):
        self.asked.append(uid)
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def run_sampler(*states):
    """cmd_watts_run against scripted logind answers. PyGObject is rigged to
    fail: the sampler must not need it. Returns (exit code, rounds, uids
    asked)."""
    fake_rapl()
    logind, fake_time = FakeLogind(*states), FakeTime()
    mx.time = fake_time
    mx.uid_state_native = logind.state_of
    mx._gio = no_dbus
    handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    os.environ["INVOCATION_ID"] = "test"
    try:
        code, _ = capture(mx.cmd_watts_run,
                          argparse.Namespace(json=True, interval=3, uid=1000))
    except ReachedDbus:
        code = "went on to D-Bus"
    except RuntimeError as exc:
        code = str(exc)
    finally:
        os.environ.pop("INVOCATION_ID")
        signal.signal(signal.SIGTERM, handlers[0])
        signal.signal(signal.SIGINT, handlers[1])
    return code, fake_time.rounds, logind.asked


temp_run()
code, rounds, asked = run_sampler("active", "active", "closing")
check("the sampler asks logind about its user every round", asked == [1000] * 3, asked)
check("the sampler stops at the first round after logout",
      code == 0 and rounds == 2, (code, rounds))
check("... and takes its file with it", not pathlib.Path(eco.WATTS_FILE).exists())
reset()

# ---------------------------------------------------------------------------
# 37. one privileged command at a time
# ---------------------------------------------------------------------------
print("\n\033[1m37. on, off and watts hold the state lock throughout\033[0m")
temp_run()
real_lock = mx.state_lock
lock_file = pathlib.Path(eco.STATE_DIR) / "lock"
with real_lock():
    mode = lock_file.stat().st_mode & 0o777
    other = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = False
    except BlockingIOError:
        held = True
try:
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    released = True
except BlockingIOError:
    released = False
os.close(other)
check("the lock file is root's alone (0600)", mode == 0o600, oct(mode))
check("... held exclusively while a command runs", held)
check("... and let go when it ends", released)
reset()

events = []


@contextlib.contextmanager
def recorded_lock():
    events.append("lock")
    try:
        yield
    finally:
        events.append("unlock")


def watts_idle():
    fake_rapl()
    mx.watts_active = lambda: False


for label, fn, a, setup in (
        ("on", mx.cmd_on, cli_args(), lambda: None),
        ("off", mx.cmd_off, cli_args(), lambda: setattr(mx, "unit_active", lambda: True)),
        ("watts on", mx.cmd_watts, watts_args("on"), watts_idle),
        ("watts off", mx.cmd_watts, watts_args("off"),
         lambda: setattr(mx, "watts_active", lambda: True))):
    temp_run()
    rec = stub_env()
    setup()
    events.clear()
    mx.sh = lambda cmd, rec=rec: (events.append(" ".join(cmd[:2])), rec(cmd))[1]
    mx.state_lock = recorded_lock
    capture(fn, a)
    check(f"{label}: the lock is taken first and let go last",
          events[:1] == ["lock"] and events[-1:] == ["unlock"] and len(events) > 2,
          events)
    mx.state_lock = real_lock
    reset()

# ---------------------------------------------------------------------------
# run's GLib glue, driven through the fakes above
# ---------------------------------------------------------------------------


def like_glib(callback, *args):
    """GLib prints an exception raised in a callback and goes on; a timeout
    whose callback raised is dropped as if it had returned False."""
    try:
        return callback(*args)
    except Exception:
        traceback.print_exc()
        return "raised"


def run_loop(drive, fans="boost", keeper=None):
    """cmd_run against the fakes. drive(bus, glib, signals) plays the main
    loop. Returns (exit code, bus, glib, stderr)."""
    temp_run()
    profile = TMP / "platform_profile"
    profile.write_text("balanced")
    eco.PLATFORM_PROFILE = str(profile)
    hwmon = TMP / "hwmon"
    hwmon.mkdir()
    (hwmon / "pwm1_enable").write_text("2")
    eco.find_hp_hwmon = lambda: str(hwmon)
    eco.on_ac = lambda: True
    eco.pkg_temp_c = lambda: 60
    bus, signals = FakeBus(str(profile)), []
    glib = FakeGLib(lambda: drive(bus, glib, signals))
    mx._gio = lambda: (FakeGio(bus), glib)
    mx._signal_add = lambda: (lambda prio, signum, handler: signals.append(handler))
    if keeper:
        mx.Keeper = keeper
    os.environ["INVOCATION_ID"] = "test"
    old, err = (sys.stdout, sys.stderr), io.StringIO()
    sys.stdout, sys.stderr = io.StringIO(), err
    try:
        code = mx.cmd_run(cli_args(fans=fans))
    except Exception as exc:              # escaped the loop: shown as a failure
        code = f"raised {exc!r}"
    finally:
        sys.stdout, sys.stderr = old
        os.environ.pop("INVOCATION_ID")
    return code, bus, glib, err.getvalue()


# ---------------------------------------------------------------------------
# 38. run listens to the daemons themselves only
# ---------------------------------------------------------------------------
print("\n\033[1m38. run takes signals from the daemons only\033[0m")
code, bus, glib, _ = run_loop(lambda bus, glib, signals: None)
senders = {member: sender for sender, _, member, _ in bus.subscriptions}
check("ProfileReleased only from power-profiles-daemon",
      senders.get("ProfileReleased") == "org.freedesktop.UPower.PowerProfiles",
      bus.subscriptions)
check("PrepareForSleep only from logind",
      senders.get("PrepareForSleep") == "org.freedesktop.login1", bus.subscriptions)
reset()

# ---------------------------------------------------------------------------
# 39. an error in a callback ends Max instead of silently killing the loop
# ---------------------------------------------------------------------------
print("\n\033[1m39. run: an error in a callback ends Max\033[0m")


def exploding(**from_call):
    """A Keeper whose named methods raise, each from its n-th call on."""
    calls = dict.fromkeys(from_call, 0)

    def make(name):
        real = getattr(ORIG["Keeper"], name)

        def boom(self, *args):
            calls[name] += 1
            if calls[name] >= from_call[name]:
                raise RuntimeError(f"boom in {name}")
            return real(self, *args)
        return boom

    return type("Exploding", (ORIG["Keeper"],), {n: make(n) for n in from_call})


def fire(which, fired):
    """A drive that sets off one of run's callbacks, the way GLib would."""
    def drive(bus, glib, signals):
        v = REAL_GLIB.Variant
        if which == "tick":
            fired["ret"] = like_glib(glib.timeouts[mx.KEEPALIVE_SECS])
        elif which == "temp":
            fired["ret"] = like_glib(glib.timeouts[mx.TEMP_POLL_SECS])
        elif which == "released":
            fired["ret"] = like_glib(bus.handlers["ProfileReleased"], None, ":1.9",
                                     mx.PPD_PATH, mx.PPD_IFACE, "ProfileReleased",
                                     v("(u)", (99,)))
        elif which == "sleep":
            fired["ret"] = like_glib(bus.handlers["PrepareForSleep"], None, ":1.2",
                                     mx.LOGIND_PATH, mx.LOGIND_IFACE, "PrepareForSleep",
                                     v("(b)", (False,)))
        else:
            fired["ret"] = like_glib(signals[0])
    return drive


for which, method, nth in (("tick", "tick", 2), ("temp", "on_temp", 1),
                           ("released", "on_released", 1), ("sleep", "on_resume", 1),
                           ("signal", "on_terminate", 1)):
    fired = {}
    code, bus, glib, err = run_loop(fire(which, fired), keeper=exploding(**{method: nth}))
    last = eco.read_json(eco.MAX_LAST_FILE) or {}
    check(f"{which}: Max ends with the error as its reason",
          last.get("reason") == f"internal error: boom in {method}", last)
    check(f"{which}: ... the hold is released and the loop quits",
          any(c[0] == "ReleaseProfile" for c in bus.calls) and glib.loop.quit_called,
          (bus.calls, glib.loop.quit_called))
    check(f"{which}: ... the traceback reaches the journal, the exit code says so",
          "Traceback" in err and f"boom in {method}" in err and code == 1, (code, err))
    if which in ("tick", "temp", "signal"):
        check(f"{which}: ... and the GLib source ends quietly (False)",
              fired.get("ret") is False, fired)
    reset()

fired = {}
code, bus, glib, err = run_loop(fire("tick", fired), keeper=exploding(tick=2, stop=1))
check("a stop that fails too still quits the loop, with both errors logged",
      fired.get("ret") is False and glib.loop.quit_called and code == 1
      and "boom in tick" in err and "boom in stop" in err, (fired, code, err))
reset()

# ---------------------------------------------------------------------------
# 40. the state directory stays readable whatever the umask
# ---------------------------------------------------------------------------
print("\n\033[1m40. /run/ecomode is 0755 whoever creates it\033[0m")


def state_dir_mode(create):
    shutil.rmtree(eco.STATE_DIR, ignore_errors=True)
    old_mask = os.umask(0o077)
    try:
        capture(lambda _a: create(), None)
    finally:
        os.umask(old_mask)
    return stat.S_IMODE(os.stat(eco.STATE_DIR).st_mode)


for label, create in (("the keeper's state file", lambda: mx.FileStore().save({"pid": 1})),
                      ("the stop reason", lambda: mx.stop_unit("off")),
                      ("the lock", lambda: mx.state_lock().__enter__()),
                      ("on", lambda: mx.cmd_on(cli_args())),
                      ("watts on", lambda: mx.cmd_watts(watts_args("on")))):
    temp_run()
    stub_env()
    fake_rapl()
    mx.watts_active = lambda: False
    mode = state_dir_mode(create)
    check(f"{label}: created 0755 under umask 077", mode == 0o755, oct(mode))
    reset()

temp_run()
os.chmod(eco.STATE_DIR, 0o700)            # left so by an earlier version
mx.FileStore().save({"pid": 1})
check("a directory left 0700 is opened up again",
      stat.S_IMODE(os.stat(eco.STATE_DIR).st_mode) == 0o755)
reset()

# ---------------------------------------------------------------------------
# 41. refusals and edges the earlier sections left out
# ---------------------------------------------------------------------------
print("\n\033[1m41. more refusals and edges\033[0m")
temp_run()
rec = stub_env()
mx.user_manager_active = lambda uid: False
code, res = capture(mx.cmd_on, cli_args())
check("on without a running user manager is refused",
      code == 1 and res["reason"] == "no running session for uid 1000"
      and started(rec) == [], (res, rec.calls))
reset()

temp_run()
rec = stub_env()
fake_rapl()
mx.watts_active = lambda: False
mx.user_manager_active = lambda uid: False
code, res = capture(mx.cmd_watts, watts_args("on"))
check("watts on without a running user manager is refused",
      code == 1 and res["reason"] == "no running session for uid 1000"
      and started(rec) == [], (res, rec.calls))
reset()

temp_run()
rec = stub_env()
fake_rapl()
mx.watts_active = lambda: False
mx.invoking_uid = lambda: None
code, res = capture(mx.cmd_watts, watts_args("on"))
check("watts on with no PKEXEC_UID/SUDO_UID is refused",
      code == 1 and "pkexec" in res["reason"] and started(rec) == [], (res, rec.calls))
reset()

m = mx.WattMeter(range_uj=262143328850, max_gap=9)
m.feed(5_000_000_000, 1.0)
check("a counter reset folded over the range (far above 1000 W) is dropped",
      m.feed(1_000_000, 2.0) is None)

k, hw, ppd, store, clock = make()
k.start(pid=1)
hw.ac = False
clock.now = 1030
k.tick()                          # paused; the daemon goes back to "balanced"
hw.profile = None                 # ... and then platform_profile cannot be read
hw.ac = True
clock.now = 1060
k.tick()
check("a profile that cannot be read after a pause ends Max as profile changed",
      k.running is False and store.last["reason"] == "profile changed"
      and len(holds(ppd)) == 1, (store.last, ppd.log))
never_auto(hw)

old_err, sys.stderr = sys.stderr, io.StringIO()
os.environ.pop("INVOCATION_ID", None)
mx.from_systemd()
refusal, sys.stderr = sys.stderr.getvalue(), old_err
check("the systemd-only refusal names run, cleanup and watts-run",
      all(f"'{c}'" in refusal for c in ("run", "cleanup", "watts-run")), refusal)

# ---------------------------------------------------------------------------
# 42. off with a stale state file
# ---------------------------------------------------------------------------
print("\n\033[1m42. off when the unit is gone but max.json is not\033[0m")
temp_run()
rec = stub_env()
pathlib.Path(eco.MAX_STATE_FILE).write_text(
    json.dumps({"pid": os.getpid(), "state": "active", "fans": "auto"}))   # a reused pid
code, res = capture(mx.cmd_off, cli_args())
check("off clears the stale state and reports ok",
      code == 0 and res["ok"] is True and not pathlib.Path(eco.MAX_STATE_FILE).exists()
      and not pathlib.Path(eco.MAX_STOP_FLAG).exists(), (code, res))
reset()

temp_run()
rec = stub_env()
mx.unit_active = lambda: True             # a unit that will not stop
code, res = capture(mx.cmd_off, cli_args())
check("a unit that does not stop is still reported",
      code == 1 and res["msg"] == "service did not stop", (code, res))
reset()

# ---------------------------------------------------------------------------
# 43. the sampler takes its temp file along; no root-owned bytecode
# ---------------------------------------------------------------------------
print("\n\033[1m43. no leftovers\033[0m")
temp_run()
pathlib.Path(eco.WATTS_FILE + ".tmp").write_text("{")   # a write cut short
code, _, _ = run_sampler("active", "offline")
check("the sampler removes its half-written temp file too",
      code == 0 and not pathlib.Path(eco.WATTS_FILE + ".tmp").exists(), code)
reset()

temp_run()
for name in ("ecomode", "ecomode-max"):
    shutil.copy(HERE.parent / "backend" / name, TMP / name)
env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
res = subprocess.run([sys.executable, str(TMP / "ecomode-max"), "--help"],
                     capture_output=True, text=True, env=env)
check("loading ecomode writes no __pycache__ next to the helpers",
      res.returncode == 0 and not (TMP / "__pycache__").exists(),
      (res.returncode, sorted(p.name for p in TMP.iterdir())))
reset()

# ---------------------------------------------------------------------------
# 44. the sampler asks logind through libsystemd, not PyGObject
# ---------------------------------------------------------------------------
print("\n\033[1m44. the sampler's session check: sd_uid_get_state\033[0m")
for state, want in (("active", True), ("online", True), ("lingering", False),
                    ("closing", False), ("offline", False)):
    got = mx.SdLoginSession(1000, read_state=lambda uid, s=state: s).alive()
    check(f"{state}: {'logged in' if want else 'logged out'}", got is want, got)

asked = []
mx.SdLoginSession(1000, read_state=lambda uid: asked.append(uid) or "active").alive()
check("the reader is asked about the session's own uid", asked == [1000], asked)


def failing_reader(uid):
    raise OSError("libsystemd went away")


old_err, sys.stderr = sys.stderr, io.StringIO()
try:
    probe = mx.SdLoginSession(1000, read_state=lambda uid: None)
    answers = [probe.alive(), probe.alive()]
    logged = sys.stderr.getvalue()
    sys.stderr = io.StringIO()
    others = [mx.SdLoginSession(1000, read_state=lambda uid: "opening").alive(),
              mx.SdLoginSession(1000, read_state=failing_reader).alive()]
    logged_others = sys.stderr.getvalue()
finally:
    sys.stderr = old_err
check("no answer (None) is unknown, so the sampler carries on",
      answers == [None, None], answers)
check("... and it is logged once", logged.count("ecomode-max:") == 1, logged)
check("a word logind may add later, or a failing reader, is unknown too",
      others == [None, None] and logged_others.count("ecomode-max:") == 2,
      (others, logged_others))

# uid_state_native against a fake libsystemd: C functions made with ctypes,
# so the pointer handed out and the one freed can be compared.
libc = ctypes.CDLL(None)
libc.strdup.restype = ctypes.c_void_p
libc.strdup.argtypes = [ctypes.c_char_p]
libc.free.argtypes = [ctypes.c_void_p]
handed, freed, stray = [], [], []


@ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
def fake_get_state(uid, out):
    if uid == 13:
        # A failure that still leaves something in `out`: only the return
        # value may decide.
        out[0] = libc.strdup(b"active")
        stray.append(out[0])
        return -5                         # -EIO
    out[0] = libc.strdup(b"lingering")
    handed.append(out[0])
    return 0


@ctypes.CFUNCTYPE(None, ctypes.c_void_p)
def fake_free(pointer):
    freed.append(pointer)
    libc.free(pointer)


mx._sd_login = (fake_get_state, fake_free)
got = mx.uid_state_native(1000)
check("the state comes back as text", got == "lingering", got)
check("... and the string libsystemd handed out is freed, once",
      len(handed) == 1 and freed == handed, (handed, freed))
handed.clear()
freed.clear()
got = mx.uid_state_native(13)
check("a failed call (r < 0) gives None, whatever it left behind",
      got is None and handed == [] and freed == [], (got, freed))
for pointer in stray:
    if pointer not in freed:              # the fake's leftover, not ours to free
        libc.free(pointer)

loads, real_cdll = [], ctypes.CDLL
ctypes.CDLL = lambda name, *a, **kw: (loads.append(name), real_cdll(name, *a, **kw))[1]
real_lib = mx.SD_LOGIN_LIB
try:
    mx._sd_login, mx.SD_LOGIN_LIB = None, "libecomode-test-missing.so.0"
    missing = [mx.uid_state_native(1000), mx.uid_state_native(1000)]
    mx._sd_login, mx.SD_LOGIN_LIB = None, real_lib
    unknown = mx.uid_state_native(4294967294)
    own = mx.uid_state_native(os.getuid())
finally:
    ctypes.CDLL = real_cdll
    mx.SD_LOGIN_LIB = real_lib
check("without libsystemd the answer is None", missing == [None, None], missing)
check("... and the load is tried once, not every round",
      loads.count("libecomode-test-missing.so.0") == 1, loads)
check("the real libsystemd is loaded once for several questions",
      loads.count(real_lib) == 1, loads)
check("... a uid logind never saw reads offline (None without libsystemd)",
      unknown in ("offline", None), unknown)
check("... and our own uid gets a state the keeper knows too (or None)",
      own in (*mx.USER_LOGGED_IN, None), own)

temp_run()
code, rounds, asked = run_sampler(None, "active", "lingering")
check("the sampler goes on past an unanswered question and stops at lingering",
      code == 0 and rounds == 2 and asked == [1000] * 3, (code, rounds, asked))
reset()

temp_run()
code, rounds, _ = run_sampler("offline")
check("offline from the start: not one round, and no file left",
      code == 0 and rounds == 0 and not pathlib.Path(eco.WATTS_FILE).exists(),
      (code, rounds))
reset()

# A fresh interpreter, so that nothing this test file imported counts: one
# sampler run must not load PyGObject at all.
SAMPLER_ALONE = r"""
import argparse, importlib.machinery, importlib.util, json, os, pathlib, sys, tempfile
spec = importlib.util.spec_from_loader(
    "ecomode_max", importlib.machinery.SourceFileLoader("ecomode_max", sys.argv[1]))
mx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mx)
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ecomode-sampler-"))
(tmp / "rapl").mkdir()
(tmp / "rapl" / "energy_uj").write_text("1000000")
mx.eco.RAPL = str(tmp / "rapl")
mx.eco.STATE_DIR = str(tmp / "run")
mx.eco.WATTS_FILE = str(tmp / "run" / "watts")
answers = ["active", "active", "offline"]
mx.uid_state_native = lambda uid: answers.pop(0)
reached = []
def no_gio():
    reached.append(True)
    raise RuntimeError("the sampler reached for PyGObject")
mx._gio = no_gio
class Clock:
    CLOCK_BOOTTIME = 7
    now = 100.0
    def clock_gettime(self, _clock): return self.now
    def time(self): return 1790000000.0 + self.now
    def sleep(self, seconds): self.now += seconds
mx.time = Clock()
os.environ["INVOCATION_ID"] = "test"
try:
    code = mx.cmd_watts_run(argparse.Namespace(json=True, interval=3, uid=1000))
except Exception as exc:
    code = repr(exc)
print(json.dumps({"code": code, "reached_gio": bool(reached),
                  "gi": sorted(m for m in sys.modules if m == "gi" or m.startswith("gi."))}))
"""
res = subprocess.run([sys.executable, "-B", "-c", SAMPLER_ALONE, str(HELPER)],
                     capture_output=True, text=True)
try:
    alone = json.loads(res.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    alone = {"stdout": res.stdout, "stderr": res.stderr[-400:]}
check("in a fresh interpreter a sampler run loads no PyGObject",
      alone.get("code") == 0 and alone.get("reached_gio") is False and alone.get("gi") == [],
      alone)


# ---------------------------------------------------------------------------
# 40. Dynamic Boost (nvidia-powerd) goes with Max
# ---------------------------------------------------------------------------
print("\n\033[1m40. Dynamic Boost\033[0m")


def powerd_env(active=False, loaded="loaded", start_ok=True):
    rec = stub_env()
    state = {"active": active}

    def sh(cmd):
        rec.calls.append(list(cmd))
        if cmd[:3] == ["systemctl", "is-active", "--quiet"] and cmd[3] == mx.POWERD:
            return subprocess.CompletedProcess(cmd, 0 if state["active"] else 3, "", "")
        if cmd[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(cmd, 0, loaded + "\n", "")
        if cmd[:2] == ["systemctl", "start"] and cmd[2] == mx.POWERD:
            if start_ok:
                state["active"] = True
            return subprocess.CompletedProcess(cmd, 0 if start_ok else 1, "",
                                               "" if start_ok else "Unit failed")
        if cmd[:2] == ["systemctl", "stop"] and cmd[2] == mx.POWERD:
            state["active"] = False
        return rec(cmd)

    def sh_and_keeper(cmd):
        if cmd[0] == "systemd-run":
            state["keeper"] = True
        return sh(cmd)

    mx.sh = sh_and_keeper
    # The keeper is up once its unit has been started.
    eco.max_state = lambda: ({"pid": 1, "state": "active", "fans": "auto"}
                             if state.get("keeper") else None)
    return rec, state


def powerd_calls(rec, verb):
    return [c for c in rec.calls if c[:2] == ["systemctl", verb] and c[-1] == mx.POWERD]


temp_run()
rec, pd = powerd_env()
code, res = capture(mx.cmd_on, cli_args(gpu_boost=True))
check("on --gpu-boost starts nvidia-powerd and says so",
      code == 0 and res["gpu_boost"] is True and powerd_calls(rec, "start"), (res, rec.calls))
check("... and records that Max owes a stop", os.path.exists(eco.POWERD_FLAG))
eco.max_state = lambda: {"pid": 1, "state": "active", "fans": "auto"}
code, res = capture(mx.cmd_on, cli_args(gpu_boost=False))
check("the same fans without boost stop it again, as a no-op for Max",
      res.get("noop") and powerd_calls(rec, "stop") and not os.path.exists(eco.POWERD_FLAG)
      and pd["active"] is False, (res, rec.calls))
reset()

temp_run()
rec, pd = powerd_env(active=True)
code, res = capture(mx.cmd_on, cli_args(gpu_boost=True))
check("a powerd that already runs is left alone and not claimed",
      not powerd_calls(rec, "start") and not os.path.exists(eco.POWERD_FLAG), rec.calls)
code, res = capture(mx.cmd_off, cli_args())
check("... so off does not stop it", not powerd_calls(rec, "stop") and pd["active"], rec.calls)
reset()

temp_run()
rec, pd = powerd_env(loaded="not-found")
code, res = capture(mx.cmd_on, cli_args(gpu_boost=True))
check("no nvidia-powerd: Max still starts, and says why there is no boost",
      code == 0 and res["ok"] and res["gpu_boost"] is False
      and "not installed" in res["gpu_boost_error"], res)
reset()

temp_run()
rec, pd = powerd_env(start_ok=False)
code, res = capture(mx.cmd_on, cli_args(gpu_boost=True))
check("a failed start is reported and claims nothing",
      code == 0 and "Unit failed" in res["gpu_boost_error"]
      and not os.path.exists(eco.POWERD_FLAG), res)
reset()

temp_run()
rec, pd = powerd_env()
capture(mx.cmd_on, cli_args())
check("without --gpu-boost nothing is started", not powerd_calls(rec, "start"), rec.calls)
reset()

temp_run()
rec, pd = powerd_env()
real_start = mx.start_powerd


def start_then_keeper_dies():
    err = real_start()
    pd["keeper"] = False                  # profile picked as Max started
    return err


mx.start_powerd = start_then_keeper_dies
code, res = capture(mx.cmd_on, cli_args(gpu_boost=True))
mx.start_powerd = real_start
check("a keeper gone by the time powerd started takes it down again",
      pd["active"] is False and not os.path.exists(eco.POWERD_FLAG), (rec.calls, pd))
reset()

temp_run()
rec, pd = powerd_env()
pathlib.Path(eco.POWERD_FLAG).write_text("max")
pd["active"] = True
code, res = capture(mx.cmd_off, cli_args())
check("off with no Max still stops a powerd Max left behind",
      res.get("noop") and pd["active"] is False and not os.path.exists(eco.POWERD_FLAG),
      (res, rec.calls))
reset()

temp_run()
stub_env()
seen_env = []
mx.sh = lambda cmd: (seen_env.append(os.environ.get(eco.LOCK_HELD_ENV)),
                     subprocess.CompletedProcess(cmd, 0, '{"ok": true}', ""))[1]
eco.is_on = lambda: True
capture(mx.cmd_on, cli_args())
eco.is_on = ORIG_IS_ON
check("ecomode off, run under Max's lock, is told the lock is held",
      seen_env and seen_env[0] == "1", seen_env)
check("... and only while Max holds it", os.environ.get(eco.LOCK_HELD_ENV) is None)
reset()

temp_run()
rec, pd = powerd_env()
capture(mx.cmd_on, cli_args(gpu_boost=True))
os.environ["INVOCATION_ID"] = "test"
try:
    capture(mx.cmd_cleanup, cli_args())
finally:
    os.environ.pop("INVOCATION_ID")
check("however Max ends, its cleanup stops the powerd it started",
      powerd_calls(rec, "stop") and not os.path.exists(eco.POWERD_FLAG), rec.calls)
reset()


# ---------------------------------------------------------------------------
# 41. the sampler and the dGPU
# ---------------------------------------------------------------------------
print("\n\033[1m41. sampler: dGPU readings\033[0m")
ORIG_GPU = (eco.gpu_runtime, eco.gpu_holders, eco.gpu_query)
gstate = {"status": "suspended", "holders": [], "scans": 0, "queries": 0}


def fake_runtime():
    return {"status": gstate["status"], "suspended_ms": 1, "active_ms": 1}


def fake_holders(exclude=()):
    gstate["scans"] += 1
    return [dict({"awake": True, "follower": False}, **h)
            for h in gstate["holders"] if h["pid"] not in exclude]


def fake_query():
    gstate["queries"] += 1
    return {"power_w": 19.3, "limit_w": 80.0, "util_pct": 100, "temp_c": 59,
            "gr_mhz": 900, "mem_mhz": 8001, "pstate": "P0", "reasons": [],
            "vram_used_mib": 2100}


eco.gpu_runtime, eco.gpu_holders, eco.gpu_query = fake_runtime, fake_holders, fake_query
clock = [1000.0]
watch = mx.GpuWatch(clock=lambda: clock[0], scan_secs=30)
check("asleep: no scan, no query, just the state",
      watch.reading() == {"state": "suspended"} and gstate["scans"] == 0
      and gstate["queries"] == 0, gstate)
gstate["status"] = "active"
r = watch.reading()
check("awake with no other holder: the driver is not asked",
      r == {"state": "active", "held": False} and gstate["scans"] == 1
      and gstate["queries"] == 0, (r, gstate))
gstate["holders"] = [{"pid": 4242, "name": "blender"}]
clock[0] += 10
r = watch.reading()
check("the holder list is not walked again before 30 s", gstate["scans"] == 1
      and r["held"] is False, gstate)
clock[0] += 25
r = watch.reading()
check("after 30 s it is, and a held GPU is read",
      gstate["scans"] == 2 and r["held"] is True and r["power_w"] == 19.3
      and gstate["queries"] == 1, (r, gstate))
check("only the published keys ride along", "vram_used_mib" not in r, r)
gstate["holders"] = [{"pid": 4243, "name": "Xwayland", "awake": False},
                     {"pid": 4244, "name": "nvidia-smi", "follower": True}]
clock[0] += 31
r = watch.reading()
check("a display-only holder and KDE's sensor do not count as keeping it awake",
      r["held"] is False and gstate["queries"] == 1, (r, gstate))
gstate["holders"] = [{"pid": os.getpid(), "name": "ecomode-max"}]
gstate["status"] = "suspended"
watch.reading()
gstate["status"] = "active"
r = watch.reading()
check("after a sleep it rescans at once, and does not count itself",
      gstate["scans"] == 4 and r["held"] is False, (r, gstate))

temp_run()
rapl = TMP / "rapl"
rapl.mkdir()
(rapl / "energy_uj").write_text("1000000")
eco.RAPL = str(rapl)
eco.battery_draw_w = lambda: None
real_limit = eco.cpu_limit_now
eco.cpu_limit_now = lambda: ["PL1"]
meter = mx.WattMeter(range_uj=262143328850, max_gap=9)
mx.watts_entry(meter, 3, 100.0, 1790000000.0, watch)
(rapl / "energy_uj").write_text("106000000")
entry = mx.watts_entry(meter, 3, 103.0, 1790000003.0, watch)
check("the record carries what limits the CPU and the GPU's state",
      entry["cpu_limit"] == ["PL1"] and entry["gpu"]["state"] == "active", entry)
eco.cpu_limit_now = real_limit
eco.gpu_runtime, eco.gpu_holders, eco.gpu_query = ORIG_GPU
reset()

# ---------------------------------------------------------------------------
print("\n\033[1m45. full speed left over from an earlier session\033[0m")
# 2026-10-25: a 7 s "max" session, then Max toggled with fans "auto" five
# times; the fans stayed at full speed until 126 s after the last toggle and
# the widget did not know. The EC's own word (pwm1_enable reads 0 at full
# speed) now counts, not only what this session commanded.

keeper, hw, ppd, store, clock = make(fans="auto")
hw.ec_max = True
keeper.start(4242)
check("an auto session's hold while the EC holds full speed: recorded as re-arming it",
      store.state["fans_armed_until"] == 1000 + mx.FAN_RELEASE_SECS, store.state)
clock.now += 10
keeper.stop("off")
check("... and its release, at the end, too",
      store.last["fans_armed_until"] == 1010 + mx.FAN_RELEASE_SECS, store.last)

keeper, hw, ppd, store, clock = make(fans="auto")
hw.ec_max = False
keeper.start(4242)
clock.now += 10
keeper.stop("off")
check("the EC on its own curve: nothing recorded",
      store.last["fans_armed_until"] is None, store.last)

keeper, hw, ppd, store, clock = make(fans="auto")
keeper.start(4242)
keeper.stop("off")
check("no reading from the EC: as before, nothing assumed",
      store.last["fans_armed_until"] is None, store.last)

S = mx.settle_needed
rec = {"fans_armed_until": 1040}
check("settle: the EC at full speed beats a short record",
      S("auto", rec, 1000, True) == mx.FAN_RELEASE_SECS
      and S("boost", None, 1000, True) == mx.FAN_RELEASE_SECS)
check("settle: the EC on its curve - the record decides",
      S("auto", rec, 1000, False) == 40 and S("auto", None, 1000, False) == 0)
check("settle: no reading - the record decides", S("auto", rec, 1000, None) == 40)
check("settle: a max session never waits", S("max", rec, 1000, True) == 0)

# ---------------------------------------------------------------------------
print(f"\n\033[1m{PASS} passed, {FAIL} failed\033[0m")
sys.exit(1 if FAIL else 0)
