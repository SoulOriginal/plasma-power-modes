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

console.log(`\n${passed} passed`)
