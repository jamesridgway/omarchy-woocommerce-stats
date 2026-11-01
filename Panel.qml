import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Bar label and popup for one WooCommerce store. All fetching, costing and
// formatting lives in woo_stats.py, so this file only renders its JSON.
Panel {
  id: root
  moduleName: "jamesridgway.woocommerce"
  manageIpc: false

  property var stats: ({ state: "loading", barText: "", tooltip: "", error: "", periods: [] })
  property bool refreshing: false
  property bool forceNext: false
  property bool refreshAgain: false
  property bool settingsOpen: false
  property int hoverIndex: -1

  // Changes show at once, and stay until a status that includes them arrives.
  property var localPrefs: null
  property var unsavedPrefs: null
  property bool awaitingSavedStatus: false

  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("woo_stats.py").toString().replace("file://", ""))
  readonly property string profile: String(setting("profile", "default"))
  readonly property int refreshIntervalSec: Math.max(60, Number(setting("refreshIntervalSec", 300)) || 300)
  readonly property bool needsSetup: stats.state === "not_configured"
  readonly property bool hasError: stats.state === "error" || stats.stale === true
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property var periods: stats.periods || []
  readonly property var catalog: stats.catalog || []
  readonly property var prefs: localPrefs || stats.prefs || ({})
  readonly property var selectedPeriods: prefs.periods || []
  readonly property string metric: prefs.trendMetric || "revenue"
  readonly property var trend: Number(prefs.trendDays) > 0 ? (stats.trend || null) : null
  readonly property var trendDays: trend ? trend.days : []
  readonly property var hoveredDay: hoverIndex >= 0 && hoverIndex < trendDays.length ? trendDays[hoverIndex] : null
  readonly property var metricOptions: [
    { value: "revenue", label: "Revenue" },
    { value: "profit", label: "Profit" },
    { value: "orders", label: "Orders" }
  ]
  readonly property var notes: {
    var seen = stats.notice ? [String(stats.notice)] : []
    for (var i = 0; i < periods.length; i++) {
      var note = String(periods[i].note || "")
      if (note !== "" && seen.indexOf(note) === -1) seen.push(note)
    }
    return seen
  }

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color surface: Commons.Color.popups.background

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    hoverIndex = -1
    if (opened) {
      if (panelFlick) panelFlick.contentY = 0
      refresh()
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
    } else {
      settingsOpen = false
    }
  }
  onSettingsChanged: if (startDelay.ranOnce) refresh()
  onSettingsOpenChanged: if (panelFlick) panelFlick.contentY = 0

  function refresh(force) {
    if (statusProcess.running) {
      if (force) forceNext = true
      else refreshAgain = true
      return
    }
    var command = ["python3", helperPath, "status",
      "--profile", profile,
      "--bar-format", String(setting("barFormat", "󰄐 {revenue} · {orders} · {margin}")),
      "--bar-period", String(setting("barPeriod", "today")),
      "--statuses", String(setting("statuses", "processing,completed")),
      "--cost-meta-keys", String(setting("costMetaKeys", "")),
      "--assumed-margin", String(setting("assumedMarginPercent", 0)),
      "--history-minutes", String(setting("historyRefreshMin", 60)),
      "--max-age", String(Math.floor(refreshIntervalSec * 0.9))]
    if (force) command.push("--force")
    refreshing = true
    statusProcess.command = command
    statusProcess.running = true
  }

  function applyOutput(raw) {
    try {
      stats = JSON.parse(raw)
    } catch (e) {
      stats = { state: "error", barText: "", tooltip: "Could not read WooCommerce stats", error: "Could not read WooCommerce stats", periods: [] }
    }
  }

  function savePrefs(changes) {
    var merged = {}
    var current = prefs
    for (var key in current) merged[key] = current[key]
    for (var change in changes) merged[change] = changes[change]
    localPrefs = merged
    unsavedPrefs = merged
    awaitingSavedStatus = true
    writeNextPrefs()
  }

  function writeNextPrefs() {
    if (prefsProcess.running || !unsavedPrefs) return
    prefsProcess.command = ["python3", helperPath, "prefs", "--profile", profile, "--set", JSON.stringify(unsavedPrefs)]
    unsavedPrefs = null
    prefsProcess.running = true
  }

  // Every monitor has its own widget, and they all need the new preferences.
  function refreshAllInstances() {
    var items = bar && typeof bar.moduleWidgets === "function" ? bar.moduleWidgets(moduleName) : [root]
    for (var i = 0; i < items.length; i++) {
      if (items[i] && items[i] !== root && typeof items[i].refresh === "function") items[i].refresh()
    }
    root.refresh()
  }

  function togglePeriod(key) {
    var next = selectedPeriods.slice()
    var at = next.indexOf(key)
    if (at === -1) next.push(key)
    else if (next.length > 1) next.splice(at, 1)
    savePrefs({ periods: next })
  }

  function cycleMetric() {
    for (var i = 0; i < metricOptions.length; i++) {
      if (metricOptions[i].value === metric) {
        savePrefs({ trendMetric: metricOptions[(i + 1) % metricOptions.length].value })
        return
      }
    }
  }

  function tint(alpha) {
    return Qt.tint(surface, Qt.rgba(foreground.r, foreground.g, foreground.b, alpha))
  }

  function shellQuote(value) {
    return "'" + String(value).replace(/'/g, "'\\''") + "'"
  }

  function openSetup() {
    close()
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
      "python3 " + shellQuote(helperPath) + " setup --profile " + shellQuote(profile)])
  }

  function openAdmin() {
    if (stats.store && stats.store.adminUrl) Qt.openUrlExternally(stats.store.adminUrl)
  }

  // Settings are injected just after load, so the first poll waits for them.
  Timer {
    id: startDelay
    property bool ranOnce: false
    interval: 300
    running: true
    onTriggered: {
      ranOnce = true
      root.refresh()
    }
  }

  Timer {
    // Checking for a finished setup is local and cheap, so it can run often.
    interval: root.needsSetup ? 15000 : root.refreshIntervalSec * 1000
    repeat: true
    running: startDelay.ranOnce
    onTriggered: root.refresh()
  }

  Process {
    id: statusProcess
    running: false
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    onExited: {
      root.refreshing = false
      root.applyOutput(statusOut.text)
      if (root.forceNext) {
        root.forceNext = false
        root.refreshAgain = false
        root.refresh(true)
      } else if (root.refreshAgain) {
        root.refreshAgain = false
        root.refresh()
      } else if (root.awaitingSavedStatus && !prefsProcess.running && !root.unsavedPrefs) {
        root.awaitingSavedStatus = false
        root.localPrefs = null
      }
    }
  }

  Process {
    id: prefsProcess
    running: false
    onExited: {
      if (root.unsavedPrefs) root.writeNextPrefs()
      else root.refreshAllInstances()
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical || root.stats.barText === "" ? "󰄐" : root.stats.barText
    active: root.hasError
    tooltipText: root.opened ? "" : String(root.stats.tooltip || "")
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) root.refresh(true)
      else if (buttonCode === Qt.RightButton) root.openAdmin()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(680))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: {
        if (root.settingsOpen) root.settingsOpen = false
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh(true)
        else if (t === "o" || t === "O") root.openAdmin()
        else if ((t === "s" || t === "S") && root.catalog.length > 0) root.settingsOpen = !root.settingsOpen
        else if ((t === "m" || t === "M") && root.trend) root.cycleMetric()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          Item {
            width: parent.width
            implicitHeight: Math.max(hero.implicitHeight, gearButton.implicitHeight)

            PanelHero {
              id: hero
              width: parent.width - gearButton.width - Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              title: root.stats.store ? root.stats.store.name : "WooCommerce"
              meta: root.refreshing ? "Refreshing…" : (root.stats.updated ? "Updated " + root.stats.updated : "")
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconComponent: Component {
                Text {
                  text: "󰄐"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
            }

            PanelActionButton {
              id: gearButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: root.catalog.length > 0
              iconText: root.settingsOpen ? "󰁍" : "󰒓"
              tooltipText: root.settingsOpen ? "Back" : "Panel settings"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.settingsOpen = !root.settingsOpen
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.needsSetup
            width: parent.width
            text: "Connect a store with a read-only WooCommerce API key."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            visible: root.hasError && String(root.stats.error || "") !== ""
            width: parent.width
            text: root.stats.stale ? root.stats.error + ". Showing the last figures." : String(root.stats.error || "")
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Button {
            visible: root.needsSetup || root.stats.errorKind === "auth" || root.stats.errorKind === "config"
            bordered: true
            text: root.needsSetup ? "Set up store" : "Run setup again"
            onClicked: root.openSetup()
          }

          SettingsView {
            visible: root.settingsOpen
            width: parent.width
          }

          PanelSeparator {
            visible: !root.settingsOpen && root.trend !== null
            foreground: root.foreground
          }

          TrendView {
            visible: !root.settingsOpen && root.trend !== null
            width: parent.width
          }

          PanelSeparator {
            visible: !root.settingsOpen && root.periods.length > 0
            foreground: root.foreground
          }

          GridLayout {
            visible: !root.settingsOpen && root.periods.length > 0
            width: parent.width
            columns: 5
            columnSpacing: Style.space(10)
            rowSpacing: Style.space(8)

            Repeater {
              model: ["", "Revenue", "Orders", "Profit", "Margin"]
              PanelSectionHeader {
                required property string modelData
                required property int index
                text: modelData
                foreground: root.foreground
                fontFamily: root.fontFamily
                Layout.fillWidth: index === 0
                Layout.alignment: index === 0 ? Qt.AlignLeft : Qt.AlignRight
              }
            }

            Repeater {
              model: {
                var cells = []
                for (var i = 0; i < root.periods.length; i++) {
                  var period = root.periods[i]
                  cells.push({ value: period.label, first: true })
                  cells.push({ value: period.revenue, first: false })
                  cells.push({ value: period.orders, first: false })
                  cells.push({ value: period.profit, first: false })
                  cells.push({ value: period.margin, first: false })
                }
                return cells
              }
              Text {
                required property var modelData
                textFormat: Text.PlainText
                text: modelData.value
                color: modelData.first ? root.dim : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                Layout.fillWidth: modelData.first
                Layout.alignment: modelData.first ? Qt.AlignLeft : Qt.AlignRight
              }
            }
          }

          Repeater {
            model: root.settingsOpen ? [] : root.notes
            Text {
              required property string modelData
              textFormat: Text.PlainText
              width: column.width
              text: modelData
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }
        }
      }
    }
  }

  // One metric at a time on one axis. The readout above the chart shows every
  // value for the hovered day, so nothing depends on reading a bar's height.
  component TrendView: Column {
    spacing: Style.space(8)

    ButtonGroup {
      options: root.metricOptions
      value: root.metric
      focusable: false
      foreground: root.foreground
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onChanged: function(value) { root.savePrefs({ trendMetric: value }) }
    }

    RowLayout {
      width: parent.width
      spacing: Style.space(10)

      Text {
        textFormat: Text.PlainText
        text: root.hoveredDay ? root.hoveredDay.label : (root.trend ? root.trend.label : "")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideLeft
        text: {
          if (root.hoveredDay) return root.hoveredDay.detail
          if (!root.trend) return ""
          var total = root.trend.totals[root.metric]
          return root.metric === "orders" ? total + " orders" : total
        }
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
    }

    TrendChart {
      width: parent.width
      height: Style.space(96)
    }

    Item {
      width: parent.width
      height: firstDate.implicitHeight

      Text {
        id: firstDate
        textFormat: Text.PlainText
        anchors.left: parent.left
        text: root.trendDays.length > 0 ? root.trendDays[0].short : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.rightMargin: Style.space(46)
        text: root.trendDays.length > 0 ? root.trendDays[root.trendDays.length - 1].short : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  component TrendChart: Item {
    id: chart

    readonly property real gutter: Style.space(46)
    readonly property real plotWidth: width - gutter
    readonly property var axis: root.trend ? root.trend.axes[root.metric] : ({ top: 1, bottom: 0, topText: "", bottomText: "" })
    readonly property real span: Math.max(1, axis.top - axis.bottom)
    readonly property real zeroY: height * axis.top / span
    readonly property real slot: root.trendDays.length > 0 ? plotWidth / root.trendDays.length : plotWidth
    readonly property real gap: slot >= 6 ? 2 : 1
    readonly property bool empty: {
      for (var i = 0; i < root.trendDays.length; i++) {
        var value = root.trendDays[i][root.metric]
        if (value !== null && value !== undefined) return false
      }
      return true
    }

    AxisLine { y: 0; visible: chart.axis.top > 0; label: chart.axis.topText }
    AxisLine { y: chart.zeroY; label: "0"; strong: true }
    AxisLine { y: chart.height - 1; visible: chart.axis.bottom < 0; label: chart.axis.bottomText }

    Repeater {
      model: root.trendDays

      Item {
        id: dayColumn
        required property var modelData
        required property int index

        readonly property var rawValue: modelData[root.metric]
        readonly property bool known: rawValue !== null && rawValue !== undefined
        readonly property real value: known ? Number(rawValue) : 0
        readonly property real barHeight: value === 0 ? 0 : Math.max(2, Math.abs(value) / chart.span * chart.height)
        readonly property color fill: root.hoverIndex === index ? root.foreground : root.tint(modelData.today ? 0.4 : 0.72)

        x: index * chart.slot
        width: chart.slot
        height: chart.height

        // Rounded at the data end, square where the bar meets the baseline.
        Rectangle {
          id: barMark
          visible: dayColumn.barHeight > 0
          anchors.horizontalCenter: parent.horizontalCenter
          width: Math.min(24, Math.max(1, chart.slot - chart.gap))
          height: dayColumn.barHeight
          y: dayColumn.value >= 0 ? chart.zeroY - height : chart.zeroY
          radius: Math.min(3, width / 2, height / 2)
          color: dayColumn.fill
        }

        Rectangle {
          visible: barMark.visible
          anchors.horizontalCenter: parent.horizontalCenter
          width: barMark.width
          height: barMark.height / 2
          y: dayColumn.value >= 0 ? chart.zeroY - height : chart.zeroY
          color: dayColumn.fill
        }

        MouseArea {
          anchors.fill: parent
          hoverEnabled: true
          onEntered: root.hoverIndex = dayColumn.index
          onExited: if (root.hoverIndex === dayColumn.index) root.hoverIndex = -1
        }
      }
    }

    Text {
      visible: chart.empty
      textFormat: Text.PlainText
      anchors.centerIn: parent
      anchors.horizontalCenterOffset: -chart.gutter / 2
      text: "No cost data to chart"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  component AxisLine: Item {
    id: axisLine
    property string label: ""
    property bool strong: false
    width: parent.width
    height: 1

    Rectangle {
      width: parent.width - Style.space(46)
      height: 1
      color: root.tint(axisLine.strong ? 0.35 : 0.14)
    }

    Text {
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: axisLine.label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  component SettingsView: Column {
    spacing: Style.space(12)

    PanelSectionHeader {
      text: "BREAKDOWNS"
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Flow {
      width: parent.width
      spacing: Style.space(6)

      Repeater {
        model: root.catalog
        Button {
          required property var modelData
          readonly property bool chosen: root.selectedPeriods.indexOf(modelData.key) !== -1
          text: (chosen ? "󰄬 " : "") + modelData.label
          selected: chosen
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: root.togglePeriod(modelData.key)
        }
      }
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      text: "Long ranges fetch more orders, so they can take a while to load the first time."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    PanelSeparator { foreground: root.foreground }

    PanelSectionHeader {
      text: "TREND"
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    ButtonGroup {
      options: [
        { value: "0", label: "Off" },
        { value: "7", label: "7 days" },
        { value: "14", label: "14" },
        { value: "30", label: "30" },
        { value: "90", label: "90" }
      ]
      value: String(root.prefs.trendDays)
      focusable: false
      foreground: root.foreground
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onChanged: function(value) { root.savePrefs({ trendDays: Number(value) }) }
    }

    PanelSeparator { foreground: root.foreground }

    PanelSectionHeader {
      text: "WEEK STARTS ON"
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    ButtonGroup {
      options: [
        { value: "monday", label: "Monday" },
        { value: "sunday", label: "Sunday" }
      ]
      value: String(root.prefs.weekStart)
      focusable: false
      foreground: root.foreground
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onChanged: function(value) { root.savePrefs({ weekStart: value }) }
    }
  }
}
