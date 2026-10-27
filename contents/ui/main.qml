// SPDX-License-Identifier: GPL-2.0-or-later
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.plasma5support as Plasma5Support
import "."
import "modes.js" as Modes

PlasmoidItem {
    id: root

    readonly property string helper: "/usr/local/bin/ecomode"
    readonly property string maxHelper: "/usr/local/bin/ecomode-max"

    property var status: ({})
    // Older helpers report no `mode`; fall back to the Eco flag they have.
    readonly property string mode: status.mode || (status.on === true ? "eco" : "normal")
    property bool busy: false
    // Two slots, so that a refresh cannot wipe what an action reported: every
    // action ends with a refresh, and opening the popup refreshes too. An
    // action's error stays until the next action starts or the popup closes;
    // statusError is set and cleared by refresh() alone.
    property string actionError: ""
    property string statusError: ""
    readonly property string lastError: actionError || statusError
    property var lastTest: null
    // Why Max runs without Dynamic Boost, from its last start.
    property string gpuBoostError: ""

    readonly property int maxPct: Plasmoid.configuration.maxPct || 40
    readonly property string level: Plasmoid.configuration.level || "gentle"
    readonly property int pollSeconds: Plasmoid.configuration.pollSeconds || 10
    // Fans inside Max: "auto", "max" or "boost". Until one is picked, the
    // old fansMax switch still decides.
    readonly property string fansMode: Plasmoid.configuration.fansMode
        || (Plasmoid.configuration.fansMax === true ? "max" : "auto")
    readonly property string middleClick: Plasmoid.configuration.middleClick || "last"
    readonly property string lastMode: Plasmoid.configuration.lastMode || "max"
    readonly property int maxPollSeconds: 30
    readonly property string maxFile: "/run/ecomode/max.json"
    readonly property string wattsFile: "/run/ecomode/watts"
    readonly property bool showWatts: Plasmoid.configuration.showWatts === true
    readonly property int wattsSeconds: Plasmoid.configuration.wattsSeconds || 3
    readonly property bool wattsGpu: Plasmoid.configuration.wattsGpu !== false
    readonly property bool followProfile: Plasmoid.configuration.followProfile === true
    // Two-way: with following on, a mode picked here also moves the KDE slider.
    readonly property bool syncProfile: followProfile && Plasmoid.configuration.syncProfile === true
    readonly property bool gpuBoost: Plasmoid.configuration.gpuBoost !== false
    // Last record from the sampler, {cpu_w, battery_w, cpu_limit, gpu, at,
    // interval}, or null.
    property var watts: null

    Plasmoid.icon: mode === "eco" ? "battery-profile-powersave"
                 : mode === "max" ? "battery-profile-performance"
                 : "battery-profile-balanced"

    toolTipMainText: mode === "eco" ? "Power: Eco"
                   : mode === "max" ? "Power: Max"
                   : "Power: Normal"
    toolTipSubText: {
        if (lastError) return "error: " + lastError
        if (!status.knobs) return "loading..."
        const parts = []
        const now = Date.now() / 1000
        if (showWatts && Modes.wattsFresh(watts, now)) {
            parts.push("CPU " + Math.round(watts.cpu_w) + " W")
            const gw = Modes.gpuWatts(watts, now)
            if (gw !== null) parts.push("GPU " + Math.round(gw) + " W")
        }
        if (mode === "max") {
            parts.push(Modes.maxLine(status, Date.now() / 1000))
            return parts.join("   ")
        }
        if (status.ppd_profile) parts.push("profile: " + status.ppd_profile)
        if (status.cpu_avg_mhz) parts.push("CPU " + status.cpu_avg_mhz + " MHz")
        const g = status.dgpu
        if (g && g.present) parts.push("dGPU " + g.runtime_status +
            (g.power_w !== null && g.power_w !== undefined ? " " + g.power_w + "W" : ""))
        return parts.join("   ")
    }

    preferredRepresentation: compactRepresentation
    compactRepresentation: Component { CompactRepresentation {} }
    fullRepresentation: Component { FullRepresentation {} }

    // ── Command runner ───────────────────────────────────────────────────────
    Plasma5Support.DataSource {
        id: runner
        engine: "executable"
        connectedSources: []
        // Callbacks per command line. The engine runs a line once while it
        // is in flight, so a second caller of the same line waits for the
        // same result instead of replacing the first one's callback.
        property var callbacks: ({})

        onNewData: function(source, data) {
            disconnectSource(source)
            const cbs = callbacks[source] || []
            delete callbacks[source]
            for (const cb of cbs)
                cb(data["exit code"] || 0,
                   (data["stdout"] || "").trim(),
                   (data["stderr"] || "").trim())
        }
    }

    function runCmd(cmd, cb) {
        const pending = runner.callbacks[cmd]
        if (pending) {
            if (cb) pending.push(cb)
            return
        }
        runner.callbacks[cmd] = cb ? [cb] : []
        runner.connectSource(cmd)
    }

    // ── State polling ────────────────────────────────────────────────────────
    // The routine poll is deliberately cheap: sysfs reads only. The --deep
    // query walks /proc and calls nvidia-smi, which *wakes the dGPU* - so it
    // runs only while the popup is actually open, never on the timer.
    function refresh() {
        const deep = root.expanded ? " --deep" : ""
        runCmd(helper + " --json status --level " + level + deep, function(code, out, err) {
            if (code !== 0) {
                statusError = err || ("status exited " + code)
                return
            }
            try {
                status = JSON.parse(out)
                statusError = ""
            } catch (e) {
                statusError = "bad JSON from helper"
            }
        })
    }

    // Collapsed with Max on, the poll reads the keeper's own state file with
    // `cat`: no Python, and no powerprofilesctl behind it. A missing,
    // unreadable or stale file (Max ended, or its keeper died) gets one full
    // status to find out what happened.
    function readMax() {
        runCmd("cat " + maxFile, function(code, out, err) {
            if (root.expanded) return   // the popup polls the full status itself
            const m = Modes.maxFromFile(code === 0 ? out : "", Date.now() / 1000)
            if (m === null) refresh()
            else status = Modes.statusWithMax(status, m)
        })
    }

    // ── Mode changes ─────────────────────────────────────────────────────────
    // Every transition goes through here. The command table lives in
    // modes.js so it is tested outside QML.
    function setMode(target, force) {
        if (busy) return
        if (target === "eco" || target === "max") Plasmoid.configuration.lastMode = target
        const cmds = Modes.commandsFor(mode, target, {
            helper: helper, maxHelper: maxHelper, level: level,
            maxPct: maxPct, fans: fansMode, gpuBoost: gpuBoost, status: status,
            force: force === true
        })
        // Two-way sync: the slider first. A profile write makes the firmware
        // reset the CPU limits, so Eco has to come after it, not before. The
        // mode change goes ahead even if the daemon is not there.
        const profile = Modes.profileCommand(target, kdeProfile, syncProfile)
        if (profile === null) {
            runSequence(cmds)
            return
        }
        busy = true
        runCmd(profile, function() {
            busy = false
            runSequence(cmds)
        })
    }

    // Middle click on the panel.
    function toggle() {
        setMode(Modes.middleClickTarget(mode, middleClick, lastMode))
    }

    // Changing level while Eco is on is a single privileged call: the helper
    // reverts the previous level to the recorded originals before applying
    // the new one, so one pkexec prompt instead of two.
    function setLevel(newLevel) {
        if (newLevel === level || busy) return
        Plasmoid.configuration.level = newLevel
        if (mode === "eco") setMode("eco")
        else refresh()
    }

    // The fan choice is remembered; while Max runs it is applied at once
    // (ecomode-max restarts its unit with the new setting).
    function setFans(fans) {
        if (busy) return
        Plasmoid.configuration.fansMode = fans
        if (mode === "max") setMode("max")
    }

    // Dynamic Boost likewise: while Max runs, ecomode-max starts or stops
    // nvidia-powerd without touching the keep-alive.
    function setGpuBoost(on) {
        if (busy) return
        Plasmoid.configuration.gpuBoost = on
        if (mode === "max") Qt.callLater(setMode, "max")
    }

    // Battery care: the helper saves the stop level and runs the root service
    // that keeps charging between stop-5 and stop.
    function setChargeLimit(on, stop) {
        if (busy) return
        runSequence([Modes.chargeCommand(helper, on, stop)])
    }

    // Battery care log: decisions to the journal and a history for the
    // chart. Saved by the root helper next to the stop level.
    function setChargeLog(on) {
        if (busy) return
        const bat = status.battery || {}
        runSequence([Modes.chargeCommand(helper, !!bat.limit, bat.limit_stop || 80, on)])
    }

    // The chart's history: read only while the popup is open and the log is
    // on - one small file, no root, once a minute.
    property var chargeHistory: null
    property int chargeHours: 24
    readonly property bool chargeLogOn: !!(status.battery && status.battery.limit
                                           && status.battery.limit_log)

    function refreshChargeHistory() {
        if (!chargeLogOn) {
            chargeHistory = null
            return
        }
        runCmd(Modes.chargeHistoryCommand(helper, chargeHours), function(code, out, err) {
            if (code !== 0) return
            try { chargeHistory = JSON.parse(out) } catch (e) {}
        })
    }

    onChargeHoursChanged: refreshChargeHistory()
    onChargeLogOnChanged: refreshChargeHistory()

    Timer {
        interval: 60000
        running: root.expanded && root.chargeLogOn
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshChargeHistory()
    }

    function setFollowProfile(on) {
        Plasmoid.configuration.followProfile = on
    }

    function setSyncProfile(on) {
        Plasmoid.configuration.syncProfile = on
    }

    // ── Following the KDE power profile ──────────────────────────────────────
    // power-profiles-daemon announces every profile change on the system bus;
    // Plasma's D-Bus QML module turns that into a property, so this costs
    // nothing until the profile actually changes. Changes come from the KDE
    // slider, Fn+P, and from Max's own hold - followTarget() tells them apart
    // on a fresh status.
    Loader {
        id: profileWatcher
        source: "ProfileWatcher.qml"
    }
    readonly property string kdeProfile: profileWatcher.item ? profileWatcher.item.profile : ""
    // Without the D-Bus module (older Plasma 6) there is nothing to follow.
    readonly property bool canFollow: profileWatcher.status === Loader.Ready

    // A profile write also makes the firmware reset the CPU power limits, so
    // Eco is checked after any change - followed or not.
    onKdeProfileChanged: if (kdeProfile !== "") profileSettle.restart()
    onFollowProfileChanged: if (followProfile && kdeProfile !== "") profileSettle.restart()

    Timer {
        id: profileSettle
        // Lets the daemon, the firmware and Max's keeper finish first.
        interval: 2000
        onTriggered: root.onProfileSettled()
    }

    // Max's hold, taken or let go by the widget itself, moves the profile
    // too; for a few seconds after a Max command a change is ours, not the
    // user's, and is not followed.
    property real profileQuietUntil: 0

    function onProfileSettled() {
        if (busy || samplerBusy) {
            profileSettle.restart()       // one thing at a time
            return
        }
        const follow = followProfile && Date.now() >= profileQuietUntil
        // A fresh status first, also when not following: Eco may have been
        // turned on from the command line while the widget was collapsed.
        const deep = root.expanded ? " --deep" : ""
        runCmd(helper + " --json status --level " + level + deep, function(code, out, err) {
            let st = null
            try { st = JSON.parse(out) } catch (e) { return }
            status = st
            const target = follow ? Modes.followTarget(kdeProfile, st)
                                  : (st.mode === "eco" ? "eco" : null)
            if (target === null) return
            if (busy) {                   // a click got in first; look again
                profileSettle.restart()
                return
            }
            // Eco again: always ask the helper. The firmware may rewrite the
            // limits after this status was read; the helper's check is cheap
            // and needs no password.
            setMode(target, target === "eco")
        })
    }

    // Runs privileged commands one after another and stops at the first
    // failure, so "max off, then eco on" never leaves both half-applied.
    function runSequence(cmds) {
        if (cmds.length === 0) {
            refresh()
            return
        }
        busy = true
        actionError = ""
        lastTest = null
        runCmd(cmds[0], function(code, out, err) {
            if (cmds[0].indexOf(maxHelper) >= 0) profileQuietUntil = Date.now() + 4000
            // pkexec returns 126/127 when the user dismisses the auth dialog.
            if (code === 126 || code === 127) {
                busy = false
                actionError = "authentication cancelled"
                refresh()
                return
            }
            let res = null
            try { res = JSON.parse(out) } catch (e) {}
            if (res && res.selftest) lastTest = res.selftest
            if (res && res.gpu_boost_error) gpuBoostError = res.gpu_boost_error
            else if (res && res.gpu_boost === true) gpuBoostError = ""
            if (!res || !res.ok) {
                busy = false
                actionError = res ? describeFailure(res) : (err || ("exited " + code))
                refresh()
                return
            }
            if (cmds.length > 1) {
                runSequence(cmds.slice(1))
            } else {
                busy = false
                refresh()
            }
        })
    }

    function describeFailure(res) {
        if (res.reason) return res.reason
        if (res.failed && res.failed.length) {
            return "could not set: " + res.failed.map(function(f) { return f.id }).join(", ")
        }
        if (res.selftest && !res.selftest.pass) {
            const bad = (res.selftest.checks || []).filter(function(c) { return !c.ok })
            return "verify failed: " + bad.map(function(c) { return c.label }).join(", ")
        }
        if (res.msg) return res.msg
        return "unknown failure"
    }

    function runSelfTest() {
        actionError = ""
        runCmd(helper + " --json selftest", function(code, out, err) {
            try { lastTest = JSON.parse(out) } catch (e) { actionError = "selftest parse error" }
        })
    }

    // ── Watts ────────────────────────────────────────────────────────────────
    // The root sampler (ecomode-max watts) runs only while "Show watts" is on.
    // The widget then reads one small file every few seconds with `cat`: no
    // Python and no nvidia-smi in the loop, and nothing at all while it is off.

    // What the sampler was last asked to be: "off", or the interval it was
    // started with. Nothing needs stopping at startup: the sampler does not
    // outlive the session.
    property string samplerSent: "off"
    property bool samplerBusy: false

    // Every wish goes through here, one command at a time. Apply in the
    // settings sets showWatts and then wattsSeconds, and a quick off/on can
    // come while a command still runs; sent at once, such commands raced in
    // the helper, and the executable engine would drop a repeat of a command
    // still in flight. Once a command returns, whatever is wanted by then is
    // compared with what was sent, and only a difference sends another.
    function syncSampler() {
        if (samplerBusy) return
        const want = showWatts ? String(wattsSeconds) : "off"
        if (want === samplerSent) return
        samplerBusy = true
        samplerSent = want
        actionError = ""
        if (want === "off") watts = null
        runCmd("pkexec " + maxHelper + " --json watts "
               + (want === "off" ? "off" : "on --interval " + want),
               function(code, out, err) {
            samplerBusy = false
            reportSampler(code, out, err)
            Qt.callLater(syncSampler)
        })
    }

    function reportSampler(code, out, err) {
        if (code === 126 || code === 127) {
            actionError = "authentication cancelled"
            return
        }
        try {
            const res = JSON.parse(out)
            if (!res.ok) actionError = describeFailure(res)
        } catch (e) {
            if (code !== 0) actionError = err || ("watts helper exited " + code)
        }
    }

    function readWatts() {
        runCmd("cat " + wattsFile, function(code, out, err) {
            // A read still out when watts were switched off would bring back
            // a figure nothing refreshes any more.
            if (!showWatts) return
            if (code !== 0 || out === "") {
                watts = null
                return
            }
            try { watts = JSON.parse(out) } catch (e) { watts = null }
        })
    }

    function setShowWatts(on) {
        Plasmoid.configuration.showWatts = on
    }

    onShowWattsChanged: Qt.callLater(syncSampler)
    onWattsSecondsChanged: Qt.callLater(syncSampler)

    Timer {
        interval: root.wattsSeconds * 1000
        running: root.showWatts
        repeat: true
        triggeredOnStart: true
        onTriggered: root.readWatts()
    }

    // ── Idle cost ────────────────────────────────────────────────────────────
    // Collapsed in the panel the widget runs nothing unless Max is on or
    // "Show watts" is on. Max can end by itself (battery pause, a hand-picked
    // profile, logout), so while it runs `cat` of max.json every 30 s keeps
    // the panel label honest (readMax); watts are the `cat` timer above.
    // Otherwise state is read once at startup and after each action, and the
    // full status is polled only while the popup is open.
    Timer {
        interval: (root.expanded ? root.pollSeconds : root.maxPollSeconds) * 1000
        running: root.expanded || root.mode === "max"
        repeat: true
        triggeredOnStart: root.expanded
        onTriggered: root.expanded ? root.refresh() : root.readMax()
    }

    onExpandedChanged: {
        if (root.expanded) refresh()   // upgrade to the --deep view immediately
        else actionError = ""     // it has been seen
    }

    Component.onCompleted: {
        refresh()
        Qt.callLater(syncSampler)   // starts the sampler if watts are shown
    }
}
