import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Launch.js" as Launch

BarWidget {
  id: root
  moduleName: "community.plaud-linux"

  // Metadata only. No account state, file access, network or background work.
  readonly property var application: Launch.entry(DesktopEntries)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function openCard() {
    return Launch.open(DesktopEntries)
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf130"
    slotSize: Style.bar.statusSlot
    dimmed: root.application === null
    tooltipText: root.application
      ? "Plaud Linux — abrir controles"
      : "Instale o Plaud Linux com install.sh para abrir os controles"
    Accessible.role: Accessible.Button
    Accessible.name: tooltipText
    Accessible.onPressAction: root.openCard()
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.LeftButton) root.openCard()
    }
  }
}
