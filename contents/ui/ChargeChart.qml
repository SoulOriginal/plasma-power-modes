// SPDX-License-Identifier: GPL-2.0-or-later
// Battery care chart: the charge over the last day or week, coloured by what
// the battery was doing, with the stop and resume levels, sleep shaded, and a
// marker where the service stopped or resumed charging. Hover for a reading.
import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PC3
import "modes.js" as Modes

ColumnLayout {
    id: chart
    property var history: null           // `ecomode --json charge-history`
    property int hours: 24
    signal hoursPicked(int hours)
    spacing: Kirigami.Units.smallSpacing
    Layout.fillWidth: true

    readonly property var series: history
        ? Modes.chargeSeries(history.points, history.now, history.hours) : null
    readonly property bool empty: !series || series.steps.length === 0
    property real hoverT: -1             // seconds; -1 = not hovering

    // Theme colours, so the chart follows the Plasma colour scheme. Checked
    // against Breeze light and dark for colour-blind separation; the orange
    // is one step darker on a dark surface to stay inside the lightness band.
    readonly property bool dark: Kirigami.Theme.backgroundColor.hslLightness < 0.5
    readonly property var colors: ({
        charging: Kirigami.Theme.positiveTextColor,
        held: Kirigami.Theme.linkColor,
        battery: dark ? Qt.darker(Kirigami.Theme.neutralTextColor, 1.08)
                      : Kirigami.Theme.neutralTextColor
    })
    readonly property color ink: Kirigami.Theme.textColor
    readonly property color surface: Kirigami.Theme.backgroundColor

    // ── Header: what the line says now, or under the pointer ─────────────────
    RowLayout {
        Layout.fillWidth: true
        PC3.Label {
            Layout.fillWidth: true
            elide: Text.ElideRight
            font: Kirigami.Theme.smallFont
            text: {
                if (chart.empty) return "History"
                const pts = chart.history.points
                if (chart.hoverT >= 0)
                    return Modes.chargeHoverText(Modes.chargeAt(pts, chart.hoverT), chart.hours)
                return "Now: " + Modes.chargeHoverText(pts[pts.length - 1], chart.hours)
                    .split(" · ").slice(1, 3).join(" · ")
            }
        }
        Repeater {
            model: [{ h: 24, text: "24 h" }, { h: 168, text: "7 d" }]
            delegate: PC3.ToolButton {
                required property var modelData
                text: modelData.text
                checkable: true
                checked: chart.hours === modelData.h
                font: Kirigami.Theme.smallFont
                onClicked: {
                    checked = Qt.binding(function() { return chart.hours === modelData.h })
                    chart.hoursPicked(modelData.h)
                }
            }
        }
    }

    // ── Plot ─────────────────────────────────────────────────────────────────
    Item {
        id: plotBox
        Layout.fillWidth: true
        Layout.preferredHeight: Kirigami.Units.gridUnit * 5
        readonly property real padL: Kirigami.Units.gridUnit * 1.6
        readonly property real padR: Kirigami.Units.gridUnit * 2
        readonly property real padT: 4
        readonly property real padB: 4
        readonly property real x0: padL
        readonly property real x1: width - padR
        readonly property real y0: padT
        readonly property real y1: height - padB

        function xOf(t) {
            const s = chart.series
            return x0 + (t - s.from) / Math.max(1, s.to - s.from) * (x1 - x0)
        }
        function yOf(cap) { return y1 - cap / 100 * (y1 - y0) }

        // Y axis: 0 / 50 / 100, muted.
        Repeater {
            model: [0, 50, 100]
            delegate: PC3.Label {
                required property int modelData
                x: 0
                width: plotBox.padL - 4
                horizontalAlignment: Text.AlignRight
                y: plotBox.yOf(modelData) - height / 2
                text: modelData
                font: Kirigami.Theme.smallFont
                opacity: 0.55
            }
        }
        // The two levels Battery care works between, labelled on the right.
        Repeater {
            model: chart.history && !chart.empty
                ? [{ v: chart.history.stop, o: 0.8 }, { v: chart.history.resume, o: 0.5 }] : []
            delegate: PC3.Label {
                required property var modelData
                x: plotBox.x1 + 4
                y: plotBox.yOf(modelData.v) - height / 2
                    + (modelData.v === chart.history.resume ? height * 0.45 : -height * 0.45)
                text: modelData.v + " %"
                font: Kirigami.Theme.smallFont
                opacity: modelData.o
            }
        }

        Canvas {
            id: canvas
            anchors.fill: parent
            renderStrategy: Canvas.Cooperative
            onPaint: {
                const ctx = getContext("2d")
                ctx.reset()
                const s = chart.series
                if (!s) return
                const P = plotBox
                const line = function(xa, ya, xb, yb) {
                    ctx.beginPath(); ctx.moveTo(xa, ya); ctx.lineTo(xb, yb); ctx.stroke()
                }
                const alpha = function(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

                // Gridlines: hairline, recessive.
                ctx.lineWidth = 1
                ctx.strokeStyle = alpha(chart.ink, 0.12)
                for (const v of [0, 50, 100])
                    line(P.x0, Math.round(P.yOf(v)) + 0.5, P.x1, Math.round(P.yOf(v)) + 0.5)

                // Asleep: a light wash with a 45° hatch.
                for (const b of s.sleeps) {
                    const xa = P.xOf(b.t0), xb = P.xOf(b.t1)
                    ctx.fillStyle = alpha(chart.ink, 0.06)
                    ctx.fillRect(xa, P.y0, Math.max(1, xb - xa), P.y1 - P.y0)
                    ctx.save()
                    ctx.beginPath(); ctx.rect(xa, P.y0, Math.max(1, xb - xa), P.y1 - P.y0); ctx.clip()
                    ctx.strokeStyle = alpha(chart.ink, 0.14)
                    const hgt = P.y1 - P.y0
                    for (let x = xa - hgt; x < xb; x += 6) line(x, P.y1, x + hgt, P.y0)
                    ctx.restore()
                }

                // Stop and resume levels.
                if (chart.history) {
                    ctx.strokeStyle = alpha(chart.ink, 0.35)
                    const ys = Math.round(P.yOf(chart.history.stop)) + 0.5
                    line(P.x0, ys, P.x1, ys)
                    ctx.strokeStyle = alpha(chart.ink, 0.2)
                    const yr = Math.round(P.yOf(chart.history.resume)) + 0.5
                    line(P.x0, yr, P.x1, yr)
                }

                // The charge: 2 px steps, each in its state's colour; a change
                // of level is drawn in the colour of what follows it.
                ctx.lineWidth = 2
                ctx.lineCap = "round"
                ctx.lineJoin = "round"
                for (let i = 0; i < s.steps.length; i++) {
                    const st = s.steps[i], prev = s.steps[i - 1]
                    const y = P.yOf(st.cap)
                    ctx.strokeStyle = chart.colors[st.state]
                    ctx.beginPath()
                    if (prev && prev.t1 === st.t0) ctx.moveTo(P.xOf(st.t0), P.yOf(prev.cap))
                    else ctx.moveTo(P.xOf(st.t0), y)
                    ctx.lineTo(P.xOf(st.t0), y)
                    ctx.lineTo(Math.max(P.xOf(st.t0) + 0.5, P.xOf(st.t1)), y)
                    ctx.stroke()
                }

                // Where the service stopped or resumed charging: 8 px dots
                // with a 2 px ring in the surface colour.
                for (const mk of s.marks) {
                    const x = P.xOf(mk.t), y = P.yOf(mk.cap)
                    ctx.beginPath(); ctx.arc(x, y, 6, 0, 2 * Math.PI)
                    ctx.fillStyle = chart.surface; ctx.fill()
                    ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI)
                    ctx.fillStyle = chart.colors[mk.event === "stop" ? "held" : "charging"]
                    ctx.fill()
                }

                // Hover: a crosshair and the reading's dot.
                if (chart.hoverT >= 0 && chart.history) {
                    const row = Modes.chargeAt(chart.history.points, chart.hoverT)
                    const x = P.xOf(chart.hoverT)
                    ctx.lineWidth = 1
                    ctx.strokeStyle = alpha(chart.ink, 0.45)
                    line(Math.round(x) + 0.5, P.y0, Math.round(x) + 0.5, P.y1)
                    if (row) {
                        const y = P.yOf(row[1])
                        ctx.beginPath(); ctx.arc(x, y, 6, 0, 2 * Math.PI)
                        ctx.fillStyle = chart.surface; ctx.fill()
                        ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI)
                        ctx.fillStyle = chart.colors[Modes.chargeState(row)]; ctx.fill()
                    }
                }
            }
        }

        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            enabled: !chart.empty
            onPositionChanged: function(mouse) {
                const s = chart.series
                const f = (mouse.x - plotBox.x0) / Math.max(1, plotBox.x1 - plotBox.x0)
                chart.hoverT = f < 0 || f > 1 ? -1 : s.from + f * (s.to - s.from)
            }
            onExited: chart.hoverT = -1
        }

        PC3.Label {
            anchors.centerIn: parent
            visible: chart.empty
            width: parent.width - Kirigami.Units.gridUnit * 2
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            font: Kirigami.Theme.smallFont
            opacity: 0.6
            text: "No history yet. It fills in as the battery changes."
        }
    }

    onSeriesChanged: canvas.requestPaint()
    onHoverTChanged: canvas.requestPaint()
    onColorsChanged: canvas.requestPaint()
    onWidthChanged: canvas.requestPaint()

    // ── Time axis ────────────────────────────────────────────────────────────
    Item {
        Layout.fillWidth: true
        Layout.preferredHeight: tickProbe.implicitHeight
        visible: !chart.empty
        PC3.Label { id: tickProbe; visible: false; text: "00:00"; font: Kirigami.Theme.smallFont }
        Repeater {
            model: {
                const s = chart.series
                if (!s) return []
                // Every 6 hours over a day, every midnight over a week.
                const ticks = []
                const d = new Date(s.from * 1000)
                d.setMinutes(0, 0, 0)
                if (chart.hours > 24) d.setHours(0)
                let t = d.getTime() / 1000
                while (t < s.from) t += 3600
                while (t <= s.to) {
                    const h = new Date(t * 1000).getHours()
                    if (chart.hours > 24 ? h === 0 : h % 6 === 0) ticks.push(t)
                    t += 3600            // hour by hour: DST-safe
                }
                return ticks
            }
            delegate: PC3.Label {
                required property real modelData
                readonly property real cx: plotBox.xOf(modelData)
                x: Math.max(0, Math.min(parent.width - width, cx - width / 2))
                text: chart.hours > 24
                    ? ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(modelData * 1000).getDay()]
                    : Modes.clockText(modelData)
                font: Kirigami.Theme.smallFont
                opacity: 0.55
            }
        }
    }

    // ── Legend: colour is never the only way to tell the states apart ────────
    Flow {
        Layout.fillWidth: true
        spacing: Kirigami.Units.largeSpacing
        visible: !chart.empty
        Repeater {
            model: [{ k: "charging", text: "charging" },
                    { k: "held", text: "on AC, not charging" },
                    { k: "battery", text: "on battery" },
                    { k: "asleep", text: "asleep" }]
            delegate: Row {
                required property var modelData
                spacing: 4
                visible: modelData.k !== "asleep" || (chart.series && chart.series.sleeps.length > 0)
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: 12
                    height: modelData.k === "asleep" ? 8 : 2
                    radius: 1
                    color: modelData.k === "asleep"
                        ? Qt.rgba(chart.ink.r, chart.ink.g, chart.ink.b, 0.15)
                        : chart.colors[modelData.k]
                }
                PC3.Label { text: modelData.text; font: Kirigami.Theme.smallFont; opacity: 0.8 }
            }
        }
    }

    PC3.Label {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        font: Kirigami.Theme.smallFont
        opacity: 0.5
        text: "Dots: charging stopped or resumed. Every decision, with its reason: "
              + "journalctl -t ecomode-charge"
    }
}
