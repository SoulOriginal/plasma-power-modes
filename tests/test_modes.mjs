// SPDX-License-Identifier: GPL-2.0-or-later
// Runs the pure helpers from contents/ui/modes.js under node.
// Usage: node tests/test_modes.mjs
import { readFileSync } from "node:fs"
import vm from "node:vm"
import assert from "node:assert/strict"

const src = readFileSync(new URL("../contents/ui/modes.js", import.meta.url), "utf8")
    .replace(/^\.pragma library\s*$/m, "")
const ctx = {}
vm.runInNewContext(src, ctx)
// Arrays and objects made inside the vm context carry that context's
// prototypes, which assert/strict's deepEqual rejects even when the contents
// match; clone every result into this realm.
const m = {}
for (const [name, fn] of Object.entries(ctx)) m[name] = (...a) => structuredClone(fn(...a))

let passed = 0
function t(name, fn) {
    fn()
    passed++
    console.log("  PASS " + name)
}

const o = {
    helper: "/usr/local/bin/ecomode",
    maxHelper: "/usr/local/bin/ecomode-max",
    level: "gentle", maxPct: 40, fans: "max",
}
const MAX_ON = "pkexec /usr/local/bin/ecomode-max --json on --fans max"
const MAX_OFF = "pkexec /usr/local/bin/ecomode-max --json off"
const ECO_ON = "pkexec /usr/local/bin/ecomode --json on --level gentle --max-pct 40"
const ECO_OFF = "pkexec /usr/local/bin/ecomode --json off"

t("normal -> max is one call", () => assert.deepEqual(m.commandsFor("normal", "max", o), [MAX_ON]))
t("eco -> max leaves the eco restore to ecomode-max",
  () => assert.deepEqual(m.commandsFor("eco", "max", o), [MAX_ON]))
t("max -> eco stops max first",
  () => assert.deepEqual(m.commandsFor("max", "eco", o), [MAX_OFF, ECO_ON]))
t("eco -> normal", () => assert.deepEqual(m.commandsFor("eco", "normal", o), [ECO_OFF]))
t("max -> normal", () => assert.deepEqual(m.commandsFor("max", "normal", o), [MAX_OFF]))
t("normal -> normal does nothing", () => assert.deepEqual(m.commandsFor("normal", "normal", o), []))
t("max -> max re-applies the fan choice",
  () => assert.deepEqual(m.commandsFor("max", "max", { ...o, fans: "auto" }),
                         ["pkexec /usr/local/bin/ecomode-max --json on --fans auto"]))
t("boost is passed through",
  () => assert.deepEqual(m.commandsFor("normal", "max", { ...o, fans: "boost" }),
                         ["pkexec /usr/local/bin/ecomode-max --json on --fans boost"]))
t("an unknown fan mode never reaches the shell",
  () => assert.deepEqual(m.commandsFor("normal", "max", { ...o, fans: "max; reboot" }),
                         ["pkexec /usr/local/bin/ecomode-max --json on --fans auto"]))
t("eco -> eco re-applies the level",
  () => assert.deepEqual(m.commandsFor("eco", "eco", { ...o, level: "deep" }),
                         ["pkexec /usr/local/bin/ecomode --json on --level deep --max-pct 40"]))
const ecoRunning = { mode: "eco", on: true, level: "gentle", max_pct: 40 }
t("eco -> eco with the level and ceiling already applied asks for nothing",
  () => assert.deepEqual(m.commandsFor("eco", "eco", { ...o, status: ecoRunning }), []))
t("eco -> eco with a new level re-applies",
  () => assert.deepEqual(m.commandsFor("eco", "eco", { ...o, level: "deep", status: ecoRunning }),
                         ["pkexec /usr/local/bin/ecomode --json on --level deep --max-pct 40"]))
t("eco -> eco with a new ceiling re-applies",
  () => assert.deepEqual(m.commandsFor("eco", "eco", { ...o, maxPct: 30, status: ecoRunning }),
                         ["pkexec /usr/local/bin/ecomode --json on --level gentle --max-pct 30"]))
t("max -> max still goes to ecomode-max, which needs no password",
  () => assert.deepEqual(m.commandsFor("max", "max", { ...o, status: ecoRunning }), [MAX_ON]))

t("panel: normal", () => assert.equal(m.compactLabel({ mode: "normal" }, false).text, "off"))
t("panel: eco", () => assert.equal(m.compactLabel({ mode: "eco" }, false).text, "ECO"))
t("panel: max", () => assert.equal(
    m.compactLabel({ mode: "max", max: { state: "active", fans: "auto" } }, false).text, "MAX"))
t("panel: max with fans", () => assert.equal(
    m.compactLabel({ mode: "max", max: { state: "active", fans: "max" } }, false).text, "MAX·F"))
t("panel: max with boost", () => assert.equal(
    m.compactLabel({ mode: "max", max: { state: "active", fans: "boost" } }, false).text, "MAX·B"))
t("panel: paused max is dimmed", () => {
    const r = m.compactLabel({ mode: "max", max: { state: "paused", fans: "auto" } }, false)
    assert.equal(r.text, "MAX ⏸")
    assert.equal(r.dim, true)
})
t("panel: settling boost shows as paused", () => {
    const r = m.compactLabel({ mode: "max", max: { state: "paused", fans: "boost",
                                                   pause_reason: "fans settling" } }, false)
    assert.equal(r.text, "MAX ⏸")
    assert.equal(r.dim, true)
})
t("panel: error colour wins", () => assert.equal(m.compactLabel({ mode: "eco" }, true).color, "error"))
t("panel: helper without mode falls back to the eco flag",
  () => assert.equal(m.compactLabel({ on: true }, false).text, "ECO"))

t("middle click: normal -> last mode", () => assert.equal(m.middleClickTarget("normal", "last", "eco"), "eco"))
t("middle click: max -> normal", () => assert.equal(m.middleClickTarget("max", "last", "max"), "normal"))
t("middle click: pinned to max", () => assert.equal(m.middleClickTarget("normal", "max", "eco"), "max"))
t("middle click: eco -> normal even when pinned to max",
  () => assert.equal(m.middleClickTarget("eco", "max", "eco"), "normal"))

t("max line: active", () => assert.equal(
    m.maxLine({ max: { state: "active", last_tick: 100, last_tick_ok: true },
                fans: { rpm: [5200, 5600] }, pkg_temp_c: 96 }, 112),
    "holding performance · keep-alive 12 s ago · fans 5200/5600 RPM · 96 °C"))
t("max line: active with the fans at full speed", () => assert.equal(
    m.maxLine({ max: { state: "active", last_tick: 100, last_tick_ok: true, fans_state: "max" },
                fans: { rpm: [5200, 5600] }, pkg_temp_c: 96 }, 112),
    "holding performance · fans max · keep-alive 12 s ago · fans 5200/5600 RPM · 96 °C"))
t("max line: paused", () => assert.equal(
    m.maxLine({ max: { state: "paused" } }, 0), "paused: on battery, resumes on AC"))
t("max line: paused on battery", () => assert.equal(
    m.maxLine({ max: { state: "paused", pause_reason: "battery", settle_until: 500 } }, 0),
    "paused: on battery, resumes on AC"))
t("max line: fans settling counts down", () => assert.equal(
    m.maxLine({ max: { state: "paused", pause_reason: "fans settling", settle_until: 1125 } },
              1000.4),
    "paused: fans returning to automatic (~125 s)"))
t("max line: a settle past its deadline shows ~0 s", () => assert.equal(
    m.maxLine({ max: { state: "paused", pause_reason: "fans settling", settle_until: 1125 } },
              1140),
    "paused: fans returning to automatic (~0 s)"))
t("max line: settling without a deadline leaves the count out", () => assert.equal(
    m.maxLine({ max: { state: "paused", pause_reason: "fans settling" } }, 1000),
    "paused: fans returning to automatic"))
t("max line: a tick error is shown", () => assert.match(
    m.maxLine({ max: { state: "active", last_tick: 1, last_tick_ok: false,
                       last_error: "platform_profile: EIO" } }, 1), /platform_profile: EIO$/))
t("max line: nothing when max is off", () => assert.equal(m.maxLine({ max: null }, 0), ""))

t("ended line", () => assert.equal(
    m.endedLine({ ended_at: 0, reason: "profile changed" }, 180),
    "Max ended 3 min ago: profile changed"))
t("ended line: just now", () => assert.equal(
    m.endedLine({ ended_at: 100, reason: "off" }, 110), "Max ended just now: off"))
t("ended line: nothing recorded", () => assert.equal(m.endedLine(null, 0), ""))

const fresh = { cpu_w: 93.6, battery_w: null, at: 1000, interval: 3 }
t("watts: a fresh reading", () => assert.equal(m.wattsFresh(fresh, 1005), true))
t("watts: stale after three missed rounds", () => assert.equal(m.wattsFresh(fresh, 1011), false))
t("watts: no record is not fresh", () => assert.equal(m.wattsFresh(null, 0), false))
t("watts: rounded to whole watts", () => assert.equal(m.formatWatts(93.6), "94W"))
t("panel with watts shows the number and keeps the mode colour", () => {
    const r = m.withWatts({ text: "MAX", color: "max", dim: false }, true, fresh, 1001)
    assert.equal(r.text, "94W")
    assert.equal(r.color, "max")
})
t("panel with watts but no fresh reading shows a dash", () => assert.equal(
    m.withWatts({ text: "off", color: "off", dim: false }, true, fresh, 2000).text, "—"))
t("panel without watts keeps the mode text", () => assert.equal(
    m.withWatts({ text: "ECO", color: "eco", dim: false }, false, fresh, 1001).text, "ECO"))
t("paused max stays dimmed with watts", () => assert.equal(
    m.withWatts({ text: "MAX ⏸", color: "max", dim: true }, true, fresh, 1001).dim, true))

// max.json as the keeper writes it
const keeperState = {
    pid: 4242, since: 900, fans: "max", state: "active", pause_reason: null,
    last_tick: 1000, last_tick_ok: true, last_error: null, fans_state: "max",
    settle_until: null, fans_armed_until: 1125,
}
t("max file: a fresh record becomes status.max", () => assert.deepEqual(
    m.maxFromFile(JSON.stringify(keeperState), 1010),
    { active: true, state: "active", pause_reason: null, fans: "max", since: 900,
      last_tick: 1000, last_tick_ok: true, last_error: null, fans_state: "max",
      settle_until: null, fans_armed_until: 1125 }))
t("max file: fresh up to three missed rounds and 5 s",
  () => assert.notEqual(m.maxFromFile(JSON.stringify(keeperState), 1095), null))
t("max file: older than that, the keeper is dead or stuck",
  () => assert.equal(m.maxFromFile(JSON.stringify(keeperState), 1096), null))
t("max file: missing", () => assert.equal(m.maxFromFile("", 1000), null))
t("max file: garbage", () => assert.equal(m.maxFromFile("{\"pid\": 42", 1000), null))
t("max file: JSON that is not a keeper record",
  () => assert.equal(m.maxFromFile("[1, 2]", 1000), null))
t("max file: no tick yet", () => assert.equal(
    m.maxFromFile(JSON.stringify({ ...keeperState, last_tick: null }), 1000), null))
t("max file: a settling pause passes through", () => {
    const r = m.maxFromFile(JSON.stringify({ ...keeperState, state: "paused",
        pause_reason: "fans settling", fans: "boost", fans_state: "auto",
        settle_until: 1100 }), 1010)
    assert.equal(r.state, "paused")
    assert.equal(r.pause_reason, "fans settling")
    assert.equal(r.fans, "boost")
    assert.equal(r.fans_state, "auto")
    assert.equal(r.settle_until, 1100)
})
t("max file: a battery pause passes through", () => {
    const r = m.maxFromFile(JSON.stringify({ ...keeperState, state: "paused",
        pause_reason: "battery" }), 1010)
    assert.equal(m.maxLine({ max: r }, 1010), "paused: on battery, resumes on AC")
})

const fullStatus = {
    mode: "max", knobs: [{ id: "rapl_pl1" }], ppd_profile: "performance",
    max: { active: true, state: "active", fans: "auto", last_tick: 900 },
    fans: { available: true, rpm: [5200, 5600], pwm_enable: "0" }, pkg_temp_c: 96,
}
t("status from the file: max and mode are replaced, the rest kept", () => {
    const fromFile = m.maxFromFile(JSON.stringify(keeperState), 1010)
    const r = m.statusWithMax(fullStatus, fromFile)
    assert.deepEqual(r.max, fromFile)
    assert.equal(r.mode, "max")
    assert.deepEqual(r.knobs, fullStatus.knobs)
    assert.equal(r.ppd_profile, "performance")
})
t("status from the file: fan speeds and temperature it does not carry are dropped", () => {
    const r = m.statusWithMax(fullStatus, m.maxFromFile(JSON.stringify(keeperState), 1010))
    assert.deepEqual(r.fans, { available: true, rpm: [], pwm_enable: null })
    assert.equal(r.pkg_temp_c, null)
    assert.equal(m.maxLine(r, 1010), "holding performance · fans max · keep-alive 10 s ago")
})
t("status from the file is a new object, so bindings see the change", () => {
    const fromFile = ctx.maxFromFile(JSON.stringify(keeperState), 1010)
    assert.notEqual(ctx.statusWithMax(fullStatus, fromFile), fullStatus)
    assert.equal(fullStatus.pkg_temp_c, 96)
})

// ── 2026-10-24: deepest, drift, follow KDE, GPU ──────────────────────────
t("deepest is passed through",
  () => assert.deepEqual(m.commandsFor("normal", "eco", { ...o, level: "deepest" }),
                         ["pkexec /usr/local/bin/ecomode --json on --level deepest --max-pct 40"]))
t("an unknown level or a wild ceiling never reaches the shell",
  () => assert.deepEqual(m.commandsFor("normal", "eco", { ...o, level: "x; reboot", maxPct: 5 }),
                         ["pkexec /usr/local/bin/ecomode --json on --level gentle --max-pct 20"]))
t("forced eco -> eco asks the helper even when the status looks applied",
  () => assert.deepEqual(m.commandsFor("eco", "eco", { ...o, status: ecoRunning, force: true }),
                         [ECO_ON]))
t("max with Dynamic Boost",
  () => assert.deepEqual(m.commandsFor("normal", "max", { ...o, gpuBoost: true }),
                         [MAX_ON + " --gpu-boost"]))
const driftedEco = { ...ecoRunning, knobs: [
    { id: "rapl_pl1", in_level: true, managed: false, eco_target: "35000000", at_eco: false },
    { id: "epp", in_level: true, managed: true, eco_target: null, at_eco: false }] }
t("eco -> eco after the firmware undid PL1 re-applies",
  () => assert.deepEqual(m.commandsFor("eco", "eco", { ...o, status: driftedEco }), [ECO_ON]))
t("knobs managed by the daemon do not count as drift",
  () => assert.deepEqual(m.commandsFor("eco", "eco", { ...o, status: { ...ecoRunning, knobs: [
      driftedEco.knobs[1], { ...driftedEco.knobs[0], at_eco: true }] } }), []))

const normalSt = { mode: "normal", max_supported: true }
t("follow: power-saver -> eco", () => assert.equal(m.followTarget("power-saver", normalSt), "eco"))
t("follow: balanced while normal -> nothing", () => assert.equal(m.followTarget("balanced", normalSt), null))
t("follow: performance -> max", () => assert.equal(m.followTarget("performance", normalSt), "max"))
t("follow: performance without Max support -> normal, i.e. nothing here",
  () => assert.equal(m.followTarget("performance", { mode: "normal", max_supported: false }), null))
t("follow: performance without Max support from eco -> normal",
  () => assert.equal(m.followTarget("performance", { mode: "eco", max_supported: false }), "normal"))
t("follow: eco stays eco but is re-applied after a profile change",
  () => assert.equal(m.followTarget("power-saver", { mode: "eco" }), "eco"))
const maxSt = { mode: "max", max_supported: true, max: { state: "active" } }
t("follow: performance while Max holds it is Max's own", () => assert.equal(m.followTarget("performance", maxSt), null))
t("follow: balanced picked during Max -> normal", () => assert.equal(m.followTarget("balanced", maxSt), "normal"))
t("follow: Max paused on battery is left alone",
  () => assert.equal(m.followTarget("balanced", { ...maxSt, max: { state: "paused", pause_reason: "battery" } }), null))
t("follow: an unknown profile does nothing", () => assert.equal(m.followTarget("quiet", normalSt), null))

const withGpu = { ...fresh, cpu_w: 21.2, gpu: { state: "active", held: true, power_w: 19.3 } }
t("panel: CPU and GPU watts", () => assert.equal(
    m.withWatts({ text: "ECO", color: "eco", dim: false }, true, withGpu, 1001, true).text, "21·19W"))
t("panel: GPU left out when not asked", () => assert.equal(
    m.withWatts({ text: "ECO", color: "eco", dim: false }, true, withGpu, 1001, false).text, "21W"))
t("panel: a GPU nobody else holds has no reading", () => assert.equal(
    m.withWatts({ text: "ECO", color: "eco", dim: false }, true,
                { ...withGpu, gpu: { state: "active", held: false } }, 1001, true).text, "21W"))
t("panel: asleep GPU shows CPU only", () => assert.equal(
    m.gpuWatts({ ...withGpu, gpu: { state: "suspended" } }, 1001), null))

t("cpu limit: nothing", () => assert.equal(m.cpuLimitText([]), "nothing, running free"))
t("cpu limit: PL1", () => assert.equal(m.cpuLimitText(["PL1"]), "sustained power limit (PL1)"))
t("cpu limit: no reading", () => assert.equal(m.cpuLimitText(null), null))
t("sleep share", () => assert.equal(m.sleepShare({ suspended_ms: 5340, active_ms: 353729 }),
                                    "asleep 1 % of the time"))
t("sleep share below one percent", () => assert.equal(
    m.sleepShare({ suspended_ms: 1, active_ms: 1000 }), "asleep <1 % of the time"))
t("gpu cap pending", () => assert.equal(m.gpuCapText({ gr: 900, mem: 405, pending: true }),
                                        "≤ 900 MHz, VRAM 405 MHz: applied when the GPU wakes"))
t("level text: deepest caps the ceiling at 30", () => assert.match(m.levelText("deepest", 40), /ceiling 30 %/))

t("charge on", () => assert.equal(m.chargeCommand("/usr/local/bin/ecomode", true, 70),
    "pkexec /usr/local/bin/ecomode --json charge on --stop 70"))
t("charge off", () => assert.equal(m.chargeCommand("/usr/local/bin/ecomode", false, 70),
    "pkexec /usr/local/bin/ecomode --json charge off"))
t("charge: a stop level outside the menu never reaches the shell",
  () => assert.equal(m.chargeCommand("/h", true, "80; reboot"), "pkexec /h --json charge on --stop 80"))
t("battery line charging with a limit", () => assert.equal(
    m.batteryLine({ capacity: 50, status: "Charging", minutes_left: 43, limit: { stop: 80 } }),
    "50 % · charging, 43 min to 80 %"))
t("battery line held by care", () => assert.equal(
    m.batteryLine({ capacity: 80, status: "Not charging", charge_mode: "inhibit-charge" }),
    "80 % · held, not charging (Battery care)"))
t("battery line on battery", () => assert.equal(
    m.batteryLine({ capacity: 41, status: "Discharging", minutes_left: 75 }),
    "41 % · on battery, 1 h 15 min left"))
t("health", () => assert.equal(
    m.healthText({ health_pct: 78, full_wh: 65, design_wh: 83, cycles: null }),
    "78 % of design (65 / 83 Wh)"))
t("no battery", () => assert.equal(m.batteryLine(null), "no battery"))

t("gpu cap on a sleeping card is not claimed as locked", () => assert.equal(
    m.gpuCapText({ gr: 900, mem: 405, applied: true }, "suspended"),
    "≤ 900 MHz, VRAM 405 MHz: set again when the GPU wakes"))
t("gpu cap locked while awake", () => assert.equal(
    m.gpuCapText({ gr: 900, mem: null, applied: true }, "active"), "≤ 900 MHz: locked"))

// ── Battery care log and chart ──────────────────────────────────────────────
t("charge: log on", () => assert.equal(m.chargeCommand("/h", true, 80, true),
    "pkexec /h --json charge on --stop 80 --log on"))
t("charge: log off with the limit off", () => assert.equal(m.chargeCommand("/h", false, 80, false),
    "pkexec /h --json charge off --log off"))
t("charge: log not given is kept", () => assert.equal(m.chargeCommand("/h", true, 70),
    "pkexec /h --json charge on --stop 70"))
t("history command: 24 h, or 7 days, nothing else",
  () => assert.deepEqual([m.chargeHistoryCommand("/h", 168), m.chargeHistoryCommand("/h", "1; rm")],
                         ["/h --json charge-history --hours 168", "/h --json charge-history --hours 24"]))

const H = 3600
const rows = [
    [0, 60, true, "auto", "Charging", "start"],
    [H, 80, true, "inhibit-charge", "Charging", "stop"],
    [H + 10, 80, true, "inhibit-charge", "Not charging", null],
    [2 * H, 79, false, "auto", "Discharging", "unplug"],
    [3 * H, 70, false, "inhibit-charge", "Discharging", "sleep"],
    [5 * H, 68, false, "auto", "Discharging", "wake"],
    [8 * H, 50, null, null, "Unknown", "start"],
]
const s = m.chargeSeries(rows, 9 * H, 24)
t("chart: steps hold the charge until the next change, with its state", () => assert.deepEqual(
    s.steps.slice(0, 3).map(x => [x.t0, x.t1, x.cap, x.state]),
    [[0, H, 60, "charging"], [H, H + 10, 80, "charging"], [H + 10, 2 * H, 80, "held"]]))
t("chart: asleep is a band, not a line", () => {
    assert.deepEqual(s.sleeps, [{ t0: 3 * H, t1: 5 * H }])
    assert.ok(!s.steps.some(x => x.t0 === 3 * H))
})
t("chart: before a service start a reading lasts an hour at most", () =>
    assert.equal(s.steps.find(x => x.cap === 68).t1, 6 * H))
t("chart: the last reading runs to now", () => assert.equal(s.steps[s.steps.length - 1].t1, 9 * H))
t("chart: markers only for stop and resume", () =>
    assert.deepEqual(s.marks.map(x => x.event), ["stop"]))
t("chart: what is older than the window is clipped, not dropped", () => {
    const w = m.chargeSeries(rows, 29.5 * H, 24)
    assert.deepEqual([w.steps[0].t0, w.steps[0].cap], [5.5 * H, 68])
})
t("chart: nothing to show", () => assert.deepEqual(m.chargeSeries(null, 100, 24).steps, []))
t("hover: the reading in force at a moment", () =>
    assert.equal(m.chargeAt(rows, H + 5)[1], 80))
t("hover text", () => {
    const txt = m.chargeHoverText(rows[1], 24)
    assert.match(txt, /^\d\d:\d\d · 80 % · charging · charging stopped at the limit$/)
})

const D1 = 86400, oct26 = new Date(2026, 9, 26, 12).getTime() / 1000
t("health trend: nothing before a day of records", () => {
    assert.equal(m.healthTrend(null), "")
    assert.equal(m.healthTrend([[oct26, 66.53, 83]]), "")
    assert.equal(m.healthTrend([[oct26, 66.53, 83], [oct26 + 3600, 66.2, 83]]), "")
})
t("health trend: a loss, in % of design, since the first record", () =>
    assert.equal(m.healthTrend([[oct26, 66.53, 83], [oct26 + 40 * D1, 66.2, 83]]),
                 "−0.4 % of design since Oct 26"))
t("health trend: unchanged", () =>
    assert.equal(m.healthTrend([[oct26, 66.53, 83], [oct26 + 2 * D1, 66.53, 83]]),
                 "no change since Oct 26"))
t("health trend: a replaced battery starts its own trend", () =>
    assert.equal(m.healthTrend([[oct26, 66.53, 83], [oct26 + 2 * D1, 89, 90],
                                [oct26 + 4 * D1, 88.5, 90]]),
                 "\u22120.6 % of design since Oct 28"))
t("health trend: losses and gains round alike", () => {
    assert.equal(m.healthTrend([[oct26, 66.53, 83], [oct26 + 2 * D1, 66.49, 83]]),
                 "no change since Oct 26")
    assert.equal(m.healthTrend([[oct26, 66.53, 83], [oct26 + 2 * D1, 66.44, 83]]),
                 "\u22120.1 % of design since Oct 26")
})
t("health trend: a recalibration upwards shows as such", () =>
    assert.equal(m.healthTrend([[oct26, 66.53, 83], [oct26 + 2 * D1, 67.4, 83]]),
                 "+1 % of design since Oct 26"))

// ── Two-way sync with the KDE slider ────────────────────────────────────────
t("sync: Eco moves the slider to Power Save", () =>
    assert.equal(m.profileCommand("/h", "eco", "balanced", true), "/h --json set-profile power-saver"))
t("sync: Normal moves it to Balanced", () =>
    assert.equal(m.profileCommand("/h", "normal", "performance", true), "/h --json set-profile balanced"))
t("sync: already there, nothing to do", () =>
    assert.equal(m.profileCommand("/h", "eco", "power-saver", true), null))
t("sync: Max sets nothing, its hold does", () =>
    assert.equal(m.profileCommand("/h", "max", "balanced", true), null))
t("sync off: the slider is never touched", () =>
    assert.equal(m.profileCommand("/h", "eco", "balanced", false), null))
t("sync: a mode name from outside never reaches the shell", () =>
    assert.equal(m.profileCommand("/h", "eco; reboot", "balanced", true), null))
t("sync: leaving Max, the slider moves after max off, before eco on", () => {
    // A manual set drops Max's hold; before "max off" Max would end as if
    // the user had moved the slider.
    const p = "/h --json set-profile power-saver"
    assert.deepEqual(m.withProfile(["MAXOFF", "ECOON"], p, "max"), ["MAXOFF", p, "ECOON"])
    assert.deepEqual(m.withProfile(["MAXOFF"], "/h --json set-profile balanced", "max"),
                     ["MAXOFF", "/h --json set-profile balanced"])
})
t("sync: from Normal, the slider before eco on", () =>
    assert.deepEqual(m.withProfile(["ECOON"], "P", "normal"), ["P", "ECOON"]))
t("sync: nothing to set, the sequence as it was", () =>
    assert.deepEqual(m.withProfile(["ECOON"], null, "normal"), ["ECOON"]))
t("sync: a slider moved by the widget is followed back to the same mode", () => {
    // Our own profile write comes back through the follow path; it must not
    // pick a different mode than the one just chosen.
    assert.equal(m.followTarget("balanced", { mode: "normal" }), null)
    assert.equal(m.followTarget("power-saver", { mode: "eco" }), "eco")   // re-check only
})

// ── Fans left at full speed after Max ───────────────────────────────────────
t("fans: not at full speed, nothing to say", () =>
    assert.equal(m.fanHoldText({ fans: { pwm_enable: "2" } }, 1000), ""))
t("fans: Max with max fans asks for it, nothing to say", () =>
    assert.equal(m.fanHoldText({ fans: { pwm_enable: "0" }, max: { fans_state: "max" } }, 1000), ""))
t("fans: left over, with the time from the last session's record", () =>
    assert.equal(m.fanHoldText({ fans: { pwm_enable: "0" }, max: null,
                                 max_last: { fans_armed_until: 1095 } }, 1000),
                 "Left at full speed after Max: back to auto in about 95 s"))
t("fans: a running auto session that is settling gives its own time", () =>
    assert.equal(m.fanHoldText({ fans: { pwm_enable: "0" },
                                 max: { fans_state: "auto", fans_armed_until: 1030 } }, 1000),
                 "Left at full speed after Max: back to auto in about 30 s"))
t("fans: no time left in the record - only what is known", () =>
    assert.match(m.fanHoldText({ fans: { pwm_enable: "0" }, max_last: { fans_armed_until: 900 } }, 1000),
                 /usually goes back to auto about 2 min after the last Max change$/))
t("fans: no Max ever ran - not blamed on Max", () =>
    assert.equal(m.fanHoldText({ fans: { pwm_enable: "0" }, max: null, max_last: null }, 1000),
                 "Full speed, not set by this widget"))
t("fans: where automatic can be written, a running Max writes it back", () =>
    assert.equal(m.fanHoldText({ fans: { pwm_enable: "0", auto_writable: true },
                                 max: { fans_state: "auto" } }, 1000),
                 "Full speed: Max puts the fans back to automatic within 30 s"))
t("fans: where automatic can be written, with Max off - how to put them back", () =>
    assert.match(m.fanHoldText({ fans: { pwm_enable: "0", auto_writable: true }, max: null,
                                 max_last: { fans_armed_until: 1095 } }, 1000),
                 /^Full speed, not set by this widget: pkexec ecomode-max fans auto/))
t("fans: Max with max fans asks for it there too, nothing to say", () =>
    assert.equal(m.fanHoldText({ fans: { pwm_enable: "0", auto_writable: true },
                                 max: { fans_state: "max" } }, 1000), ""))

// ── Battery use while asleep ────────────────────────────────────────────────
const SL = [
    [0, 80, true, "inhibit-charge", "Not charging", "sleep"],
    [31500, 80, true, "auto", "Not charging", "wake"],          // 8 h 45 min on AC
    [40000, 74, false, "auto", "Discharging", "sleep"],
    [42400, 73, false, "auto", "Discharging", "wake"],          // 40 min on battery
    [50000, 70, false, "auto", "Discharging", "sleep"],
    [60000, 60, false, "auto", "Discharging", "start"],         // never woke
]
t("sleeps: pairs, a sleep with no wake is dropped", () =>
    assert.deepEqual(m.chargeSleeps(SL).map(s => [s.secs, s.from, s.to, s.ac]),
                     [[31500, 80, 80, true], [2400, 74, 73, false]]))
const PLUGGED = [[0, 50, false, "a", "D", "sleep"], [7200, 80, true, "a", "C", "wake"]]
t("sleeps: plugged in while asleep - levels only, no rate", () =>
    assert.equal(m.lastSleepText(PLUGGED), "2 h 00 min on battery, then AC, 50 % → 80 %"))
t("drain: a sleep that ended on AC is not counted as drain", () =>
    assert.equal(m.sleepDrainText(PLUGGED.concat([
        [10000, 80, false, "a", "D", "sleep"], [13600, 79, false, "a", "D", "wake"]])),
        "1 %/h over 1 sleep on battery"))
t("last sleep: on battery, with its rate", () =>
    assert.equal(m.lastSleepText(SL), "40 min on battery, −1 % (1.5 %/h)"))
t("last sleep: on AC, the levels", () =>
    assert.equal(m.lastSleepText(SL.slice(0, 2)), "8 h 45 min on AC, 80 % → 80 %"))
t("last sleep: too short for a rate", () =>
    assert.equal(m.lastSleepText([[0, 50, false, "a", "D", "sleep"], [600, 50, false, "a", "D", "wake"]]),
                 "10 min on battery, ±0 %"))
t("drain: under an hour of sleeps on battery says nothing", () =>
    assert.equal(m.sleepDrainText(SL), ""))
t("drain: weighted by time over the sleeps on battery", () =>
    assert.equal(m.sleepDrainText(SL.concat([
        [70000, 60, false, "a", "D", "sleep"], [77200, 52, false, "a", "D", "wake"]])),
        "3.4 %/h over 2 sleeps on battery"))
t("sleeps: no history", () => assert.equal(m.lastSleepText(null), ""))

// ── the settings' power limits ──
t("eco on carries the settings' limits, in range only", () => {
    assert.deepEqual(m.commandsFor("normal", "eco", { ...o, pl1: 30, pl2: 40, tau: 28 }),
                     [ECO_ON + " --pl1 30 --pl2 40 --tau 28"])
    assert.deepEqual(m.commandsFor("normal", "eco", { ...o, pl1: 0, pl2: 0, tau: 0 }), [ECO_ON])
    assert.deepEqual(m.commandsFor("normal", "eco", { ...o, pl1: 300, pl2: 3, tau: 500 }), [ECO_ON])
    assert.deepEqual(m.commandsFor("normal", "eco", { ...o, pl1: "30; reboot", tau: 28 }),
                     [ECO_ON + " --tau 28"])
    // a burst limit under the sustained one would be refused by the helper
    assert.deepEqual(m.commandsFor("normal", "eco", { ...o, pl1: 40, pl2: 30 }),
                     [ECO_ON + " --pl1 40"])
})
t("eco -> eco with the limits already applied asks for nothing; changed ones re-apply", () => {
    const running = { mode: "eco", level: "gentle", max_pct: 40, limits: { pl1: 30, tau: 28 } }
    assert.deepEqual(m.commandsFor("eco", "eco", { ...o, pl1: 30, tau: 28, status: running }), [])
    assert.deepEqual(m.commandsFor("eco", "eco", { ...o, pl1: 30, tau: 30, status: running }),
                     [ECO_ON + " --pl1 30 --tau 30"])
    // the settings cleared while Eco runs with limits: re-apply the level's own
    assert.deepEqual(m.commandsFor("eco", "eco", { ...o, status: running }), [ECO_ON])
    // no limits anywhere: applied as asked
    assert.deepEqual(m.commandsFor("eco", "eco", { ...o, status: { ...running, limits: {} } }), [])
    assert.deepEqual(m.commandsFor("eco", "eco", { ...o, pl1: 30, status: { ...running, limits: {} } }),
                     [ECO_ON + " --pl1 30"])
    assert.equal(m.sameLimits({ pl1: 30 }, { pl1: 30, pl2: undefined }), true)
    assert.equal(m.sameLimits({ pl1: 30, pl2: null }, { pl1: 30 }), true)
    assert.equal(m.limitsText({ pl1: 30, pl2: 40, tau: 28 }), "PL1 30 W · PL2 40 W · Tau 28 s")
    assert.equal(m.limitsText({}), "")
    assert.equal(m.knobValue({ knobs: [{ id: "rapl_tau", current_human: "28 s" }] }, "rapl_tau"), "28 s")
    assert.equal(m.knobValue({}, "rapl_tau"), "")
})
// ── the dGPU through LACT ──
t("max on selects the LACT profile, quoted for the shell", () => {
    assert.deepEqual(m.commandsFor("normal", "max", { ...o, gpuProfile: "Over clock" }),
                     [MAX_ON + " --gpu-profile 'Over clock'"])
    assert.deepEqual(m.commandsFor("normal", "max", { ...o, gpuProfile: "default", gpuBoost: true }),
                     [MAX_ON + " --gpu-boost --gpu-profile 'default'"])
    assert.deepEqual(m.commandsFor("normal", "max", { ...o, gpuProfile: "" }), [MAX_ON])
    assert.deepEqual(m.commandsFor("normal", "max", { ...o, gpuProfile: "x'; reboot #" }),
                     [MAX_ON + " --gpu-profile 'x'\\''; reboot #'"])
    assert.equal(m.shellQuote("a b"), "'a b'")
})
t("GPU clocks card lines", () => {
    const lact = { available: true, profiles: ["Overclock", "Quiet"], current: "Overclock",
                   auto_switch: false,
                   clocks: { core: { current: 100, min: -1000, max: 1000 },
                             mem: { current: 180, min: -2000, max: 6000 } } }
    assert.equal(m.lactSummary(lact), "profile Overclock · core +100 MHz · memory +180 MHz")
    assert.equal(m.lactSummary({ ...lact, current: null, auto_switch: true, clocks: null }),
                 "profile default (auto)")
    assert.equal(m.lactSummary({ available: false, error: "lactd: Connection refused" }),
                 "lactd: Connection refused")
    assert.equal(m.lactSummary(null), "")
    assert.equal(m.offsetText(-50), "−50 MHz")
    assert.equal(m.offsetText(0), "0 MHz")
    assert.equal(m.offsetText(null), "")
    assert.equal(m.lactProfileName(null), "default")
    assert.deepEqual(m.gpuProfileChoices(lact),
                     [{ id: "", text: "Leave as is" }, { id: "default", text: "LACT default" },
                      { id: "Overclock", text: "Overclock" }, { id: "Quiet", text: "Quiet" }])
    assert.equal(m.lactClocksNote(lact, { runtime_status: "active" }), "")
    assert.equal(m.lactClocksNote({ ...lact, clocks: null }, { runtime_status: "suspended" }),
                 "The offsets are read while the GPU is awake; it is asleep now.")
    assert.equal(m.lactClocksNote({ ...lact, clocks: { error: "lactd: x" } }, {}), "lactd: x")
    assert.equal(m.gpuLimitText({ limit_w: 115, max_limit_w: 140 }),
                 "115 W, set by the vBIOS · Dynamic Boost to 140 W")
    assert.equal(m.gpuLimitText({ limit_w: 115, max_limit_w: 115 }), "115 W, set by the vBIOS")
    assert.equal(m.gpuLimitText({ power_w: 19 }), "")
})

console.log(`\n${passed} passed`)
