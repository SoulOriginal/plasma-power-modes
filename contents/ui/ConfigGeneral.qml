// SPDX-License-Identifier: GPL-2.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "modes.js" as Modes

Kirigami.FormLayout {
    id: page

    property alias cfg_level: levelGroup.value
    property alias cfg_maxPct: maxPctSpin.value
    property alias cfg_pollSeconds: pollSpin.value
    property alias cfg_fansMode: fansGroup.value
    property alias cfg_middleClick: middleGroup.value
    // Internal (remembered by the widget itself); declared so the config
    // dialog has somewhere to put it.
    property string cfg_lastMode
    // Legacy, replaced by fansMode: it still decides until a fan mode is
    // picked (main.qml), and the dialog needs somewhere to put it.
    property bool cfg_fansMax
    property alias cfg_showWatts: showWattsBox.checked
    property alias cfg_wattsSeconds: wattsSpin.value
    property alias cfg_wattsGpu: wattsGpuBox.checked
    property alias cfg_followProfile: followBox.checked
    property alias cfg_syncProfile: syncBox.checked
    property alias cfg_gpuBoost: gpuBoostBox.checked
    property alias cfg_ecoPl1W: pl1Spin.value
    property alias cfg_ecoPl2W: pl2Spin.value
    property alias cfg_ecoTauS: tauSpin.value
    property alias cfg_maxGpuProfile: gpuProfileField.text

    // The fan mode the widget runs with, as main.qml works it out.
    readonly property string fansShown: fansGroup.value || (cfg_fansMax ? "max" : "auto")

    QtObject {
        id: middleGroup
        property string value: "last"
    }

    QtObject {
        id: fansGroup
        property string value: ""
    }

    QtObject {
        id: levelGroup
        property string value: "gentle"
    }

    ColumnLayout {
        Kirigami.FormData.label: "Tuning level"
        spacing: Kirigami.Units.smallSpacing

        Repeater {
            model: [
                { id: "gentle", text: "Gentle: invisible to running apps" },
                { id: "deep", text: "Deep: also cap bursts, frequency and the GPU" },
                { id: "deepest", text: "Deepest: as low as it goes" }
            ]
            delegate: ColumnLayout {
                required property var modelData
                spacing: 0
                RadioButton {
                    text: modelData.text
                    checked: levelGroup.value === modelData.id
                    onToggled: if (checked) levelGroup.value = modelData.id
                }
                Label {
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.maximumWidth: Kirigami.Units.gridUnit * 20
                    wrapMode: Text.WordWrap
                    opacity: 0.7
                    font: Kirigami.Theme.smallFont
                    text: Modes.levelText(modelData.id, maxPctSpin.value)
                }
            }
        }
    }

    CheckBox {
        id: followBox
        Kirigami.FormData.label: "KDE profile:"
        text: "Follow the Power Profile slider"
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "Power Save turns Eco on, Balanced goes back to Normal, " +
              "Performance starts Max. The widget reacts to the daemon's " +
              "change signal, so this costs nothing while the profile stays put."
    }

    CheckBox {
        id: syncBox
        enabled: followBox.checked
        text: "Set the slider from the widget too (two-way)"
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "Picking Eco here moves the slider to Power Save, Normal to " +
              "Balanced. Max needs nothing: it holds Performance itself, and " +
              "when it ends the slider is where Normal or Eco put it."
    }

    Item { Kirigami.FormData.isSection: true }

    SpinBox {
        id: maxPctSpin
        Kirigami.FormData.label: "Deep ceiling:"
        // intel_pstate will not go below its min_perf_pct (16 here).
        from: 20
        to: 100
        stepSize: 5
        enabled: levelGroup.value !== "gentle"
        textFromValue: function(v) { return v + " %" }
        valueFromText: function(t) { return parseInt(t) }
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "Percent of the highest turbo P-state, not of base clock. " +
              "At or below base ÷ max turbo (42 % on an i9-13900HX) it already " +
              "does what disabling Turbo Boost would. Deepest never goes " +
              "above 30 %."
    }

    // The CPU's RAPL limits by hand, over the level's own. 0 stands for the
    // level's value; the helper takes 5..250 W and 1..120 s.
    SpinBox {
        id: pl1Spin
        Kirigami.FormData.label: "Sustained (PL1):"
        from: 0
        to: 250
        stepSize: 5
        textFromValue: function(v) { return v === 0 ? "level's own" : v + " W" }
        valueFromText: function(t) { return parseInt(t) || 0 }
    }

    SpinBox {
        id: pl2Spin
        Kirigami.FormData.label: "Burst (PL2):"
        from: 0
        to: 250
        stepSize: 5
        textFromValue: function(v) { return v === 0 ? "level's own" : v + " W" }
        valueFromText: function(t) { return parseInt(t) || 0 }
    }

    SpinBox {
        id: tauSpin
        Kirigami.FormData.label: "PL1 window (Tau):"
        from: 0
        to: 120
        stepSize: 1
        textFromValue: function(v) { return v === 0 ? "untouched" : v + " s" }
        valueFromText: function(t) { return parseInt(t) || 0 }
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "Eco's power limits by hand, instead of the level's (gentle 35 W, " +
              "deep 25/35 W, deepest 20/25 W). A burst limit set here applies " +
              "at Gentle too, and the window is how long the CPU may run above " +
              "PL1 before the sustained limit binds. Both are RAPL limits, " +
              "written and restored like the level's own; a burst limit under " +
              "the sustained one is refused. Raising them above the firmware's " +
              "defaults buys nothing where the embedded controller caps the CPU " +
              "on its own (the OMEN 17: 45 W over PECI). An Eco that is on " +
              "takes a change at once."
    }

    SpinBox {
        id: pollSpin
        Kirigami.FormData.label: "Refresh while open:"
        from: 2
        to: 120
        stepSize: 1
        textFromValue: function(v) { return v + " s" }
        valueFromText: function(t) { return parseInt(t) }
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "Collapsed in the panel the widget runs nothing, unless Max " +
              "is on (one small file read every 30 s) or watts are shown " +
              "(one small file read per watts refresh). This interval " +
              "applies only while the popup is open. GPU power draw is read " +
              "only then too, since querying nvidia-smi wakes the discrete GPU."
    }

    Item { Kirigami.FormData.isSection: true }

    ColumnLayout {
        Kirigami.FormData.label: "Fans in Max:"
        spacing: Kirigami.Units.smallSpacing

        RadioButton {
            text: "Auto: the firmware's own fan curve"
            checked: page.fansShown === "auto"
            onToggled: if (checked) fansGroup.value = "auto"
        }
        RadioButton {
            text: "Max: full speed while Max runs"
            checked: page.fansShown === "max"
            onToggled: if (checked) fansGroup.value = "max"
        }
        RadioButton {
            text: "Boost: full speed from 85 °C until 3 minutes below 65 °C"
            checked: page.fansShown === "boost"
            onToggled: if (checked) fansGroup.value = "boost"
        }
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "Max holds the performance profile for this session only and " +
              "re-sends it every 30 s so the laptop's controller keeps the " +
              "full power limits. It ends on logout, reboot, or when you pick " +
              "a power profile by hand, and pauses on battery. On the OMEN 16 " +
              "the controller hands fans at full speed back to automatic only " +
              "after about 2 minutes without Max, so leaving full speed takes " +
              "that long and a switch to Auto or Boost starts with a pause; on " +
              "the OMEN 17 (and other boards outside hp-wmi's Victus-S table) " +
              "automatic is written back at once."
    }

    CheckBox {
        id: gpuBoostBox
        Kirigami.FormData.label: "GPU in Max:"
        text: "Dynamic Boost (nvidia-powerd)"
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "Starts NVIDIA's power daemon for as long as Max runs, unless " +
              "it runs already. On the tested RTX 4060 Laptop the GPU's limit " +
              "went from 80 W to about 130 W in the performance profile."
    }

    TextField {
        id: gpuProfileField
        Kirigami.FormData.label: "GPU profile in Max:"
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        placeholderText: "leave as is"
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "The LACT profile selected for the NVIDIA GPU while Max runs, " +
              "an overclock you defined in LACT, by its exact name, or " +
              "\"default\" for LACT's own; the previous profile comes back " +
              "when Max ends. The popup's GPU clocks card lists the names and " +
              "needs lactd running. Empty leaves the GPU to whatever LACT has " +
              "in force."
    }

    ColumnLayout {
        Kirigami.FormData.label: "Middle click:"
        spacing: Kirigami.Units.smallSpacing

        RadioButton {
            text: "Toggle the last used mode"
            checked: middleGroup.value === "last"
            onToggled: if (checked) middleGroup.value = "last"
        }
        RadioButton {
            text: "Toggle Max"
            checked: middleGroup.value === "max"
            onToggled: if (checked) middleGroup.value = "max"
        }
        RadioButton {
            text: "Toggle Eco"
            checked: middleGroup.value === "eco"
            onToggled: if (checked) middleGroup.value = "eco"
        }
    }

    Item { Kirigami.FormData.isSection: true }

    CheckBox {
        id: showWattsBox
        Kirigami.FormData.label: "Watts:"
        text: "Show CPU power in the panel"
    }

    CheckBox {
        id: wattsGpuBox
        text: "Add the GPU's power while something keeps it busy"
        enabled: showWattsBox.checked
    }

    SpinBox {
        id: wattsSpin
        Kirigami.FormData.label: "Watts refresh:"
        from: 1
        to: 30
        stepSize: 1
        enabled: showWattsBox.checked
        textFromValue: function(v) { return v + " s" }
        valueFromText: function(t) { return parseInt(t) }
    }

    Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: "The CPU energy counter is readable by root only, so a small " +
              "root sampler runs while this is on and publishes one averaged " +
              "reading per interval, plus what limits the CPU right now; the " +
              "widget reads that file. Off means nothing runs. The figure is " +
              "the CPU package, not the whole laptop; on battery the popup " +
              "also shows the laptop's total draw. The GPU's draw is read only " +
              "while another program keeps it awake anyway (\"21·19W\"): " +
              "asking an idle GPU every few seconds would stop it from ever " +
              "powering down."
    }
}
