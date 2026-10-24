// SPDX-License-Identifier: GPL-2.0-or-later
import QtQuick
import org.kde.plasma.workspace.dbus as DBus

// power-profiles-daemon's ActiveProfile, as a property that changes on the
// daemon's own signal. Loaded through a Loader in main.qml: on a Plasma
// without this D-Bus module the widget still loads, only without following.
Item {
    readonly property string profile: ppd.properties.ActiveProfile || ""

    DBus.Properties {
        id: ppd
        busType: DBus.BusType.System
        service: "org.freedesktop.UPower.PowerProfiles"
        path: "/org/freedesktop/UPower/PowerProfiles"
        iface: "org.freedesktop.UPower.PowerProfiles"
    }
}
