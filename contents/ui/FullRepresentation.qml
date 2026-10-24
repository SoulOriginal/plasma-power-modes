// SPDX-License-Identifier: GPL-2.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PC3
import "."
import "modes.js" as Modes

Item {
    id: full
    implicitWidth: Kirigami.Units.gridUnit * 26
    implicitHeight: Kirigami.Units.gridUnit * 36

    readonly property var st: root.status
    readonly property var dgpu: st.dgpu || ({})
    readonly property var cpu: st.cpu || ({})
    readonly property var fans: st.fans || ({})
    // Fans inside Max: what actually runs, not only the remembered choice.
    readonly property string fansShown: (st.max ? st.max.fans : root.fansMode) || "auto"
    // Re-evaluated whenever a new status arrives; that is often enough for
    // "keep-alive N s ago".
    readonly property real nowSec: { st; return Date.now() / 1000 }
    readonly property real wattsNow: { root.watts; return Date.now() / 1000 }
    readonly property bool wattsFresh: Modes.wattsFresh(root.watts, full.wattsNow)
    readonly property var live: full.wattsFresh ? root.watts : null
    // The GPU as the popup's own status read it (fresh while open), else as
    // the sampler last saw it.
    readonly property var gq: full.dgpu.gpu
        || (full.live && full.live.gpu && full.live.gpu.held ? full.live.gpu : null)
    readonly property var gpuW: full.gq && typeof full.gq.power_w === "number"
                                ? full.gq.power_w : null
    readonly property var cpuLimit: full.live ? Modes.cpuLimitText(full.live.cpu_limit) : null

    readonly property color modeColor: root.lastError !== "" ? "#f44336"
        : root.mode === "eco" ? "#4caf50"
        : root.mode === "max" ? "#ff9800"
        : "#78909c"

    function has(v) { return v !== null && v !== undefined }
    function w(v) { return has(v) ? Math.round(v) + " W" : "—" }

    // ── Building blocks ──────────────────────────────────────────────────────

    // A card: a titled block on a slightly raised background.
    component Card: Rectangle {
        id: card
        default property alias content: body.data
        property string title: ""
        property string subtitle: ""
        property string icon: ""
        Layout.fillWidth: true
        implicitHeight: col.implicitHeight + Kirigami.Units.largeSpacing * 2
        radius: Kirigami.Units.cornerRadius
        color: Qt.alpha(Kirigami.Theme.textColor, 0.04)
        border.color: Qt.alpha(Kirigami.Theme.textColor, 0.08)

        ColumnLayout {
            id: col
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            spacing: Kirigami.Units.smallSpacing

            RowLayout {
                visible: card.title !== ""
                spacing: Kirigami.Units.smallSpacing
                Kirigami.Icon {
                    visible: card.icon !== ""
                    source: card.icon
                    Layout.preferredWidth: Kirigami.Units.iconSizes.small
                    Layout.preferredHeight: Kirigami.Units.iconSizes.small
                }
                Kirigami.Heading {
                    level: 5
                    text: card.title
                    font.weight: Font.DemiBold
                }
                PC3.Label {
                    Layout.fillWidth: true
                    visible: card.subtitle !== ""
                    text: card.subtitle
                    elide: Text.ElideRight
                    opacity: 0.6
                    font: Kirigami.Theme.smallFont
                }
            }
            ColumnLayout {
                id: body
                Layout.fillWidth: true
                spacing: 2
            }
        }
    }

    // One "label   value" line in a card.
    component Stat: RowLayout {
        property string label: ""
        property string value: ""
        property color valueColor: Kirigami.Theme.textColor
        property bool wrap: false
        Layout.fillWidth: true
        spacing: Kirigami.Units.largeSpacing
        PC3.Label {
            Layout.preferredWidth: Kirigami.Units.gridUnit * 7
            Layout.alignment: Qt.AlignTop
            text: parent.label
            opacity: 0.65
            font: Kirigami.Theme.smallFont
        }
        PC3.Label {
            Layout.fillWidth: true
            text: parent.value
            color: parent.valueColor
            font: Kirigami.Theme.smallFont
            wrapMode: parent.wrap ? Text.WordWrap : Text.NoWrap
            elide: parent.wrap ? Text.ElideNone : Text.ElideRight
        }
    }

    // A big number with a caption under it.
    component Tile: Item {
        id: tile
        property string value: "—"
        property string caption: ""
        property color accent: Kirigami.Theme.textColor
        Layout.fillWidth: true
        Layout.preferredWidth: Kirigami.Units.gridUnit * 5
        implicitHeight: tileCol.implicitHeight
        ColumnLayout {
            id: tileCol
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 0
            PC3.Label {
                Layout.alignment: Qt.AlignHCenter
                text: tile.value
                color: tile.accent
                font.pixelSize: Kirigami.Units.gridUnit * 1.3
                font.weight: Font.DemiBold
            }
            PC3.Label {
                Layout.alignment: Qt.AlignHCenter
                text: tile.caption
                opacity: 0.6
                font: Kirigami.Theme.smallFont
            }
        }
    }

    // A segmented row of checkable buttons bound to real state.
    component Segments: RowLayout {
        id: seg
        property var options: []          // [{ id, text }]
        property string current: ""
        property bool allowed: true
        property var isEnabled: function(id) { return true }
        signal picked(string id)
        Layout.fillWidth: true
        spacing: 0
        Repeater {
            model: seg.options
            delegate: PC3.Button {
                required property var modelData
                Layout.fillWidth: true
                text: modelData.text
                checkable: true
                checked: seg.current === modelData.id
                enabled: seg.allowed && seg.isEnabled(modelData.id)
                onClicked: {
                    // Keep the visual bound to real state, not to the click.
                    checked = Qt.binding(function() { return seg.current === modelData.id })
                    seg.picked(modelData.id)
                }
            }
        }
    }

    // A labelled switch bound to a setting.
    component Toggle: RowLayout {
        id: tg
        property string text: ""
        property string hint: ""
        property bool on: false
        signal toggled(bool want)
        Layout.fillWidth: true
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            PC3.Label { Layout.fillWidth: true; text: tg.text; elide: Text.ElideRight }
            PC3.Label {
                Layout.fillWidth: true
                visible: tg.hint !== ""
                text: tg.hint
                wrapMode: Text.WordWrap
                opacity: 0.6
                font: Kirigami.Theme.smallFont
            }
        }
        Switch {
            checked: tg.on
            onToggled: {
                const want = checked
                checked = Qt.binding(function() { return tg.on })
                tg.toggled(want)
            }
        }
    }

    // ── Layout ───────────────────────────────────────────────────────────────

    PC3.ScrollView {
        id: scroll
        anchors.fill: parent
        contentWidth: availableWidth
        PC3.ScrollBar.horizontal.policy: PC3.ScrollBar.AlwaysOff

        ColumnLayout {
            width: scroll.availableWidth
            spacing: Kirigami.Units.largeSpacing

            // ── Header ───────────────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.largeSpacing

                Rectangle {
                    width: 12; height: 12; radius: 6
                    color: full.modeColor
                    border.color: Qt.darker(color, 1.4)
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    Kirigami.Heading {
                        level: 3
                        text: root.mode === "eco"
                            ? "Eco · " + ({ gentle: "Gentle", deep: "Deep", deepest: "Deepest" }
                                          [st.level || "gentle"] || st.level)
                            : root.mode === "max" ? "Max" : "Normal"
                    }
                    PC3.Label {
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        opacity: 0.7
                        font: Kirigami.Theme.smallFont
                        text: "KDE profile: " + (root.kdeProfile || st.ppd_profile || "?")
                              + (root.followProfile ? "  ·  following" : "")
                    }
                }
                PC3.BusyIndicator {
                    running: root.busy
                    visible: root.busy
                    Layout.preferredWidth: Kirigami.Units.iconSizes.small
                    Layout.preferredHeight: Kirigami.Units.iconSizes.small
                }
            }

            Segments {
                options: [{ id: "eco", text: "Eco" }, { id: "normal", text: "Normal" },
                          { id: "max", text: "Max" }]
                current: root.mode
                allowed: !root.busy
                isEnabled: function(id) { return id !== "max" || st.max_supported === true }
                onPicked: function(id) { root.setMode(id) }
            }

            PC3.Label {
                Layout.fillWidth: true
                visible: st.max_supported === false
                wrapMode: Text.WordWrap
                font: Kirigami.Theme.smallFont
                opacity: 0.6
                text: "Max unavailable: " + (st.max_unsupported_reason || "unknown reason")
            }

            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Error
                visible: root.lastError !== ""
                text: root.lastError
            }

            // ── Mode details ─────────────────────────────────────────────────
            Card {
                visible: root.mode === "eco"
                title: "Level"
                icon: "battery-profile-powersave"

                // Switching while Eco is on re-applies in one call; the helper
                // reverts the old level first so the recorded originals stay
                // correct.
                Segments {
                    options: [{ id: "gentle", text: "Gentle" }, { id: "deep", text: "Deep" },
                              { id: "deepest", text: "Deepest" }]
                    current: root.level
                    allowed: !root.busy
                    onPicked: function(id) { root.setLevel(id) }
                }
                PC3.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    font: Kirigami.Theme.smallFont
                    opacity: 0.75
                    text: Modes.levelText(root.level, root.maxPct)
                }
            }

            Card {
                visible: root.mode === "max"
                title: "Max"
                icon: "battery-profile-performance"

                PC3.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    font: Kirigami.Theme.smallFont
                    color: st.max && st.max.state === "paused"
                        ? Kirigami.Theme.neutralTextColor : Kirigami.Theme.textColor
                    text: Modes.maxLine(st, full.nowSec)
                }

                PC3.Label {
                    text: "Fans"
                    opacity: 0.65
                    font: Kirigami.Theme.smallFont
                    Layout.topMargin: Kirigami.Units.smallSpacing
                }
                // A new choice restarts Max; leaving full speed starts with a
                // pause while the controller takes the fans back.
                Segments {
                    options: [{ id: "auto", text: "Auto" }, { id: "max", text: "Max" },
                              { id: "boost", text: "Boost" }]
                    current: full.fansShown
                    allowed: !root.busy && full.fans.available === true
                    onPicked: function(id) { root.setFans(id) }
                }
                PC3.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                    text: full.fansShown === "max"
                        ? "Full speed. Back to automatic about 2 minutes after Max ends "
                          + "or you switch."
                        : full.fansShown === "boost"
                        ? "Full speed from 85 °C; after 3 minutes below 65 °C, Max pauses "
                          + "about 2 minutes while the fans hand back to automatic."
                        : "The firmware's own fan curve."
                }

                Toggle {
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    text: "GPU Dynamic Boost"
                    hint: root.gpuBoostError !== "" ? root.gpuBoostError
                        : st.gpu_boost ? "nvidia-powerd runs: the GPU may draw past its base limit"
                        : full.dgpu.powerd === true
                        ? "nvidia-powerd already runs (not started by Max)"
                        : "nvidia-powerd off: the GPU keeps its base power limit"
                    on: root.gpuBoost
                    onToggled: function(want) { root.setGpuBoost(want) }
                }

                PC3.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    font: Kirigami.Theme.smallFont
                    opacity: 0.55
                    text: "Ends on logout, reboot, or if you switch the power profile."
                }
            }

            PC3.Label {
                Layout.fillWidth: true
                visible: root.mode === "normal" && text !== ""
                wrapMode: Text.WordWrap
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                text: Modes.endedLine(st.max_last, full.nowSec)
            }

            // ── Switches ─────────────────────────────────────────────────────
            Card {
                Toggle {
                    enabled: root.canFollow
                    text: "Follow KDE power profile"
                    hint: root.canFollow
                        ? "Power Save → Eco, Balanced → Normal, Performance → Max"
                        : "Needs Plasma's D-Bus QML module (newer Plasma 6)"
                    on: root.followProfile
                    onToggled: function(want) { root.setFollowProfile(want) }
                }
                Toggle {
                    text: "Show watts in panel"
                    hint: "Runs a small root sampler; also shows what limits the CPU"
                    on: root.showWatts
                    onToggled: function(want) { root.setShowWatts(want) }
                }
            }

            // ── Power at a glance ────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                visible: root.showWatts || full.has(full.gpuW)
                spacing: 0

                Tile {
                    value: full.live ? full.w(full.live.cpu_w) : "—"
                    caption: "CPU"
                    accent: full.modeColor
                }
                Tile {
                    value: full.has(full.gpuW) ? full.w(full.gpuW)
                         : full.dgpu.runtime_status === "suspended" ? "off" : "—"
                    caption: "GPU"
                }
                Tile {
                    visible: !!full.live && full.has(full.live.battery_w)
                    value: full.live ? full.w(full.live.battery_w) : "—"
                    caption: "laptop (battery)"
                }
            }

            // ── CPU ──────────────────────────────────────────────────────────
            Card {
                title: "CPU"
                subtitle: full.cpu.model || ""
                icon: "cpu"

                Stat {
                    label: "Power limits"
                    value: full.has(full.cpu.eff_pl1_w)
                        ? "PL1 " + full.cpu.eff_pl1_w + " W · PL2 " + full.cpu.eff_pl2_w + " W"
                        : "—"
                }
                Stat {
                    label: "Limited by"
                    value: full.cpuLimit !== null ? full.cpuLimit
                         : "turn on \"Show watts\" to see this live"
                    valueColor: full.cpuLimit === null ? Kirigami.Theme.disabledTextColor
                              : full.live && full.live.cpu_limit.length
                                ? Kirigami.Theme.neutralTextColor
                                : Kirigami.Theme.positiveTextColor
                    wrap: true
                }
                Stat {
                    visible: root.mode === "max"
                    label: "EC 44 W fallback"
                    value: st.max && st.max.state === "active"
                        ? "held off by the keep-alive" : "not held off while paused"
                    valueColor: st.max && st.max.state === "active"
                        ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.neutralTextColor
                }
                Stat {
                    label: "Clock"
                    value: st.cpu_avg_mhz ? st.cpu_avg_mhz + " MHz average" : "—"
                }
                Stat {
                    visible: full.has(st.pkg_temp_c)
                    label: "Temperature"
                    value: st.pkg_temp_c + " °C" + (full.has(full.cpu.tjmax_c)
                        ? " (throttles at " + full.cpu.tjmax_c + " °C)" : "")
                    valueColor: st.pkg_temp_c >= (full.cpu.tjmax_c || 100) - 5
                              ? Kirigami.Theme.negativeTextColor
                              : st.pkg_temp_c >= (full.cpu.tjmax_c || 100) - 15
                              ? Kirigami.Theme.neutralTextColor
                              : Kirigami.Theme.textColor
                }
                Stat {
                    visible: (full.fans.rpm || []).length > 0
                    label: "Fans"
                    value: (full.fans.rpm || []).join(" / ") + " RPM"
                           + (full.fans.pwm_enable === "0" ? "  (full speed)" : "")
                }
                Stat {
                    visible: full.has(full.cpu.throttle_ms)
                    label: "Hot since boot"
                    value: full.has(full.cpu.throttle_ms)
                        ? Math.round(full.cpu.throttle_ms / 60000) + " min thermally throttled"
                        : ""
                }
                // Measured at apply time, one second apart under the same load.
                Stat {
                    visible: root.mode === "eco" && full.has(st.power_before) && full.has(st.power_after)
                    label: "At apply"
                    value: full.has(st.power_before)
                        ? st.power_before + " W → " + st.power_after + " W" : ""
                }
            }

            // ── GPU ──────────────────────────────────────────────────────────
            Card {
                visible: full.dgpu.present === true
                title: "GPU"
                subtitle: full.gq && full.gq.name ? full.gq.name.replace("NVIDIA GeForce ", "")
                                                 : "discrete NVIDIA"
                icon: "video-card-symbolic"

                Stat {
                    label: "State"
                    value: Modes.gpuStateText(full.dgpu)
                           + (Modes.sleepShare(full.dgpu) ? " · " + Modes.sleepShare(full.dgpu) : "")
                    valueColor: full.dgpu.runtime_status === "suspended"
                        ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.neutralTextColor
                }
                Stat {
                    visible: full.gq !== null
                    label: "Power"
                    value: full.gq ? full.w(full.gq.power_w) + " of " + full.w(full.gq.limit_w)
                                     + (full.gq.pstate ? " · " + full.gq.pstate : "") : ""
                }
                Stat {
                    visible: full.gq !== null && full.has(full.gq.util_pct)
                    label: "Load"
                    value: full.gq ? full.gq.util_pct + " %"
                        + (full.has(full.gq.vram_used_mib)
                           ? " · VRAM " + Math.round(full.gq.vram_used_mib / 102.4) / 10 + " / "
                             + Math.round(full.gq.vram_total_mib / 1024) + " GiB" : "") : ""
                }
                Stat {
                    visible: full.gq !== null && full.has(full.gq.gr_mhz)
                    label: "Clocks"
                    value: full.gq ? full.gq.gr_mhz + " MHz core · " + full.gq.mem_mhz + " MHz memory"
                                   : ""
                }
                Stat {
                    visible: full.gq !== null && full.has(full.gq.temp_c)
                    label: "Temperature"
                    value: full.gq ? full.gq.temp_c + " °C" : ""
                    valueColor: full.gq && full.gq.temp_c >= 85
                        ? Kirigami.Theme.neutralTextColor : Kirigami.Theme.textColor
                }
                Stat {
                    visible: full.gq !== null && (full.gq.reasons || []).length > 0
                    label: "Slowed by"
                    value: full.gq ? (full.gq.reasons || []).join(", ") : ""
                    valueColor: Kirigami.Theme.neutralTextColor
                }
                Stat {
                    visible: full.has(full.dgpu.caps) && full.dgpu.caps !== null
                    label: "Eco clock cap"
                    value: Modes.gpuCapText(full.dgpu.caps, full.dgpu.runtime_status)
                    wrap: true
                }
                Stat {
                    visible: full.has(full.dgpu.powerd)
                    label: "Dynamic Boost"
                    value: full.dgpu.powerd ? "on (nvidia-powerd)" : "off"
                }
                Stat {
                    label: "Displays"
                    value: (full.dgpu.displays || []).length
                        ? full.dgpu.displays.join(", ") + ", keeps it awake"
                        : "none on this GPU"
                }

                PC3.Label {
                    visible: (full.dgpu.holders || []).length > 0
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    text: (full.dgpu.holders || []).some(function(h) {
                              return h.awake !== false && !h.follower })
                          ? "Kept awake by" : "Open on it (not keeping it awake)"
                    opacity: 0.65
                    font: Kirigami.Theme.smallFont
                }
                Repeater {
                    model: full.dgpu.holders || []
                    delegate: ColumnLayout {
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.leftMargin: Kirigami.Units.largeSpacing
                        spacing: 0
                        PC3.Label {
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                            font: Kirigami.Theme.smallFont
                            // Display-only holders do not keep it awake.
                            opacity: modelData.awake === false ? 0.55 : 1
                            text: "• " + modelData.name + "  (pid " + modelData.pid + ")"
                        }
                        PC3.Label {
                            visible: !!modelData.note
                            Layout.fillWidth: true
                            Layout.leftMargin: Kirigami.Units.largeSpacing
                            wrapMode: Text.WordWrap
                            opacity: 0.6
                            font: Kirigami.Theme.smallFont
                            text: modelData.note || ""
                        }
                    }
                }
                PC3.Label {
                    visible: full.dgpu.runtime_status === "active" && full.dgpu.deep === true
                             && (full.dgpu.holders || []).filter(function(h) {
                                    return h.awake !== false && !h.follower }).length === 0
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    opacity: 0.6
                    font: Kirigami.Theme.smallFont
                    text: "No process of yours holds it; it should power down shortly "
                          + "(system processes are not visible here)."
                }
            }

            // ── Laptop ───────────────────────────────────────────────────────
            Card {
                id: laptop
                readonly property var bat: st.battery || null
                title: "Laptop"
                icon: st.on_ac === false ? "battery-good" : "ac-adapter"
                Stat {
                    label: "Power source"
                    value: st.on_ac === false ? "battery" : st.on_ac === true ? "AC adapter" : "?"
                }
                Stat {
                    visible: laptop.bat !== null
                    label: "Battery"
                    value: Modes.batteryLine(laptop.bat)
                    valueColor: laptop.bat && laptop.bat.charge_mode === "inhibit-charge"
                        ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.textColor
                }
                Stat {
                    visible: Modes.healthText(laptop.bat) !== ""
                    label: "Health"
                    value: Modes.healthText(laptop.bat)
                    valueColor: laptop.bat && laptop.bat.health_pct < 80
                        ? Kirigami.Theme.neutralTextColor : Kirigami.Theme.textColor
                }
                Stat {
                    label: "Total draw"
                    value: full.live && full.has(full.live.battery_w)
                        ? full.w(full.live.battery_w)
                        : st.on_ac === false ? "turn on \"Show watts\" to see it"
                        : "unknown on AC: the adapter has no sensor"
                    wrap: true
                }

                // Battery care: only where the battery's driver offers
                // charge_behaviour with inhibit-charge.
                Toggle {
                    visible: laptop.bat !== null
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    enabled: !root.busy && laptop.bat !== null && laptop.bat.charge_supported
                    text: "Battery care"
                    hint: !laptop.bat || !laptop.bat.charge_supported
                        ? "This battery's driver offers no charge control"
                          + (st.max_supported ? ". On this OMEN, install kernel/hp-wmi-charge" : "")
                        : laptop.bat.limit
                        ? "Stops charging at " + laptop.bat.limit.stop + " %, resumes at "
                          + (laptop.bat.limit.stop - 5) + " %, also while logged out"
                        : "Charge to 100 % (off)"
                    on: !!(laptop.bat && laptop.bat.limit)
                    onToggled: function(want) {
                        root.setChargeLimit(want, laptop.bat ? laptop.bat.limit_stop : 80)
                    }
                }
                Segments {
                    visible: !!(laptop.bat && laptop.bat.limit)
                    options: [{ id: "60", text: "60 %" }, { id: "70", text: "70 %" },
                              { id: "80", text: "80 %" }, { id: "90", text: "90 %" }]
                    current: laptop.bat && laptop.bat.limit ? String(laptop.bat.limit.stop) : ""
                    allowed: !root.busy
                    onPicked: function(id) { root.setChargeLimit(true, parseInt(id)) }
                }
            }

            // ── Eco knobs ────────────────────────────────────────────────────
            Card {
                visible: root.mode === "eco"
                title: "What Eco set"
                icon: "configure"

                Repeater {
                    model: st.knobs || []
                    delegate: Item {
                        required property var modelData
                        Layout.fillWidth: true
                        implicitHeight: krow.implicitHeight + 4

                        HoverHandler { id: hover }
                        PC3.ToolTip.visible: hover.hovered && modelData.desc
                        PC3.ToolTip.text: modelData.desc || ""
                        PC3.ToolTip.delay: 600

                        RowLayout {
                            id: krow
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: Kirigami.Units.smallSpacing

                            // green = at eco target, blue = PPD's, grey = idle
                            Rectangle {
                                Layout.preferredWidth: 6
                                Layout.preferredHeight: 6
                                radius: 3
                                color: modelData.managed
                                    ? Kirigami.Theme.linkColor
                                    : (modelData.at_eco ? Kirigami.Theme.positiveTextColor
                                                        : Kirigami.Theme.disabledTextColor)
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0
                                PC3.Label {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    font: Kirigami.Theme.smallFont
                                    text: modelData.label
                                }
                                PC3.Label {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    visible: modelData.managed || !modelData.in_level
                                    opacity: 0.55
                                    font: Kirigami.Theme.smallFont
                                    text: modelData.managed
                                        ? "managed by power-profiles-daemon"
                                        : "used by the Deep levels"
                                }
                            }
                            PC3.Label {
                                text: modelData.current_human
                                font.family: "monospace"
                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                color: !modelData.managed && modelData.at_eco
                                    ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.textColor
                            }
                            PC3.Label {
                                visible: !modelData.managed && !modelData.at_eco
                                         && !!modelData.eco_target_human
                                opacity: 0.55
                                font.family: "monospace"
                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                text: "→ " + (modelData.eco_target_human || "")
                            }
                        }
                    }
                }

                // ── Self-test ────────────────────────────────────────────────
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    spacing: Kirigami.Units.smallSpacing

                    Kirigami.Icon {
                        visible: root.lastTest !== null
                        Layout.preferredWidth: Kirigami.Units.iconSizes.small
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        source: {
                            if (!root.lastTest || root.lastTest.applicable === false)
                                return "dialog-information"
                            return root.lastTest.pass ? "dialog-ok" : "dialog-warning"
                        }
                    }
                    PC3.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        font: Kirigami.Theme.smallFont
                        opacity: 0.8
                        text: {
                            if (!root.lastTest) return "no self-test run yet"
                            if (root.lastTest.applicable === false)
                                return root.lastTest.note || "eco is off; nothing to verify"
                            const checks = root.lastTest.checks || []
                            const bad = checks.filter(function(c) { return c.status === "fail" })
                            const skipped = checks.filter(function(c) { return c.status === "skipped" })
                            if (bad.length)
                                return "self-test FAILED: " + bad.map(function(c) {
                                    return c.label + (c.note ? " (" + c.note + ")" : "")
                                }).join(", ")
                            let msg = "self-test passed: " + checks.length + " checks"
                            if (skipped.length) msg += ", " + skipped.length + " skipped"
                            return msg
                        }
                    }
                    PC3.Button {
                        text: "Self-test"
                        icon.name: "checkmark"
                        enabled: !root.busy
                        onClicked: root.runSelfTest()
                    }
                }
            }

            Item { Layout.preferredHeight: Kirigami.Units.smallSpacing }
        }
    }
}
