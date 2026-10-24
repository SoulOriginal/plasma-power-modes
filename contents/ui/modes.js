.pragma library
// SPDX-License-Identifier: GPL-2.0-or-later

// Pure helpers for the QML files. No QML types in here, so node can test
// them: see tests/test_modes.mjs.

// Fan modes inside Max, as ecomode-max takes them. The value ends up in a
// shell command line, so anything else falls back to the firmware's curve.
var FAN_MODES = ["auto", "max", "boost"]
var LEVELS = ["gentle", "deep", "deepest"]

// Shell commands taking the machine from one mode to another, in order.
// Both helpers run through pkexec, and polkit lets the active local session
// run them without a password. Every value is checked here as well: the
// command is a shell line.
function commandsFor(from, to, o) {
    var level = LEVELS.indexOf(o.level) >= 0 ? o.level : "gentle"
    var pct = Math.max(20, Math.min(100, Math.round(Number(o.maxPct)) || 40))
    var ecoOn = "pkexec " + o.helper + " --json on --level " + level +
                " --max-pct " + pct
    var ecoOff = "pkexec " + o.helper + " --json off"
    var fans = FAN_MODES.indexOf(o.fans) >= 0 ? o.fans : "auto"
    var maxOn = "pkexec " + o.maxHelper + " --json on --fans " + fans +
                (o.gpuBoost ? " --gpu-boost" : "")
    var maxOff = "pkexec " + o.maxHelper + " --json off"

    if (from === to) {
        // Re-selecting the current mode re-applies it with the current
        // settings; both helpers treat an unchanged request as a no-op, and
        // Eco puts back whatever the firmware undid. An Eco known to be
        // applied as asked (o.status) needs no call at all - unless the
        // caller knows better (o.force: the profile just changed, and the
        // firmware may rewrite the limits after the status was read).
        if (to === "eco") return !o.force && ecoApplied(o.status, o) ? [] : [ecoOn]
        if (to === "max") return [maxOn]
        return []
    }
    if (to === "normal") return from === "eco" ? [ecoOff] : [maxOff]
    if (to === "max") return [maxOn]              // ecomode-max restores Eco itself
    return from === "max" ? [maxOff, ecoOn] : [ecoOn]
}

// Whether the status shows Eco on with exactly this level and ceiling, and
// every knob of the level still at Eco's value (a profile change makes the
// firmware rewrite the power limits under it).
function ecoApplied(st, o) {
    if (!st || st.mode !== "eco" || st.level !== o.level || st.max_pct !== o.maxPct)
        return false
    return !ecoDrifted(st)
}

function ecoDrifted(st) {
    var knobs = (st && st.knobs) || []
    for (var i = 0; i < knobs.length; i++) {
        var k = knobs[i]
        if (k.in_level && !k.managed && k.eco_target !== null &&
                k.eco_target !== undefined && !k.at_eco)
            return true
    }
    return false
}

// Following the KDE power profile: the mode the widget should be in for
// `profile`, or null to leave things as they are. `st` is a full status read
// after the change settled.
var PROFILE_MODES = { "power-saver": "eco", "balanced": "normal", "performance": "max" }

function followTarget(profile, st) {
    var target = PROFILE_MODES[profile]
    if (!target || !st) return null
    var mode = st.mode || (st.on === true ? "eco" : "normal")
    if (mode === "max") {
        var m = st.max || {}
        // Paused on battery Max has let go of its hold, and the profile
        // underneath shows through; that must not end it.
        if (m.state === "paused") return null
        // performance while Max runs is Max's own hold.
        if (target === "max") return null
    }
    if (target === "max" && st.max_supported !== true) target = "normal"
    // Eco again on a profile change: the firmware has likely undone it.
    if (target === mode && target !== "eco") return null
    return target
}

// What the panel shows. `color` is a key the QML maps to a real colour.
function compactLabel(st, hasError) {
    var mode = st.mode || (st.on === true ? "eco" : "normal")
    var res = { text: "off", color: "off", dim: false }
    if (mode === "eco") {
        res = { text: "ECO", color: "eco", dim: false }
    } else if (mode === "max") {
        var m = st.max || {}
        if (m.state === "paused")
            res = { text: "MAX ⏸", color: "max", dim: true }
        else
            res = { text: m.fans === "max" ? "MAX·F" : m.fans === "boost" ? "MAX·B" : "MAX",
                    color: "max", dim: false }
    }
    if (hasError) res.color = "error"
    return res
}

// Middle click flips between Normal and one other mode.
function middleClickTarget(mode, pref, lastMode) {
    if (mode !== "normal") return "normal"
    if (pref === "eco" || pref === "max") return pref
    return lastMode === "eco" ? "eco" : "max"
}

function maxLine(st, nowSec) {
    var m = st.max
    if (!m) return ""
    if (m.state === "paused") {
        if (m.pause_reason !== "fans settling") return "paused: on battery, resumes on AC"
        // Max stays silent until the EC's own timeout hands the fans back.
        var left = typeof m.settle_until === "number"
                   ? " (~" + Math.max(0, Math.round(m.settle_until - nowSec)) + " s)" : ""
        return "paused: fans returning to automatic" + left
    }
    var parts = ["holding performance"]
    if (m.fans_state === "max") parts.push("fans max")
    if (m.last_tick)
        parts.push("keep-alive " + Math.max(0, Math.round(nowSec - m.last_tick)) + " s ago")
    var f = st.fans || {}
    if (f.rpm && f.rpm.length) parts.push("fans " + f.rpm.join("/") + " RPM")
    if (st.pkg_temp_c !== null && st.pkg_temp_c !== undefined)
        parts.push(st.pkg_temp_c + " °C")
    var line = parts.join(" · ")
    if (m.last_tick_ok === false && m.last_error) line += ": " + m.last_error
    return line
}

function endedLine(last, nowSec) {
    if (!last || !last.reason) return ""
    // Only a missing timestamp means "now"; 0 is a real one.
    var endedAt = last.ended_at !== null && last.ended_at !== undefined
                  ? last.ended_at : nowSec
    var minutes = Math.round((nowSec - endedAt) / 60)
    var when = minutes < 1 ? "just now" : minutes + " min ago"
    return "Max ended " + when + ": " + last.reason
}

// A watts record goes stale once the sampler has missed a few rounds: it
// stopped, crashed or the machine slept. An old number shown as current
// would be worse than none.
var STALE_ROUNDS = 3

function wattsFresh(entry, nowSec) {
    if (!entry || entry.cpu_w === null || entry.cpu_w === undefined) return false
    var interval = entry.interval || 3
    return nowSec - (entry.at || 0) <= STALE_ROUNDS * interval + 1
}

function formatWatts(w) {
    return Math.round(w) + "W"
}

// The GPU's draw from a fresh sampler record, or null: the sampler reads it
// only while another process keeps the GPU awake anyway.
function gpuWatts(entry, nowSec) {
    if (!wattsFresh(entry, nowSec)) return null
    var g = entry.gpu
    if (!g || g.held !== true || typeof g.power_w !== "number") return null
    return g.power_w
}

// The panel label with watts on: the number replaces the mode text, the
// dot's colour still tells the mode, and "—" stands in for a missing reading.
// With withGpu, an awake and busy dGPU adds its draw: "21·19W".
function withWatts(label, showWatts, entry, nowSec, withGpu) {
    if (!showWatts) return label
    var text = "—"
    if (wattsFresh(entry, nowSec)) {
        var gpu = withGpu ? gpuWatts(entry, nowSec) : null
        text = gpu === null ? formatWatts(entry.cpu_w)
                            : Math.round(entry.cpu_w) + "·" + formatWatts(gpu)
    }
    return { text: text, color: label.color, dim: label.dim }
}

// ── Popup text ─────────────────────────────────────────────────────────────

var CPU_LIMIT_WORDS = {
    "PL1": "sustained power limit (PL1)",
    "PL2": "burst power limit (PL2)",
    "thermal": "temperature",
    "PROCHOT": "PROCHOT (platform asked to slow down)",
    "avg thermal": "average temperature",
    "VR thermal": "voltage regulator temperature",
    "VR current": "voltage regulator current",
    "electrical": "electrical design limit",
    "turbo limit": "turbo ratio limit",
    "turbo attenuation": "turbo attenuation",
    "residency": "C-state residency"
}

// What holds the CPU back, from the sampler's MSR reading: null when there
// is no reading (sampler off), "nothing" when nothing does.
function cpuLimitText(limits) {
    if (limits === null || limits === undefined) return null
    if (!limits.length) return "nothing, running free"
    return limits.map(function(l) { return CPU_LIMIT_WORDS[l] || l }).join(", ")
}

// Share of the uptime the dGPU spent powered down, e.g. "asleep 2 % of the time".
function sleepShare(g) {
    if (!g || typeof g.suspended_ms !== "number" || typeof g.active_ms !== "number")
        return ""
    var total = g.suspended_ms + g.active_ms
    if (total <= 0) return ""
    var pct = 100 * g.suspended_ms / total
    return "asleep " + (pct > 0 && pct < 1 ? "<1" : Math.round(pct)) + " % of the time"
}

function gpuStateText(g) {
    if (!g || !g.present) return "not found"
    if (g.runtime_status === "suspended") return "asleep (D3cold)"
    if (g.runtime_status === "active") return "awake"
    return g.runtime_status || "?"
}

// The Eco clock cap as the popup shows it.
function gpuCapText(caps, runtime) {
    if (!caps) return ""
    var s = "≤ " + caps.gr + " MHz" + (caps.mem ? ", VRAM " + caps.mem + " MHz" : "")
    if (caps.error) return s + ": " + caps.error
    // A sleeping card may have dropped the lock; the guard sets it on wake.
    if (runtime === "suspended") return s + ": set again when the GPU wakes"
    if (caps.applied) return s + ": locked"
    if (caps.pending) return s + ": applied when the GPU wakes"
    return s
}

// The level descriptions, shared by the popup and the settings.
function levelText(level, maxPct) {
    if (level === "deepest")
        return "CPU 20 W sustained, 25 W bursts, ceiling " + Math.min(maxPct, 30) +
               " %; dGPU ≤ 900 MHz with its memory at the lowest clock. " +
               "Everything still works, slowly. For a machine left alone " +
               "or a battery that has to last."
    if (level === "deep")
        return "CPU 25 W sustained, 35 W bursts, ceiling " + maxPct +
               " %; dGPU ≤ 1500 MHz. Nothing breaks, heavy work runs slower."
    return "Caps sustained CPU power only (35 W). Bursts keep their full " +
           "budget, so interactive work feels unchanged. The dGPU is untouched."
}

// Collapsed with Max on, the widget reads the keeper's own max.json with
// `cat` every 30 s instead of running the Python status. The keeper rewrites
// it every 30 s, so a last tick older than three rounds and a bit means it
// is dead or stuck.
var MAX_STALE_SECS = 3 * 30 + 5
var MAX_KEYS = ["state", "pause_reason", "fans", "since", "last_tick",
                "last_tick_ok", "last_error", "fans_state", "settle_until",
                "fans_armed_until"]

// The file's text as status.max (what ecomode's max_public() gives), or null
// when it is missing, unparsable or stale: then only a full status can tell
// what happened.
function maxFromFile(text, nowSec) {
    var st = null
    try { st = JSON.parse(text) } catch (e) { return null }
    if (!st || typeof st !== "object" || Array.isArray(st)) return null
    if (typeof st.last_tick !== "number" || nowSec - st.last_tick > MAX_STALE_SECS)
        return null
    var out = { active: true }
    for (var i = 0; i < MAX_KEYS.length; i++) {
        var v = st[MAX_KEYS[i]]
        out[MAX_KEYS[i]] = v === undefined ? null : v
    }
    return out
}

// A new status with Max taken from the file; a new object, so bindings
// notice. The file has no fan speeds and no temperature, and the last full
// status's would only grow older, so they are dropped rather than shown as
// current; opening the popup brings the full status back.
function statusWithMax(st, max) {
    var out = {}
    for (var k in st) out[k] = st[k]
    out.mode = "max"
    out.max = max
    out.fans = { available: (st.fans || {}).available, rpm: [], pwm_enable: null }
    out.pkg_temp_c = null
    return out
}

// ── Battery ─────────────────────────────────────────────────────────────────

var CHARGE_STOPS = [60, 70, 80, 90]

// The command for the charge limit; any other stop level falls back to 80.
function chargeCommand(helper, on, stop) {
    if (!on) return "pkexec " + helper + " --json charge off"
    var s = CHARGE_STOPS.indexOf(stop) >= 0 ? stop : 80
    return "pkexec " + helper + " --json charge on --stop " + s
}

function minutesText(m) {
    if (typeof m !== "number" || m < 0) return ""
    var h = Math.floor(m / 60), mm = m % 60
    return h > 0 ? h + " h " + (mm < 10 ? "0" : "") + mm + " min" : mm + " min"
}

// "50 % · charging, 43 min to 80 %" and the like.
function batteryLine(b) {
    if (!b) return "no battery"
    var parts = [b.capacity + " %"]
    var left = minutesText(b.minutes_left)
    if (b.status === "Discharging") parts.push("on battery" + (left ? ", " + left + " left" : ""))
    else if (b.status === "Charging")
        parts.push("charging" + (left ? ", " + left + " to " +
                                 (b.limit ? b.limit.stop : 100) + " %" : ""))
    else if (b.charge_mode === "inhibit-charge") parts.push("held, not charging (Battery care)")
    else if (b.status) parts.push(b.status.toLowerCase())
    return parts.join(" · ")
}

function healthText(b) {
    if (!b || typeof b.health_pct !== "number") return ""
    return b.health_pct + " % of design (" + b.full_wh + " / " + b.design_wh + " Wh)" +
           (typeof b.cycles === "number" ? " · " + b.cycles + " cycles" : "")
}

