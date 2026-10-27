#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise apply / restore / self-test against a fake sysfs tree.

Runs entirely in a temp directory: no root, and nothing on the real system
is read or written. Every knob path in the registry is rebound to a file
under the temp root, so the same code paths the privileged helper uses are
the ones under test here.

Covers the failure modes that actually matter on this hardware:
  * firmware refusing a RAPL write (lock bit)
  * the embedded controller accepting a write then reverting it
  * restore not taking, which must be reported rather than swallowed
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
HELPER = HERE.parent / "backend" / "ecomode"

spec = importlib.util.spec_from_loader(
    "ecomode", importlib.machinery.SourceFileLoader("ecomode", str(HELPER)))
eco = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eco)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

PASS = FAIL = 0
ROOT = None


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m {label}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label}   {detail}")


def fake_sysfs(initial):
    """Rebind every knob path into a temp tree seeded with `initial`."""
    global ROOT
    ROOT = pathlib.Path(tempfile.mkdtemp(prefix="ecomode-test-"))

    eco.STATE_DIR = str(ROOT / "run")
    eco.STATE_FILE = str(ROOT / "run" / "state.json")

    for knob in eco.KNOBS:
        rel = knob["path"].lstrip("/")
        new = ROOT / "sys" / rel
        knob["path"] = str(new)
        if knob["kind"] == "glob":
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(initial.get(knob["id"], "0"))

    # Glob knobs: create two fake CPUs so "mixed" detection is exercised.
    for knob in eco.KNOBS:
        if knob["kind"] != "glob":
            continue
        for cpu in ("cpu0", "cpu1"):
            p = pathlib.Path(knob["path"].replace("cpu*", cpu))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(initial.get(knob["id"], "0"))

    # The dGPU: its runtime PM files sit next to the dgpu_pm knob's file.
    # Asleep by default, so nothing reaches nvidia-smi unless a test says so.
    dgpu_pm = next(k for k in eco.KNOBS if k["id"] == "dgpu_pm")["path"]
    eco.DGPU = str(pathlib.Path(dgpu_pm).parent.parent)
    for name, text in (("runtime_status", "suspended"),
                       ("runtime_suspended_time", "1000"),
                       ("runtime_active_time", "500")):
        (pathlib.Path(eco.DGPU) / "power" / name).write_text(text)
    # Bound to NVIDIA's driver, as here; tests of nouveau rebind it.
    (ROOT / "drivers" / "nvidia").mkdir(parents=True, exist_ok=True)
    os.symlink(ROOT / "drivers" / "nvidia", pathlib.Path(eco.DGPU) / "driver")
    eco.DRI_BY_PATH = str(ROOT / "dri")
    eco.dgpu_connectors = lambda enabled_only=False: []    # no display on the dGPU
    eco.GPU_FILE = str(ROOT / "run" / "gpu.json")
    RUNS.calls.clear()
    RUNS.replies.clear()
    RUNS.replies[("systemctl", "is-active")] = (3, "", "")    # nothing runs

    eco.MAX_STATE_FILE = str(ROOT / "run" / "max.json")
    eco.MAX_LAST_FILE = str(ROOT / "run" / "max-last.json")
    eco.MAX_STOP_FLAG = str(ROOT / "run" / "max-stop-reason")
    eco.WATTS_FILE = str(ROOT / "run" / "watts")
    (ROOT / "run").mkdir(parents=True, exist_ok=True)


class Runs:
    """Stands in for eco.run: records every command, and answers 0 unless a
    test queued another reply for a command starting with a given prefix.
    Nothing here ever reaches nvidia-smi, systemctl or systemd-run."""

    def __init__(self):
        self.calls = []
        self.replies = {}           # tuple(argv prefix) -> (code, out, err)

    def __call__(self, argv, timeout=10):
        self.calls.append(list(argv))
        for prefix, reply in self.replies.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return reply
        return 0, "", ""

    def ran(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]


RUNS = Runs()
eco.run = RUNS
# nvsmi() looks for the binary first; the fake answers either way.
eco.shutil = type("S", (), {"which": staticmethod(lambda name: "/usr/bin/" + name)})


def cleanup():
    if ROOT and ROOT.exists():
        shutil.rmtree(ROOT)


def fake_max_env(board="8BAB", profile_driver="hp-wmi",
                 choices="low-power balanced performance",
                 hwmon=True, ac="1", pwm="2", temp="70000",
                 bat_status="Full", bat_power="0"):
    """Fake the sysfs bits Max mode reads, under the current ROOT."""
    sysroot = ROOT / "sys2"

    def w(rel, text):
        p = sysroot / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    eco.BOARD_NAME = str(w("dmi/board_name", board))
    w("pp/platform-profile-0/name", profile_driver)
    w("pp/platform-profile-0/choices", choices)
    eco.PROFILE_CLASS_GLOB = str(sysroot / "pp" / "platform-profile-*")
    w("hwmon/hwmon3/name", "acpitz")
    if hwmon:
        w("hwmon/hwmon6/name", "hp")
        w("hwmon/hwmon6/fan1_input", "3700")
        w("hwmon/hwmon6/fan2_input", "4000")
        w("hwmon/hwmon6/pwm1_enable", pwm)
    eco.HWMON_GLOB = str(sysroot / "hwmon" / "hwmon*")
    w("ps/ACAD/type", "Mains")
    w("ps/ACAD/online", ac)
    w("ps/BAT1/type", "Battery")
    w("ps/BAT1/status", bat_status)
    w("ps/BAT1/power_now", bat_power)
    eco.POWER_SUPPLY_GLOB = str(sysroot / "ps" / "*")
    w("th/thermal_zone0/type", "acpitz")
    w("th/thermal_zone0/temp", "27000")
    w("th/thermal_zone5/type", "x86_pkg_temp")
    w("th/thermal_zone5/temp", temp)
    eco.THERMAL_GLOB = str(sysroot / "th" / "thermal_zone*")
    eco.PLATFORM_PROFILE = next(
        k for k in eco.KNOBS if k["id"] == "platform_profile")["path"]
    return sysroot


def write_max_state(**fields):
    pathlib.Path(eco.MAX_STATE_FILE).write_text(json.dumps(fields))


def args(**kw):
    base = dict(json=True, level="gentle", max_pct=40, deep=False, cmd=None)
    base.update(kw)
    return argparse.Namespace(**base)


def capture(fn, a):
    """Run a command, returning the dict it emitted."""
    import io
    import json as _json
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        fn(a)
    finally:
        sys.stdout = old
    return _json.loads(buf.getvalue())


def capture_text(fn, a):
    """Run a command, returning what it printed to stdout and stderr."""
    import io
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    olderr, sys.stderr = sys.stderr, buf
    try:
        fn(a)
    finally:
        sys.stdout, sys.stderr = old, olderr
    return buf.getvalue()


def val(knob_id):
    knob = next(k for k in eco.KNOBS if k["id"] == knob_id)
    return eco.read_knob(knob)


BASE = {
    "rapl_pl1": "55000000",
    "rapl_pl2": "130000000",
    "max_pct": "100",
    "dgpu_pm": "auto",
    "epp": "power",
    "governor": "powersave",
    "platform_profile": "low-power",
}


# ---------------------------------------------------------------------------
# 1. gentle apply + restore round trip
# ---------------------------------------------------------------------------
print("\n\033[1m1. gentle: apply, verify, restore\033[0m")
fake_sysfs(BASE)

res = capture(eco.cmd_on, args(level="gentle"))
check("apply reports ok", res["ok"], res)
check("PL1 lowered 55W -> 35W", val("rapl_pl1") == "35000000", val("rapl_pl1"))
check("PL2 untouched at gentle", val("rapl_pl2") == "130000000", val("rapl_pl2"))
check("CPU ceiling untouched at gentle", val("max_pct") == "100", val("max_pct"))
check("self-test ran real checks", len(res["selftest"]["checks"]) > 0,
      res["selftest"])
check("self-test passed", res["selftest"]["pass"] is True)
check("PPD-owned knobs never written",
      val("epp") == "power" and val("platform_profile") == "low-power")

st = capture(eco.cmd_selftest, args())
check("standalone selftest is applicable while on", st.get("applicable") is True)

res = capture(eco.cmd_off, args())
check("restore reports ok", res["ok"], res)
check("PL1 restored to exactly 55W", val("rapl_pl1") == "55000000", val("rapl_pl1"))
check("state file cleared", eco.load_state() is None)

st = capture(eco.cmd_selftest, args())
check("selftest while off is NOT a bare pass",
      st.get("applicable") is False and st.get("pass") is None, st)
cleanup()


# ---------------------------------------------------------------------------
# 2. deep applies the extra knobs
# ---------------------------------------------------------------------------
print("\n\033[1m2. deep: all three knobs move\033[0m")
fake_sysfs(BASE)
res = capture(eco.cmd_on, args(level="deep", max_pct=40))
check("apply reports ok", res["ok"], res)
check("PL1 -> 25W", val("rapl_pl1") == "25000000", val("rapl_pl1"))
check("PL2 -> 35W", val("rapl_pl2") == "35000000", val("rapl_pl2"))
check("ceiling -> 40%", val("max_pct") == "40", val("max_pct"))
check("3 CPU knobs checked", len(res["selftest"]["checks"]) >= 3)

capture(eco.cmd_off, args())
check("all three restored",
      (val("rapl_pl1"), val("rapl_pl2"), val("max_pct"))
      == ("55000000", "130000000", "100"))
cleanup()


# ---------------------------------------------------------------------------
# 3. firmware lock bit: the write is rejected outright
# ---------------------------------------------------------------------------
print("\n\033[1m3. RAPL lock bit: write rejected\033[0m")
fake_sysfs(BASE)
real_write = eco.write_path


def locked_write(path, value):
    if "constraint_0" in path:
        return False, "[Errno 5] Input/output error"
    return real_write(path, value)


eco.write_path = locked_write
res = capture(eco.cmd_on, args(level="gentle"))
eco.write_path = real_write

check("apply reports NOT ok", res["ok"] is False, res)
check("failure names the locked knob",
      any(f["id"] == "rapl_pl1" for f in res["failed"]), res["failed"])
check("PL1 really unchanged", val("rapl_pl1") == "55000000", val("rapl_pl1"))
cleanup()


# ---------------------------------------------------------------------------
# 4. embedded controller reverts the value a moment later
# ---------------------------------------------------------------------------
print("\n\033[1m4. EC rewrites the limit back after the write\033[0m")
fake_sysfs(BASE)
real_read_knob = eco.read_knob
seen = {"n": 0}


def flaky_read(knob):
    # First read after the write looks correct; the second - the one the
    # comparator takes two seconds later - shows the firmware's value.
    if knob["id"] == "rapl_pl1":
        seen["n"] += 1
        if seen["n"] > 1:
            return "55000000"
    return real_read_knob(knob)


eco.read_knob = flaky_read
real_sleep = eco.time.sleep
eco.time.sleep = lambda s: None          # keep the suite fast
res = capture(eco.cmd_on, args(level="gentle"))
eco.read_knob = real_read_knob
eco.time.sleep = real_sleep

pl1 = next(c for c in res["selftest"]["checks"] if c["id"] == "rapl_pl1")
check("double-read catches the revert", pl1["status"] == "fail", pl1)
check("note explains firmware rewrote it",
      pl1["note"] and "rewrote" in pl1["note"], pl1.get("note"))
check("overall apply reports NOT ok", res["ok"] is False)
cleanup()


# ---------------------------------------------------------------------------
# 5. restore that does not take must be reported, not swallowed
# ---------------------------------------------------------------------------
print("\n\033[1m5. restore fails -> reported, state kept\033[0m")
fake_sysfs(BASE)
capture(eco.cmd_on, args(level="gentle"))


def refuse_restore(path, value):
    if "constraint_0" in path and value == "55000000":
        return True, None        # claims success, writes nothing
    return real_write(path, value)


eco.write_path = refuse_restore
res = capture(eco.cmd_off, args())
eco.write_path = real_write

check("restore reports NOT ok", res["ok"] is False, res)
check("mismatch is itemised",
      any(c["status"] == "fail" for c in res["selftest"]["checks"]),
      res["selftest"]["checks"])
check("state file KEPT so the knob is not orphaned",
      eco.load_state() is not None)
cleanup()


# ---------------------------------------------------------------------------
# 6. comparator semantics
# ---------------------------------------------------------------------------
print("\n\033[1m6. comparators\033[0m")
fake_sysfs(BASE)
k_pl1 = next(k for k in eco.KNOBS if k["id"] == "rapl_pl1")
k_max = next(k for k in eco.KNOBS if k["id"] == "max_pct")

eco.time.sleep = lambda s: None
check("unreadable knob -> skipped, not fail",
      eco.compare_knob(k_max, "40", None)[0] == "skipped")
check("exact mismatch -> fail",
      eco.compare_knob(k_max, "40", "100")[0] == "fail")
check("exact match -> pass",
      eco.compare_knob(k_max, "40", "40")[0] == "pass")
check("rapl mismatch -> fail (lock bit)",
      eco.compare_knob(k_pl1, "35000000", "55000000")[0] == "fail")
eco.time.sleep = real_sleep
cleanup()


# ---------------------------------------------------------------------------
# 7. power sampling: slope of the energy counter
# ---------------------------------------------------------------------------
print("\n\033[1m7. package power measurement\033[0m")


class FakeClock:
    """Drives sample_power deterministically: sleep(n) advances the clock."""
    def __init__(self):
        self.now = 100.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now

    def time(self):
        return int(self.now)


def with_energy(readings, rng="262143328850"):
    it = iter(readings)
    clock = FakeClock()
    eco.time = clock

    def fake_read(path):
        if path.endswith("max_energy_range_uj"):
            return rng
        if path.endswith("energy_uj"):
            return next(it)
        return None

    eco.read_path = fake_read
    return clock


real_read_path = eco.read_path
real_time = eco.time

# 2 J over 0.5 s = 4 W
with_energy(["1000000", "3000000"])
check("computes 4 W from a 2 J / 0.5 s slope",
      eco.sample_power(0.5) == 4.0, eco.sample_power)

# Counter wraps: reading goes backwards, must fold over max_energy_range_uj
with_energy(["262143328000", "1000850"], rng="262143328850")
w = eco.sample_power(0.5)
check("handles counter wraparound", w is not None and w > 0, w)

# Unreadable (the unprivileged case)
eco.read_path = lambda p: None
check("unreadable energy_uj -> None, not a crash",
      eco.sample_power(0.1) is None)

# Garbage in sysfs must not raise
with_energy(["not-a-number", "123"])
check("non-numeric counter -> None", eco.sample_power(0.1) is None)

eco.read_path = real_read_path
eco.time = real_time

# before/after is recorded into the state file at apply time
fake_sysfs(BASE)
eco.time = FakeClock()
energy = iter(["1000000", "6000000", "10000000", "13000000"])
eco.read_path = (lambda real: lambda p: next(energy)
                 if p.endswith("energy_uj") else real(p))(real_read_path)
res = capture(eco.cmd_on, args(level="gentle"))
eco.read_path = real_read_path
eco.time = real_time

check("apply records power_before", res.get("power_before") is not None, res)
check("apply records power_after", res.get("power_after") is not None, res)
saved = eco.load_state()
check("both readings persisted to state",
      saved.get("power_before") is not None and saved.get("power_after") is not None,
      saved)
cleanup()


# ---------------------------------------------------------------------------
# 8. switching level while on must not poison the restore values
# ---------------------------------------------------------------------------
print("\n\033[1m8. gentle -> deep -> off restores the ORIGINAL values\033[0m")
fake_sysfs(BASE)

capture(eco.cmd_on, args(level="gentle"))
check("gentle applied", val("rapl_pl1") == "35000000", val("rapl_pl1"))

res = capture(eco.cmd_on, args(level="deep", max_pct=40))
check("switching to deep is not a no-op", not res.get("noop"), res)
check("switch reported", res.get("switched") is True, res)
check("deep PL1 applied", val("rapl_pl1") == "25000000", val("rapl_pl1"))
check("deep PL2 applied", val("rapl_pl2") == "35000000", val("rapl_pl2"))
check("deep ceiling applied", val("max_pct") == "40", val("max_pct"))

saved = eco.load_state()
check("state recorded the TRUE original PL1, not gentle's 35W",
      saved["knobs"]["rapl_pl1"][
          next(iter(saved["knobs"]["rapl_pl1"]))] == "55000000",
      saved["knobs"]["rapl_pl1"])

res = capture(eco.cmd_off, args())
check("restore ok", res["ok"], res)
check("PL1 back to the original 55W (not 35W)",
      val("rapl_pl1") == "55000000", val("rapl_pl1"))
check("PL2 back to original", val("rapl_pl2") == "130000000", val("rapl_pl2"))
check("ceiling back to original", val("max_pct") == "100", val("max_pct"))

# same level twice really is a no-op
capture(eco.cmd_on, args(level="gentle"))
res = capture(eco.cmd_on, args(level="gentle"))
check("re-applying the same level is a no-op", res.get("noop") is True, res)
capture(eco.cmd_off, args())
cleanup()


# ---------------------------------------------------------------------------
# 9. status reports the power mode
# ---------------------------------------------------------------------------
print("\n\033[1m9. status reports the power mode\033[0m")
fake_sysfs(BASE)
fake_max_env()

st = capture(eco.cmd_status, args())
check("mode is normal with nothing applied", st["mode"] == "normal", st.get("mode"))
check("8BAB with hp-wmi performance is supported",
      st["max_supported"] is True, st.get("max_unsupported_reason"))
check("... and then no reason is given",
      st["max_unsupported_reason"] is None, st.get("max_unsupported_reason"))
check("max block is null when not running", st["max"] is None, st.get("max"))
check("fans found by hwmon name, not number",
      st["fans"]["available"] and st["fans"]["rpm"] == [3700, 4000], st["fans"])
check("package temperature from x86_pkg_temp", st["pkg_temp_c"] == 70, st["pkg_temp_c"])
check("on AC", st["on_ac"] is True, st["on_ac"])

capture(eco.cmd_on, args(level="gentle"))
check("mode is eco while eco state exists",
      capture(eco.cmd_status, args())["mode"] == "eco")
capture(eco.cmd_off, args())

write_max_state(pid=os.getpid(), state="paused", pause_reason="battery",
                fans="max", since=1, last_tick=2, last_tick_ok=True,
                last_error=None)
st = capture(eco.cmd_status, args())
check("mode is max while the keep-alive pid lives", st["mode"] == "max", st.get("mode"))
check("paused state is published",
      st["max"]["state"] == "paused" and st["max"]["pause_reason"] == "battery",
      st["max"])
check("max block marks itself active", st["max"]["active"] is True, st["max"])

write_max_state(pid=os.getpid(), state="paused", pause_reason="fans settling",
                fans="boost", fans_state="auto", settle_until=1790000125,
                fans_armed_until=1790000125, since=1, last_tick=2,
                last_tick_ok=True, last_error=None)
st = capture(eco.cmd_status, args())
check("the fan hand-back is published",
      st["max"]["fans"] == "boost" and st["max"]["fans_state"] == "auto"
      and st["max"]["settle_until"] == 1790000125
      and st["max"]["fans_armed_until"] == 1790000125, st["max"])

write_max_state(pid=999999999, state="active")
st = capture(eco.cmd_status, args())
check("a dead pid means stale state, not max",
      st["mode"] == "normal" and st["max"] is None, st.get("mode"))

pathlib.Path(eco.MAX_LAST_FILE).write_text(
    json.dumps({"ended_at": 5, "reason": "profile changed"}))
check("last end reason is reported",
      capture(eco.cmd_status, args())["max_last"]["reason"] == "profile changed")
cleanup()


# ---------------------------------------------------------------------------
# 10. max support probe
# ---------------------------------------------------------------------------
print("\n\033[1m10. max support probe\033[0m")
fake_sysfs(BASE)
fake_max_env(board="8C78")
ok, why = eco.max_support()
check("unknown board is unsupported with a reason",
      ok is False and "8C78" in why, why)
cleanup()

fake_sysfs(BASE)
fake_max_env(profile_driver="thinkpad_acpi")
ok, why = eco.max_support()
check("non hp-wmi profile is unsupported", ok is False and "hp-wmi" in why, why)
cleanup()

fake_sysfs(BASE)
fake_max_env(choices="low-power balanced")
ok, why = eco.max_support()
check("hp-wmi without performance is unsupported",
      ok is False and "performance" in why, why)
cleanup()

fake_sysfs(BASE)
fake_max_env(hwmon=False)
check("no hp hwmon -> fans not controllable", eco.fans_controllable() is False)
check("fan_info says unavailable", eco.fan_info()["available"] is False)
cleanup()

fake_sysfs(BASE)
fake_max_env(ac="0")
check("mains offline -> on battery", eco.on_ac() is False)
cleanup()


# ---------------------------------------------------------------------------
# 11. eco refuses to stack on top of max
# ---------------------------------------------------------------------------
print("\n\033[1m11. eco refuses to stack on top of max\033[0m")
fake_sysfs(BASE)
fake_max_env()
write_max_state(pid=os.getpid(), state="active", fans="auto")
res = capture(eco.cmd_on, args(level="gentle"))
check("eco on is refused while max runs",
      res["ok"] is False and "max" in res["msg"], res)
check("PL1 untouched", val("rapl_pl1") == "55000000", val("rapl_pl1"))
check("no eco state written", eco.load_state() is None)
cleanup()

# ---------------------------------------------------------------------------
# 12. battery draw
# ---------------------------------------------------------------------------
print("\n\033[1m12. battery draw\033[0m")
fake_sysfs(BASE)
fake_max_env(ac="0", bat_status="Discharging", bat_power="63012000")
check("discharging battery reports the whole laptop's draw",
      eco.battery_draw_w() == 63.0, eco.battery_draw_w())
cleanup()

fake_sysfs(BASE)
fake_max_env(bat_status="Charging", bat_power="42000000")
check("charging is not draw", eco.battery_draw_w() is None, eco.battery_draw_w())
cleanup()

fake_sysfs(BASE)
fake_max_env(ac="0", bat_status="Discharging", bat_power="garbage")
check("unreadable power_now gives None", eco.battery_draw_w() is None)
cleanup()

# ---------------------------------------------------------------------------
# 13. the power profile, read without powerprofilesctl
# ---------------------------------------------------------------------------
print("\n\033[1m13. power profile from platform_profile\033[0m")


class Spawns:
    """Stands in for eco's subprocess module and records every command."""
    SubprocessError = subprocess.SubprocessError

    def __init__(self):
        self.cmds = []

    def run(self, cmd, **kw):
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="power-saver\n", stderr="")


fake_sysfs(BASE)
fake_max_env()
profile = pathlib.Path(eco.PLATFORM_PROFILE)
real_subprocess, spawns = eco.subprocess, Spawns()
eco.subprocess = spawns
try:
    for value, name in (("low-power", "power-saver"), ("balanced", "balanced"),
                        ("performance", "performance")):
        profile.write_text(value)
        spawns.cmds.clear()
        got = eco.ppd_profile()
        check(f"{value} reads as {name}, with no process spawned",
              got == name and spawns.cmds == [], (got, spawns.cmds))

    profile.write_text("balanced")
    spawns.cmds.clear()
    st = capture(eco.cmd_status, args())
    check("status spawns no process at all", spawns.cmds == [], spawns.cmds)
    check("... and still reports the profile", st["ppd_profile"] == "balanced", st["ppd_profile"])

    asked = []
    real_ctl = eco.ppd_profile_ctl
    eco.ppd_profile_ctl = lambda: (asked.append(True), "quiet-from-ppd")[1]
    profile.write_text("quiet")
    check("a value the map does not know asks power-profiles-daemon",
          eco.ppd_profile() == "quiet-from-ppd" and asked == [True], asked)
    profile.unlink()
    asked.clear()
    check("so does an unreadable platform_profile",
          eco.ppd_profile() == "quiet-from-ppd" and asked == [True], asked)
    eco.ppd_profile_ctl = real_ctl
finally:
    eco.subprocess = real_subprocess
cleanup()

# ---------------------------------------------------------------------------
# 14. /run/ecomode stays readable whatever the umask
# ---------------------------------------------------------------------------
print("\n\033[1m14. the state directory ignores the umask\033[0m")
fake_sysfs(BASE)
shutil.rmtree(eco.STATE_DIR)
old_mask = os.umask(0o077)
try:
    eco.save_state({"level": "gentle", "knobs": {}})
finally:
    os.umask(old_mask)
check("save_state creates it 0755 under umask 077",
      stat.S_IMODE(os.stat(eco.STATE_DIR).st_mode) == 0o755,
      oct(os.stat(eco.STATE_DIR).st_mode))
os.chmod(eco.STATE_DIR, 0o700)            # left so by an earlier version
eco.save_state({"level": "gentle", "knobs": {}})
check("... and opens up one left 0700", stat.S_IMODE(os.stat(eco.STATE_DIR).st_mode) == 0o755)
cleanup()

# ---------------------------------------------------------------------------
# 15. deepest: lower limits, ceiling at most 30 %
# ---------------------------------------------------------------------------
print("\n\033[1m15. deepest\033[0m")
fake_sysfs(BASE)
res = capture(eco.cmd_on, args(level="deepest", max_pct=40))
check("apply reports ok", res["ok"], res)
check("PL1 -> 20W", val("rapl_pl1") == "20000000", val("rapl_pl1"))
check("PL2 -> 25W", val("rapl_pl2") == "25000000", val("rapl_pl2"))
check("ceiling capped at 30% although Deep's is 40", val("max_pct") == "30", val("max_pct"))
capture(eco.cmd_off, args())
check("all restored", (val("rapl_pl1"), val("rapl_pl2"), val("max_pct"))
      == ("55000000", "130000000", "100"))
capture(eco.cmd_on, args(level="deepest", max_pct=20))
check("a ceiling below 30 is kept", val("max_pct") == "20", val("max_pct"))
capture(eco.cmd_off, args())
cleanup()


# ---------------------------------------------------------------------------
# 16. a profile change undoes Eco under it: `on` puts it back
# ---------------------------------------------------------------------------
print("\n\033[1m16. drift after a profile change\033[0m")
fake_sysfs(BASE)
pl1 = next(k for k in eco.KNOBS if k["id"] == "rapl_pl1")["path"]
capture(eco.cmd_on, args(level="gentle"))
pathlib.Path(pl1).write_text("130000000")     # hp-wmi on a switch to performance
res = capture(eco.cmd_on, args(level="gentle"))
check("same level again is not a no-op once something drifted",
      not res.get("noop") and res.get("repaired") == ["rapl_pl1"], res)
check("PL1 back at 35W", val("rapl_pl1") == "35000000", val("rapl_pl1"))
check("the firmware's new value became the recorded original",
      list(eco.load_state()["knobs"]["rapl_pl1"].values()) == ["130000000"],
      eco.load_state()["knobs"]["rapl_pl1"])
res = capture(eco.cmd_on, args(level="gentle"))
check("nothing drifted: a plain no-op again", res.get("noop") is True, res)
res = capture(eco.cmd_off, args())
check("off restores the new original", res["ok"] and val("rapl_pl1") == "130000000",
      (res, val("rapl_pl1")))

capture(eco.cmd_on, args(level="gentle"))
pathlib.Path(pl1).write_text("55000000")      # the firmware again, before off
res = capture(eco.cmd_off, args())
check("off leaves a value someone changed since alone, and says so",
      res["ok"] and res["kept"] == [pl1] and val("rapl_pl1") == "55000000",
      (res, val("rapl_pl1")))
check("... and still clears the state", eco.load_state() is None)
cleanup()


# ---------------------------------------------------------------------------
# 17. dGPU clock caps
# ---------------------------------------------------------------------------
print("\n\033[1m17. dGPU clock caps\033[0m")


def gpu_status(value):
    (pathlib.Path(eco.DGPU) / "power" / "runtime_status").write_text(value)


def nvsmi_calls():
    return [c[1:] for c in RUNS.ran("nvidia-smi")]


fake_sysfs(BASE)
res = capture(eco.cmd_on, args(level="gentle"))
check("gentle touches no GPU clock and starts no guard",
      nvsmi_calls() == [] and RUNS.ran("systemd-run") == [], RUNS.calls)
capture(eco.cmd_off, args())
check("... nor does its off", nvsmi_calls() == [], RUNS.calls)
cleanup()

fake_sysfs(BASE)
res = capture(eco.cmd_on, args(level="deep"))
rec = eco.read_json(eco.GPU_FILE)
check("a sleeping GPU is not woken to cap it", nvsmi_calls() == [], RUNS.calls)
check("... the record says pending", rec and rec["pending"] and not rec["applied"], rec)
check("... and the guard is started",
      any("ecomode-gpu.service" in " ".join(c) for c in RUNS.ran("systemd-run")), RUNS.calls)
gpu = next(c for c in res["selftest"]["checks"] if c["id"] == "gpu_caps")
check("self-test skips the cap, it does not fail it", gpu["status"] == "skipped", gpu)
RUNS.calls.clear()
capture(eco.cmd_off, args())
check("off after a GPU that slept throughout wakes nothing",
      nvsmi_calls() == [], RUNS.calls)
check("... stops the guard", RUNS.ran("systemctl", "stop", "ecomode-gpu.service") != [])
check("... and drops the record", not os.path.exists(eco.GPU_FILE))
cleanup()

fake_sysfs(BASE)
gpu_status("active")
res = capture(eco.cmd_on, args(level="deep"))
check("deep on an awake GPU locks the graphics clock only",
      nvsmi_calls() == [["-lgc", "210,1500"]], nvsmi_calls())
check("... and the self-test passes it",
      next(c for c in res["selftest"]["checks"] if c["id"] == "gpu_caps")["status"] == "pass")
RUNS.calls.clear()
res = capture(eco.cmd_on, args(level="deepest", max_pct=40))
check("switching to deepest resets, then locks graphics and memory",
      nvsmi_calls() == [["-rgc"], ["-lgc", "210,900"], ["-lmc", "405,405"]], nvsmi_calls())
RUNS.calls.clear()
capture(eco.cmd_off, args())
check("off resets both", nvsmi_calls() == [["-rgc"], ["-rmc"]], nvsmi_calls())
cleanup()

fake_sysfs(BASE)
gpu_status("active")
eco.dgpu_connectors = lambda enabled_only=False: ["card0-HDMI-A-1"]
capture(eco.cmd_on, args(level="deepest"))
rec = eco.read_json(eco.GPU_FILE)
check("with a display on the dGPU the memory clock is left alone",
      nvsmi_calls() == [["-lgc", "210,900"]] and rec["mem"] is None
      and rec["mem_skipped"], (nvsmi_calls(), rec))
capture(eco.cmd_off, args())
cleanup()

fake_sysfs(BASE)
gpu_status("active")
RUNS.replies[("nvidia-smi", "-lgc")] = (1, "", "Setting locked clocks is not supported")
res = capture(eco.cmd_on, args(level="deep"))
gpu = next(c for c in res["selftest"]["checks"] if c["id"] == "gpu_caps")
check("a refused lock fails its self-test line with the driver's words",
      gpu["status"] == "fail" and "not supported" in gpu["note"], gpu)
capture(eco.cmd_off, args())
check("... and off then has nothing to reset", ["-rgc"] not in nvsmi_calls(), nvsmi_calls())
cleanup()


# ---------------------------------------------------------------------------
# 18. the guard re-applies after the GPU slept
# ---------------------------------------------------------------------------
print("\n\033[1m18. GPU guard\033[0m")
fake_sysfs(BASE)
check("nothing to guard without Eco", eco.guard_round() is False)
capture(eco.cmd_on, args(level="deep"))          # GPU asleep: pending
RUNS.calls.clear()
check("asleep: the guard waits and asks nothing",
      eco.guard_round() is True and nvsmi_calls() == [], RUNS.calls)
gpu_status("active")
eco.guard_round()
check("awake: it locks", nvsmi_calls() == [["-lgc", "210,1500"]], nvsmi_calls())
RUNS.calls.clear()
eco.guard_round()
check("still awake, nothing slept: nothing more", nvsmi_calls() == [], nvsmi_calls())
(pathlib.Path(eco.DGPU) / "power" / "runtime_suspended_time").write_text("4000")
eco.guard_round()
check("it slept and woke: locked again", nvsmi_calls() == [["-lgc", "210,1500"]],
      nvsmi_calls())
capture(eco.cmd_on, args(level="gentle"))
check("a level without caps ends the guard", eco.guard_round() is False)
capture(eco.cmd_off, args())
cleanup()


# ---------------------------------------------------------------------------
# 19. parsing what the driver says
# ---------------------------------------------------------------------------
print("\n\033[1m19. nvidia-smi and MSR parsing\033[0m")
line = ("P0, 66.82, 80.00, 80.00, 140.00, 97, 40, "
        "68, 2475, 8001, 3105, 8001, 2100, 8188, 0x0000000000000024, "
        "NVIDIA GeForce RTX 4060 Laptop GPU")
g = eco.parse_gpu_csv(line)
check("numbers parsed", g["power_w"] == 66.82 and g["util_pct"] == 97 and
      g["gr_mhz"] == 2475 and g["vram_total_mib"] == 8188, g)
check("throttle bits decoded", g["reasons"] == ["power cap", "thermal (driver)"], g["reasons"])
na = eco.parse_gpu_csv(line.replace("80.00, 80.00", "[N/A], 80.00"))
check("[N/A] becomes None", na["limit_w"] is None, na)
check("a short line is refused", eco.parse_gpu_csv("P0, 3") is None)
check("a name with a comma survives",
      eco.parse_gpu_csv(line.replace("NVIDIA GeForce", "NVIDIA, Inc. GeForce"))["name"]
      == "NVIDIA, Inc. GeForce RTX 4060 Laptop GPU")
check("0x64F: PL1 and thermal now, log bits ignored",
      eco.decode_cpu_limits(0x1D030402) == ["PL1", "thermal"],
      eco.decode_cpu_limits(0x1D030402))
check("0x64F: nothing limiting", eco.decode_cpu_limits(0x1D030000) == [])
check("no MSR, no answer", eco.decode_cpu_limits(None) is None)


# ---------------------------------------------------------------------------
# 20. arguments stay inside what the password-free action allows
# ---------------------------------------------------------------------------
print("\n\033[1m20. argument bounds\033[0m")
for text, ok in (("1", True), ("10", True), ("0.5", False), ("0.001", False), ("11", False),
                 ("nan", False), ("x", False)):
    try:
        eco.power_window(text)
        good = True
    except argparse.ArgumentTypeError:
        good = False
    check(f"power --seconds {text}: {'allowed' if ok else 'refused'}", good is ok)

# ---------------------------------------------------------------------------
# 21. review follow-ups
# ---------------------------------------------------------------------------
print("\n\033[1m21. review follow-ups\033[0m")

# C1: the kernel clamps max_perf_pct to min_perf_pct and says nothing.
fake_sysfs(BASE)
pct = next(k for k in eco.KNOBS if k["id"] == "max_pct")["path"]
real_write = eco.write_path


def clamping_write(path, value):
    if path == pct:
        value = str(max(int(value), 16))
    return real_write(path, value)


eco.write_path = clamping_write
capture(eco.cmd_on, args(level="deep", max_pct=40))
pathlib.Path(pct).write_text("16")            # as if 10 had been asked for
eco.load_state()                              # sanity
state = eco.load_state()
state["written"][pct] = "16"                  # what an apply of 10 would record
eco.save_state(state)
check("a clamped value read back is Eco's own, not drift",
      not any(p == pct for _, p, _, _ in eco.drifted(state)), eco.drifted(state))
res = capture(eco.cmd_off, args())
check("... so off restores the original ceiling",
      res["ok"] and val("max_pct") == "100" and pct not in res["kept"], (res, val("max_pct")))
eco.write_path = real_write
cleanup()
check("a ceiling under the kernel's floor is refused",
      eco.MAX_PCT_RANGE[0] >= 20, eco.MAX_PCT_RANGE)

# I2: the state is on disk before the guard is started.
fake_sysfs(BASE)
seen = []
real_runs_call = RUNS.__call__
RUNS_ORIG = eco.run


def run_and_look(argv, timeout=10):
    if argv[0] == "systemd-run":
        seen.append(eco.load_state() is not None)
    return RUNS(argv, timeout)


eco.run = run_and_look
capture(eco.cmd_on, args(level="deep"))
eco.run = RUNS_ORIG
check("the guard starts with the state already saved", seen == [True], seen)
capture(eco.cmd_off, args())
cleanup()

# I3: a refused lock is not retried every round.
fake_sysfs(BASE)
(pathlib.Path(eco.DGPU) / "power" / "runtime_status").write_text("active")
RUNS.replies[("nvidia-smi", "-lgc")] = (1, "", "not supported")
capture(eco.cmd_on, args(level="deep"))
at = eco.read_json(eco.GPU_FILE)["at"]
RUNS.calls.clear()
eco.guard_round(now=at + 5)
check("refused: no retry 5 s later", RUNS.ran("nvidia-smi") == [], RUNS.calls)
eco.guard_round(now=at + 31)
check("... one after 30 s", len(RUNS.ran("nvidia-smi", "-lgc")) == 1, RUNS.calls)
rec = eco.read_json(eco.GPU_FILE)
RUNS.calls.clear()
eco.guard_round(now=rec["at"] + 31)
check("... then the pause doubles", RUNS.ran("nvidia-smi") == [], RUNS.calls)
eco.guard_round(now=rec["at"] + 61)
check("... to 60 s", len(RUNS.ran("nvidia-smi", "-lgc")) == 1, RUNS.calls)
(pathlib.Path(eco.DGPU) / "power" / "runtime_suspended_time").write_text("9000")
RUNS.calls.clear()
eco.guard_round(now=rec["at"] + 62)
check("a sleep in between makes it try at once", len(RUNS.ran("nvidia-smi", "-lgc")) == 1,
      RUNS.calls)
capture(eco.cmd_off, args())
cleanup()

# I4: a lock call cut short still gets reset.
fake_sysfs(BASE)
(pathlib.Path(eco.DGPU) / "power" / "runtime_status").write_text("active")
marks = []


def interrupted(argv, timeout=10):
    if argv[:2] == ["nvidia-smi", "-lgc"]:
        marks.append(eco.read_json(eco.GPU_FILE))
    return RUNS(argv, timeout)


eco.run = interrupted
capture(eco.cmd_on, args(level="deep"))
eco.run = RUNS_ORIG
check("the record says a lock may have reached the driver before it is asked",
      marks and marks[0] and marks[0]["gr_ever"] is True, marks)
capture(eco.cmd_off, args())
cleanup()

# I1: on and off take the state lock; ecomode-max's child does not.
fake_sysfs(BASE)
import fcntl as _fcntl
held = []
real_flock = eco.fcntl.flock
eco.fcntl = type("F", (), {"LOCK_EX": _fcntl.LOCK_EX,
                           "flock": staticmethod(lambda fd, op: held.append(op))})
capture(eco.cmd_on, args(level="gentle"))
capture(eco.cmd_off, args())
check("on and off each take the lock", len(held) == 2, held)
os.environ[eco.LOCK_HELD_ENV] = "1"
held.clear()
capture(eco.cmd_on, args(level="gentle"))
capture(eco.cmd_off, args())
os.environ.pop(eco.LOCK_HELD_ENV)
check("... unless the parent holds it already", held == [], held)
eco.fcntl = _fcntl
cleanup()

# Portability: no NVIDIA driver, no clock caps - and no failure either.
fake_sysfs(BASE)
drv = pathlib.Path(eco.DGPU) / "driver"
drv.unlink()
(ROOT / "drivers" / "nouveau").mkdir(parents=True)
os.symlink(ROOT / "drivers" / "nouveau", drv)
(pathlib.Path(eco.DGPU) / "power" / "runtime_status").write_text("active")
res = capture(eco.cmd_on, args(level="deepest"))
check("deepest on nouveau: CPU applied, no GPU line, no nvidia-smi, passes",
      res["ok"] and not any(c["id"] == "gpu_caps" for c in res["selftest"]["checks"])
      and RUNS.ran("nvidia-smi") == [] and RUNS.ran("systemd-run") == [],
      (res["selftest"], RUNS.calls))
capture(eco.cmd_off, args())
cleanup()

fake_sysfs(BASE)
real_dgpu = eco.DGPU
eco.DGPU = str(ROOT / "no-such-gpu")
res = capture(eco.cmd_on, args(level="deep"))
check("deep without any NVIDIA GPU: fine, nothing GPU-side",
      res["ok"] and RUNS.ran("nvidia-smi") == [], res)
st = capture(eco.cmd_status, args(deep=True))
check("... and status says there is none", st["dgpu"]["present"] is False, st["dgpu"])
capture(eco.cmd_off, args())
eco.DGPU = real_dgpu
cleanup()

# find_dgpu: by vendor and class, not by address.
tmp = pathlib.Path(tempfile.mkdtemp())
for addr, vendor, klass in (("0000:00:02.0", "0x8086", "0x030000"),
                            ("0000:02:00.0", "0x10de", "0x040300"),   # its audio
                            ("0000:64:00.0", "0x10de", "0x030200")):
    d = tmp / addr
    d.mkdir()
    (d / "vendor").write_text(vendor + "\n")
    (d / "class").write_text(klass + "\n")
real_pci = eco.PCI_DEVICES
eco.PCI_DEVICES = str(tmp)
check("the NVIDIA display controller is found wherever it sits",
      eco.find_dgpu() == str(tmp / "0000:64:00.0"), eco.find_dgpu())
shutil.rmtree(tmp / "0000:64:00.0")
check("no NVIDIA display controller: a path that does not exist",
      not os.path.exists(eco.find_dgpu()), eco.find_dgpu())
eco.PCI_DEVICES = real_pci
shutil.rmtree(tmp)

# MSR 0x64F only on Intel.
real_intel = eco.cpu_is_intel
real_msr = eco.read_msr
eco.read_msr = lambda reg, cpu=0: 0x400
eco.cpu_is_intel = lambda: False
check("not Intel: no limit reasons, whatever the register holds", eco.cpu_limit_now() is None)
eco.cpu_is_intel = lambda: True
check("Intel: decoded", eco.cpu_limit_now() == ["PL1"])
eco.cpu_is_intel, eco.read_msr = real_intel, real_msr

# M6: no root-only views through pkexec.
os.environ["PKEXEC_UID"] = "1000"
real_argv = sys.argv
try:
    for argv in (["ecomode", "status", "--deep"], ["ecomode", "diagnostics"]):
        sys.argv = argv
        import contextlib as _cl, io as _io
        with _cl.redirect_stderr(_io.StringIO()):
            code = eco.main()
        check(f"pkexec {' '.join(argv[1:])} is refused", code == 2, code)
finally:
    sys.argv = real_argv
    os.environ.pop("PKEXEC_UID")

# ---------------------------------------------------------------------------
# 22. battery and charge limit
# ---------------------------------------------------------------------------
print("\n\033[1m22. battery and charge limit\033[0m")


def fake_battery(capacity=50, status="Charging", ac="1", behaviour="[auto] inhibit-charge",
                 extra=True):
    """A laptop battery with charge_behaviour that formats like sysfs does,
    a mouse battery to be ignored, and the mains supply."""
    ps = ROOT / "ps"
    shutil.rmtree(ps, ignore_errors=True)

    def w(rel, text):
        f = ps / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    w("hidpp_battery_0/type", "Battery")
    w("hidpp_battery_0/scope", "Device")
    w("hidpp_battery_0/capacity", "90")
    w("BAT1/type", "Battery")
    w("BAT1/present", "1")
    w("BAT1/status", status)
    w("BAT1/capacity", str(capacity))
    w("BAT1/energy_now", str(int(65_000_000 * capacity / 100)))
    w("BAT1/energy_full", "65000000")
    w("BAT1/energy_full_design", "83000000")
    w("BAT1/power_now", "20000000")
    w("BAT1/cycle_count", "0")
    if behaviour is not None:
        w("BAT1/charge_behaviour", behaviour)
    w("ACAD/type", "Mains")
    w("ACAD/online", ac)
    eco.POWER_SUPPLY_GLOB = str(ps / "*")
    eco.CHARGE_DIR = str(ROOT / "varlib")
    eco.CHARGE_CONF = str(ROOT / "varlib" / "charge.json")
    eco.CHARGE_HISTORY = str(ROOT / "varlib" / "charge-history")
    eco.CHARGE_SLEEP_FILE = str(ROOT / "varlib" / "charge-sleep.json")
    eco.CHARGE_HEALTH = str(ROOT / "varlib" / "battery-health")
    return ps / "BAT1"


real_write_path = eco.write_path


def sysfs_write(path, value):
    """charge_behaviour takes a mode and shows them all, the current one
    bracketed; only the modes it offers are accepted."""
    if path.endswith("charge_behaviour"):
        modes = ["auto", "inhibit-charge"]
        if value not in modes:
            return False, "Invalid argument"
        value = " ".join(f"[{m}]" if m == value else m for m in modes)
    return real_write_path(path, value)


eco.write_path = sysfs_write

D = eco.charge_decide
check("decide: on AC at the stop level -> inhibit", D(True, 80, "auto", 80) == "inhibit-charge")
check("decide: above it too", D(True, 93, "auto", 80) == "inhibit-charge")
check("decide: already inhibited -> leave it", D(True, 85, "inhibit-charge", 80) is None)
check("decide: inside the window -> leave it, either way",
      D(True, 77, "inhibit-charge", 80) is None and D(True, 77, "auto", 80) is None)
check("decide: at stop-5 -> auto again", D(True, 75, "inhibit-charge", 80) == "auto")
check("decide: on battery the flag is cleared", D(False, 90, "inhibit-charge", 80) == "auto")
check("decide: on battery and already auto -> nothing", D(False, 90, "auto", 80) is None)
check("decide: no mains reading counts as battery", D(None, 90, "inhibit-charge", 80) == "auto")
check("decide: no capacity -> nothing", D(True, None, "auto", 80) is None)

check("parse: current and offered",
      eco.parse_behaviour("auto [inhibit-charge] force-discharge")
      == ("inhibit-charge", ["auto", "inhibit-charge", "force-discharge"]))
check("parse: nothing", eco.parse_behaviour(None) == (None, []))

fake_sysfs(BASE)
bat = fake_battery()
info = eco.battery_info()
check("the laptop battery is found, not the mouse's", info["name"] == "BAT1", info)
check("health from full vs design", info["health_pct"] == 78, info)
check("a cycle count of 0 means not kept", info["cycles"] is None, info)
check("charge control offered", info["charge_supported"] is True and info["charge_mode"] == "auto", info)
check("time to full when charging", info["minutes_left"] == round(60 * (65e6 - 32.5e6) / 20e6), info)
(bat / "status").write_text("Discharging")
check("time left when discharging", eco.battery_info()["minutes_left"] == round(60 * 32.5e6 / 20e6))

RUNS.calls.clear()
RUNS.replies[("systemctl", "cat")] = (0, "[Unit]", "")
(bat / "status").write_text("Charging")
(bat / "capacity").write_text("85")
res = capture(eco.cmd_charge, argparse.Namespace(json=True, action="on", stop=80))
check("on: saved, service enabled, and at 85 % charging stops at once",
      res["ok"] and res["written"] == "inhibit-charge"
      and RUNS.ran("systemctl", "enable", "--now", "ecomode-charge.service")
      and eco.load_charge_conf() == {"enabled": True, "stop": 80, "log": False}, (res, RUNS.calls))
check("... sysfs now says inhibit-charge",
      eco.parse_behaviour((bat / "charge_behaviour").read_text())[0] == "inhibit-charge")
(bat / "capacity").write_text("78")
check("a round inside the window changes nothing", eco.charge_round() == (None, None))
(bat / "capacity").write_text("75")
check("at 75 % charging resumes", eco.charge_round() == ("auto", None))
(ROOT / "ps" / "ACAD" / "online").write_text("0")
(bat / "capacity").write_text("90")
check("unplugged at 90 %: nothing set", eco.charge_round() == (None, None))

(ROOT / "ps" / "ACAD" / "online").write_text("1")
eco.charge_round()
res = capture(eco.cmd_charge, argparse.Namespace(json=True, action="off", stop=80))
check("off: service disabled, charging back to auto, setting kept as off",
      res["ok"] and RUNS.ran("systemctl", "disable", "--now", "ecomode-charge.service")
      and eco.parse_behaviour((bat / "charge_behaviour").read_text())[0] == "auto"
      and eco.load_charge_conf()["enabled"] is False, res)
check("a round with the limit off sets nothing more", eco.charge_round() == (None, None))

RUNS.calls.clear()
fake_battery(behaviour=None)
res = capture(eco.cmd_charge, argparse.Namespace(json=True, action="on", stop=80))
check("a battery without charge_behaviour: refused, nothing enabled",
      not res["ok"] and "charge_behaviour" in res["reason"]
      and RUNS.ran("systemctl", "enable") == [], (res, RUNS.calls))
check("... the service round says why and writes nothing",
      eco.charge_round() == (None, "no charge_behaviour with auto and inhibit-charge"))
check("... and status says it is not supported", eco.battery_info()["charge_supported"] is False)

fake_battery(behaviour="[auto] force-discharge")
check("only force-discharge offered is not enough", eco.charge_supported(eco.find_battery()) is False)

RUNS.calls.clear()
fake_battery()
RUNS.replies[("systemctl", "cat")] = (1, "", "No files found")
res = capture(eco.cmd_charge, argparse.Namespace(json=True, action="on", stop=80))
check("no service installed: refused with a pointer to install.sh",
      not res["ok"] and "install.sh" in res["reason"], res)

pathlib.Path(eco.CHARGE_DIR).mkdir(parents=True, exist_ok=True)
pathlib.Path(eco.CHARGE_CONF).write_text('{"enabled": true, "stop": 7}')
check("a stop level out of range in the file falls back to 80",
      eco.load_charge_conf() == {"enabled": True, "stop": 80, "log": False})
pathlib.Path(eco.CHARGE_CONF).write_text("garbage")
check("a broken file means off", eco.load_charge_conf()["enabled"] is False)

for text, ok in (("80", True), ("50", True), ("95", True), ("49", False), ("96", False),
                 ("100", False), ("8O", False), ("-80", False)):
    try:
        eco.charge_stop(text)
        good = True
    except argparse.ArgumentTypeError:
        good = False
    check(f"charge --stop {text}: {'allowed' if ok else 'refused'}", good is ok)


# ── Who keeps the dGPU awake ─────────────────────────────────────────────────
print("\n\033[1m22a. dGPU holders\033[0m")
K = eco.holder_keeps_awake
NV = {"/dev/nvidia0", "/dev/nvidiactl"}
check("an app with the driver open keeps it awake", K("blender", NV, False) is True)
check("the DRM card node alone never does", K("Xwayland", {"/dev/dri/card0"}, True) is False)
check("the desktop with the driver open, no display on the dGPU: lets it sleep",
      not K("kwin_wayland", NV, False) and not K("Xorg", NV, False)
      and not K("Xwayland", NV, False) and not K("plasmashell", NV, False))
check("the desktop driving a display on the dGPU: keeps it awake",
      K("kwin_wayland", NV, True) and K("Xorg", NV, True))
check("the note says why the desktop is listed",
      "lets it sleep" in eco.holder_note(1, "kwin_wayland", False)
      and "drives a display" in eco.holder_note(1, "kwin_wayland", True))

NVSMI = """+-----------------------------------------------------------------------------------------+
| Processes:                                                                              |
|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |
|        ID   ID                                                               Usage      |
|=========================================================================================|
|    0   N/A  N/A            1095      G   /usr/lib/Xorg                             4MiB |
|    0   N/A  N/A            1350    C+G   /usr/bin/kwin_wayland                     7MiB |
|    0   N/A  N/A            4321      C   /opt/my app/bin/render job               812MiB |
+-----------------------------------------------------------------------------------------+
"""
procs = eco.parse_nvsmi_processes(NVSMI)
check("driver list: pid, path (spaces kept) and type",
      procs == [(1095, "/usr/lib/Xorg", "G"), (1350, "/usr/bin/kwin_wayland", "C+G"),
                (4321, "/opt/my app/bin/render job", "C")], procs)
check("driver list: no table at all is not an empty list", eco.parse_nvsmi_processes("no gpu\n") is None
      and eco.parse_nvsmi_processes(None) is None)
check("driver list: a table with no processes is empty",
      eco.parse_nvsmi_processes("| Processes: |\n|  No running processes found |\n") == [])
check("driver list: a row it cannot read makes the list unknown",
      eco.parse_nvsmi_processes("| Processes: |\n|    0   N/A  N/A   1095   X   /usr/lib/Xorg  4MiB |\n")
      is None)
mine = [{"pid": 77, "name": "blender", "count": 1, "awake": True, "follower": False, "note": None}]
merged = eco.merge_driver_holders(mine, procs, False)
names = [(h["name"], h["awake"]) for h in merged]
check("merged: the hidden desktop is listed, not keeping it awake; a real job is",
      names == [("blender", True), ("render job", True), ("kwin_wayland", False),
                ("Xorg", False)], names)
check("merged: a process seen in /proc is not listed twice",
      len(eco.merge_driver_holders(
          [{"pid": 1350, "name": "kwin_wayland", "count": 1, "awake": False,
            "follower": False, "note": None}], procs, False)) == 3)
check("merged: with a display on the dGPU the desktop does keep it awake",
      all(h["awake"] for h in eco.merge_driver_holders([], procs, True)))

# ── Battery care log: what the journal says, and the chart's history ────────
print("\n\033[1m22b. battery care log\033[0m")
CONF = {"enabled": True, "stop": 80, "log": True}


def snap(cap, ac=True, mode="auto", status="Charging", power_w=30.0):
    return {"cap": cap, "ac": ac, "mode": mode, "status": status, "power_w": power_w}


w = eco.ChargeWatch()
lines, ev = w.observe(1000, snap(61), None, CONF)
check("first reading: says the limit and the battery",
      ev == "start" and lines == ["watching: limit 80 %, resume at 75 %; "
                                  "battery 61 %, charging at 30.0 W, on AC"], lines)
lines, ev = w.observe(1030, snap(62), None, CONF)
check("a 1 % step inside the same 5 %: no line, but a chart point", lines == [] and ev == "", (lines, ev))
lines, ev = w.observe(1060, snap(65), None, CONF)
check("crossing 65 %: one step line", lines == ["battery 65 %, charging at 30.0 W, on AC"], lines)
lines, ev = w.observe(1090, snap(80, mode="inhibit-charge"), "inhibit-charge", CONF)
check("the stop: says why and where it resumes", ev == "stop" and lines == [
      "charging stopped at 80 %: reached the 80 % limit (charge_behaviour = inhibit-charge); "
      "resumes at 75 %"], lines)
lines, ev = w.observe(1095, snap(80, mode="inhibit-charge", status="Not charging", power_w=None),
                      None, CONF)
check("the firmware following is confirmed, with the delay",
      lines == ["firmware followed: battery not charging after 5 s"], lines)
lines, ev = w.observe(1095 + 1800, snap(80, mode="inhibit-charge", status="Not charging",
                                        power_w=None), None, CONF)
check("held half an hour: nothing logged, nothing new for the chart", lines == [] and ev is None,
      (lines, ev))
lines, ev = w.observe(1095 + 3600, snap(80, mode="inhibit-charge", status="Not charging",
                                        power_w=None), None, CONF)
check("held an hour: one heartbeat, and a chart point with it",
      lines == ["no change for 1 h 00 min: battery 80 %, held, not charging, on AC"]
      and ev == "", (lines, ev))
lines, ev = w.observe(5000, snap(79, ac=False, mode="auto", status="Discharging", power_w=12.0),
                      "auto", CONF)
check("unplugged: the cause first, then what was done",
      ev == "unplug" and lines == ["AC unplugged at 79 %: running on battery",
                                   "charge hold cleared at 79 %: not needed on battery "
                                   "(charge_behaviour = auto)"], lines)
lines, ev = w.observe(5030, snap(79, ac=False, mode="inhibit-charge", status="Discharging"),
                      None, CONF)
check("a mode change nobody here made: warned",
      ev == "outside" and lines[0].startswith("warning: charge_behaviour changed outside"), lines)

w = eco.ChargeWatch()
w.observe(0, snap(70), None, CONF)
w.observe(30, snap(75, mode="auto", status="Not charging"), "auto", CONF)
lines, _ = w.observe(30 + eco.CHARGE_CONFIRM_SECS, snap(75, mode="auto", status="Not charging"),
                     None, CONF)
check("the firmware not following a write: one warning after the wait",
      len(lines) == 1 and lines[0].startswith("warning: charge_behaviour is auto but the battery "
                                              "still reports Not charging"), lines)
lines, _ = w.observe(30 + 2 * eco.CHARGE_CONFIRM_SECS, snap(75, mode="auto", status="Not charging"),
                     None, CONF)
check("... and only once", not any(x.startswith("warning") for x in lines), lines)

w = eco.ChargeWatch()
w.observe(0, snap(67), None, CONF)
lines, ev = w.observe(10, snap(67, mode="inhibit-charge"), None, CONF, sleeping=True)
check("the sleep hook's hold is not taken for an outside change", lines == [], lines)
lines, ev = w.observe(4000, snap(66, mode="auto"), None, CONF, slept=True)
check("after a wake: a chart point, no lines of its own", ev == "wake" and lines == [], (lines, ev))

check("durations read like a person wrote them",
      (eco.duration_text(42), eco.duration_text(600), eco.duration_text(4320))
      == ("42 s", "10 min", "1 h 12 min"))

fake_sysfs(BASE)
bat = fake_battery(capacity=67)
eco.history_append(100, snap(67), "plug")
eco.history_append(200, snap(68, ac=None, mode=None, status="Weird"))
with open(eco.CHARGE_HISTORY, "a") as fh:
    fh.write("garbage line\n")
check("history: about 25 bytes a line", len(pathlib.Path(eco.CHARGE_HISTORY).read_text()
                                           .splitlines()[0]) <= 30)
rows = eco.history_read(0)
check("history reads back, damaged lines skipped",
      rows == [[100, 67, True, "auto", "Charging", "plug"],
               [200, 68, None, None, "Unknown", None]], rows)
check("history: only the asked window", eco.history_read(150) == rows[1:])
eco.history_trim(150 + 7 * 86400, days=7)
check("trim drops what is older than the window",
      [r[0] for r in eco.history_read(0)] == [200], eco.history_read(0))

# Capacity as the battery's controller estimates it: every change, and daily.
DAY = 86400
noon = time.mktime((2026, 10, 26, 12, 0, 0, 0, 0, -1))
check("health: due with nothing recorded", eco.health_due(None, noon, 66.53))
check("health: not due again the same day", not eco.health_due([noon, 66.53, 83.0], noon + 3600, 66.53))
check("health: due at once when the estimate changes",
      eco.health_due([noon, 66.53, 83.0], noon + 60, 66.2))
check("health: a few mWh of jitter is not a change",
      not eco.health_due([noon, 66.53, 83.0], noon + 60, 66.48))
check("health: due on the next day", eco.health_due([noon, 66.53, 83.0], noon + DAY, 66.53))
check("health: capacity in Wh from energy_full", eco.battery_capacity_wh(bat) == (65.0, 83.0))
last, line = eco.health_round(bat, noon, None)
check("health: the first record is logged once",
      last == [int(noon), 65.0, 83.0] and line.startswith("battery capacity: 65.00 Wh, 78.3 %"), line)
last, line = eco.health_round(bat, noon + 30, last)
check("health: an unchanged estimate the same day writes nothing", line is None
      and len(eco.health_read()) == 1)
(bat / "energy_full").write_text("64500000")
last, line = eco.health_round(bat, noon + 60, last)
check("health: a change is logged with both values",
      line == "battery capacity estimate changed: 65.00 -> 64.50 Wh (77.7 % of the 83 Wh design)", line)
eco.health_round(bat, noon + DAY, last)
check("health: the daily line, nothing logged for it",
      [r[1] for r in eco.health_read()] == [65.0, 64.5, 64.5])
with open(eco.CHARGE_HEALTH, "a") as fh:
    fh.write("garbage\n")
check("health: damaged lines are skipped", len(eco.health_read()) == 3)
eco.trim_rows(eco.CHARGE_HEALTH, eco.parse_health_line, noon + 400 * DAY + 30, eco.CHARGE_HEALTH_DAYS)
check("health: trimmed after 400 days", [r[0] for r in eco.health_read()]
      == [int(noon + 60), int(noon + DAY)], eco.health_read())
rows_before = len(eco.health_read())
(bat / "energy_full").unlink()
check("health: a battery without energy_full is skipped, nothing written",
      eco.health_round(bat, noon + 2 * DAY, None) == (None, None)
      and len(eco.health_read()) == rows_before)
(bat / "energy_full").write_text("65000000")
real_append = eco.append_line
eco.append_line = lambda path, text: False
last = [int(noon), 65.0, 83.0]
(bat / "energy_full").write_text("64000000")
check("health: a failed write is retried, not logged as recorded",
      eco.health_round(bat, noon + 3 * DAY, last) == (last, None))
eco.append_line = real_append
(bat / "energy_full").write_text("65000000")

# The sleep hook: holds charging before suspend, the service restores it.
eco.save_charge_conf({"enabled": True, "stop": 80, "log": True})
(ROOT / "ps" / "ACAD" / "online").write_text("1")
out = capture_text(eco.cmd_charge_sleep, argparse.Namespace(when="pre", kind="suspend"))
check("pre-suspend at 67 % on AC: charging held",
      eco.parse_behaviour((bat / "charge_behaviour").read_text())[0] == "inhibit-charge"
      and "going to sleep at 67 %" in out, out)
check("... and the history marks the sleep", eco.history_read(0)[-1][5] == "sleep")
check("the service sees a suspend under way, not a wake",
      eco.charge_after_sleep(bat) == (False, True))
(bat / "capacity").write_text("66")
out = capture_text(eco.cmd_charge_sleep, argparse.Namespace(when="post", kind="suspend"))
check("post: says how long and what it was", "woke up after" in out and "67 % -> 66 %" in out, out)
check("the service then puts auto back and drops the file",
      eco.charge_after_sleep(bat) == (True, False)
      and eco.parse_behaviour((bat / "charge_behaviour").read_text())[0] == "auto"
      and not os.path.exists(eco.CHARGE_SLEEP_FILE))
out = capture_text(eco.cmd_charge_sleep, argparse.Namespace(when="pre", kind="hibernate"))
check("hibernate is left alone",
      eco.parse_behaviour((bat / "charge_behaviour").read_text())[0] == "auto"
      and not os.path.exists(eco.CHARGE_SLEEP_FILE), out)
def mode_now():
    return eco.parse_behaviour((bat / "charge_behaviour").read_text())[0]


PRE = argparse.Namespace(when="pre", kind="suspend")
POST = argparse.Namespace(when="post", kind="suspend")

(bat / "capacity").write_text("30")
out = capture_text(eco.cmd_charge_sleep, PRE)
check("pre-suspend at 30 % on AC: not held, it charges while asleep",
      mode_now() == "auto" and "below 50 %" in out
      and eco.read_json(eco.CHARGE_SLEEP_FILE)["held"] is False, out)
(bat / "capacity").write_text("55")
out = capture_text(eco.cmd_charge_sleep, POST)
check("... the wake is still logged, with nothing to put back", "nothing was held" in out, out)
check("... and the service restores nothing, but the sleep ends for the chart",
      eco.charge_after_sleep(bat) == (True, False) and mode_now() == "auto"
      and not os.path.exists(eco.CHARGE_SLEEP_FILE))

(bat / "capacity").write_text("49")
capture_text(eco.cmd_charge_sleep, PRE)
check("at 49 %: still below the floor", mode_now() == "auto")
capture_text(eco.cmd_charge_sleep, POST)
eco.charge_after_sleep(bat)
(bat / "capacity").write_text("50")
capture_text(eco.cmd_charge_sleep, PRE)
check("at 50 % on AC: held", mode_now() == "inhibit-charge")
capture_text(eco.cmd_charge_sleep, POST)
eco.charge_after_sleep(bat)
check("... and auto again after the wake", mode_now() == "auto")

(ROOT / "ps" / "ACAD" / "online").write_text("0")
(bat / "capacity").write_text("90")
out = capture_text(eco.cmd_charge_sleep, PRE)
check("pre-suspend on battery, even at 90 %: nothing held",
      mode_now() == "auto" and "nothing held" in out, out)
capture_text(eco.cmd_charge_sleep, POST)
eco.charge_after_sleep(bat)
(ROOT / "ps" / "ACAD" / "online").write_text("1")

pathlib.Path(eco.CHARGE_SLEEP_FILE).write_text(
    '{"t": 0, "cap": 80, "mode": "inhibit-charge", "ac": true, "woke": true}')
check("a file from the old hook (no 'held') still gets its mode back",
      eco.charge_after_sleep(bat) == (True, False) and mode_now() == "inhibit-charge")
eco.write_path(str(bat / "charge_behaviour"), "auto")

eco.save_charge_conf({"enabled": True, "stop": 80, "log": False})
(bat / "capacity").write_text("30")
out = capture_text(eco.cmd_charge_sleep, PRE)
check("log off and nothing held: the hook says nothing", out == "", out)
capture_text(eco.cmd_charge_sleep, POST)
eco.charge_after_sleep(bat)

eco.save_charge_conf({"enabled": False, "stop": 80, "log": True})
capture_text(eco.cmd_charge_sleep, argparse.Namespace(when="pre", kind="suspend"))
check("Battery care off: the hook does nothing",
      eco.parse_behaviour((bat / "charge_behaviour").read_text())[0] == "auto")

RUNS.replies[("systemctl", "cat")] = (0, "[Unit]", "")
eco.save_charge_conf({"enabled": True, "stop": 70, "log": True})
capture(eco.cmd_charge, argparse.Namespace(json=True, action="on", stop=80, log=None))
check("--log not given: the saved choice stays", eco.load_charge_conf()["log"] is True)
capture(eco.cmd_charge, argparse.Namespace(json=True, action="on", stop=80, log="off"))
check("--log off", eco.load_charge_conf() == {"enabled": True, "stop": 80, "log": False})
res = capture(eco.cmd_charge_history, argparse.Namespace(json=True, hours=24))
check("charge-history: the window and the levels for the chart",
      res["stop"] == 80 and res["resume"] == 75 and res["hours"] == 24
      and isinstance(res["points"], list), res)
check("charge-history: the capacity record comes with it", isinstance(res["health"], list)
      and all(len(r) == 3 for r in res["health"]), res["health"])
check("charge-history: sleeps over the whole week, whatever the chart window",
      all(r[5] in ("sleep", "wake", "start") for r in res["sleeps"]), res["sleeps"])

real_which = eco.shutil.which
eco.shutil.which = lambda name: "/usr/bin/" + name
RUNS.calls.clear()
RUNS.replies[("powerprofilesctl", "set")] = (0, "", "")
res = capture(eco.cmd_set_profile, argparse.Namespace(json=True, profile="power-saver"))
check("set-profile: the slider moves, the step answers ok",
      res == {"ok": True, "profile": "power-saver"}
      and RUNS.ran("powerprofilesctl", "set", "power-saver"), (res, RUNS.calls))
RUNS.replies[("powerprofilesctl", "set")] = (1, "", "Failed to set profile")
res = capture(eco.cmd_set_profile, argparse.Namespace(json=True, profile="balanced"))
check("set-profile: a failure says what and why",
      not res["ok"] and "balanced" in res["reason"] and "Failed to set" in res["reason"], res)
eco.shutil.which = lambda name: None
res = capture(eco.cmd_set_profile, argparse.Namespace(json=True, profile="balanced"))
check("set-profile: no daemon tool, said plainly", not res["ok"] and "powerprofilesctl" in res["reason"], res)
eco.shutil.which = real_which

# The service on stop: the limit holds above the stop level, never below it.
def run_service_once(capacity, enabled=True, stop=80):
    bat = fake_battery(capacity=capacity)
    eco.save_charge_conf({"enabled": enabled, "stop": stop})

    class Stop(Exception):
        pass

    real_sleep = eco.time.sleep
    eco.time.sleep = lambda s: (_ for _ in ()).throw(SystemExit(0))
    os.environ["INVOCATION_ID"] = "test"
    try:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                eco.cmd_charge_run(argparse.Namespace())
        except SystemExit:
            pass
    finally:
        eco.time.sleep = real_sleep
        os.environ.pop("INVOCATION_ID")
    return eco.parse_behaviour((bat / "charge_behaviour").read_text())[0]


import contextlib, io
eco.write_path = sysfs_write
check("service stopped at 85 % with the limit on: stays inhibited over a shutdown",
      run_service_once(85) == "inhibit-charge")
check("service stopped at 60 %: never left unable to charge", run_service_once(60) == "auto")
check("service with the limit off: ends at once, charging auto",
      run_service_once(90, enabled=False) == "auto")

no_bat = ROOT / "ps"
shutil.rmtree(no_bat)
no_bat.mkdir()
check("no battery at all: status says None", eco.battery_info() is None)
check("... and a round does nothing", eco.charge_round()[0] is None)
eco.write_path = real_write_path
cleanup()

# ---------------------------------------------------------------------------
print(f"\n\033[1m{PASS} passed, {FAIL} failed\033[0m")
sys.exit(1 if FAIL else 0)
