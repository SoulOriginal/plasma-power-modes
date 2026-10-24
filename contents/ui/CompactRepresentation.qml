// SPDX-License-Identifier: GPL-2.0-or-later
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.kirigami as Kirigami
import "modes.js" as Modes

Item {
    id: compact

    readonly property bool horizontal: Plasmoid.formFactor === 2
    readonly property real cellH: horizontal ? parent.height : Kirigami.Units.iconSizes.smallMedium
    // Re-evaluated whenever a new watts record or status arrives.
    readonly property real nowSec: { root.watts; root.status; return Date.now() / 1000 }
    readonly property var label: Modes.withWatts(
        Modes.compactLabel(root.status, root.lastError !== ""),
        root.showWatts, root.watts, compact.nowSec, root.wattsGpu)
    readonly property bool gpuShown: root.showWatts && root.wattsGpu
                                     && Modes.gpuWatts(root.watts, compact.nowSec) !== null

    Layout.preferredWidth: horizontal ? row.implicitWidth + Kirigami.Units.smallSpacing * 2
                                      : parent.width
    Layout.preferredHeight: horizontal ? parent.height
                                       : row.implicitHeight + Kirigami.Units.smallSpacing
    Layout.minimumWidth: Layout.preferredWidth
    Layout.maximumWidth: Layout.preferredWidth

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        onClicked: function(e) {
            if (e.button === Qt.MiddleButton) {
                root.toggle()          // middle click = Normal <-> chosen mode
            } else {
                root.expanded = !root.expanded
            }
        }
    }

    RowLayout {
        id: row
        anchors.centerIn: parent
        spacing: Kirigami.Units.smallSpacing / 2
        opacity: root.busy ? 0.45 : 1.0

        Behavior on opacity {
            NumberAnimation { duration: Kirigami.Units.shortDuration }
        }

        Rectangle {
            implicitWidth: Math.round(compact.cellH * 0.4)
            implicitHeight: implicitWidth
            radius: implicitWidth / 2
            opacity: compact.label.dim ? 0.5 : 1.0
            color: compact.label.color === "error" ? "#f44336"
                 : compact.label.color === "eco" ? "#4caf50"
                 : compact.label.color === "max" ? "#ff9800"
                 : "#78909c"
            border.color: Qt.darker(color, 1.4)
            border.width: 1
        }

        Text {
            id: labelText
            text: compact.label.text
            color: Kirigami.Theme.textColor
            font.pixelSize: Math.round(compact.cellH * 0.45)
            font.family: Kirigami.Theme.smallFont.family
            verticalAlignment: Text.AlignVCenter
            horizontalAlignment: Text.AlignHCenter
            // With watts shown the label is a number that keeps changing
            // (9 W, 10 W, 100 W, "—"); room for three digits keeps the panel
            // from shifting its neighbours on every reading.
            Layout.minimumWidth: root.showWatts ? Math.ceil(widest.advanceWidth) : 0
        }
    }

    TextMetrics {
        id: widest
        font: labelText.font
        // "CPU·GPU" while the GPU's draw is shown, CPU alone otherwise.
        text: compact.gpuShown ? "188·88W" : "888W"
    }
}
