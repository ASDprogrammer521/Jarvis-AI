// Dynamic Island — Qt Quick edition. Everything here is drawn by the Qt Quick scene graph on the GPU
// (Direct3D 11 on Windows). The OS window never changes size: it is a fixed transparent canvas, and the pill inside it
// springs between its states with SpringAnimation, so the motion is the same at 60, 120 or 144 Hz.
//
// The backend (ui/dynamic_island.py) is the context property `backend`:
//   media     {music, playing, title, artist, album, pos, end, stamp(ms), art(url)}
//   weather   {ok, t, unit, words, icon(url), city, hi, lo, days:[{name, icon, hi, lo}]}
//   notice    {active, icon, text}            a short event: battery, clipboard...
//   today     {month, year, day, weekday, date}
//   calendar  [{d, k}]   k = today | other | weekend | day
//   system    {cpu, mem, gpu, tmp, net(MB/s), batt, plugged}   -1 = no sensor
//   accent    colour of the cover
import QtQuick
import QtQuick.Effects
import QtQuick.Shapes
import "IslandUtil.js" as U

Item {
    id: root
    width: 420
    height: 230

    // ── look & feel (tune here) ─────────────────────────────────────────────────
    readonly property real kSpring: 3.0        // stiffness of the open / close spring
    readonly property real kDampOpen: 0.30     // < 0.4 = a small, lively overshoot when it opens
    readonly property real kDampClose: 0.48    // closing settles with almost no bounce

    readonly property int m: 16
    readonly property real baseW: 158
    readonly property real baseH: 34
    readonly property real openW: 388
    readonly property string ff: "Segoe UI"
    readonly property color cDot: "#ff9f0a"
    readonly property color cMed: "#aab3d9"
    readonly property color cDim: "#6b74a6"
    readonly property color cAcc2: "#ff7ad9"
    readonly property color cRed: "#ff453a"

    // ── data ────────────────────────────────────────────────────────────────────
    readonly property var md: backend.media
    readonly property var wx: backend.weather
    readonly property var nt: backend.notice
    readonly property var today: backend.today
    readonly property var sy: backend.system
    readonly property bool music: md.music === true
    property color accent: backend.accent
    Behavior on accent { ColorAnimation { duration: 650; easing.type: Easing.InOutQuad } }
    property date clockNow: new Date()
    property real nowMs: Date.now()

    // playback position, interpolated here so Python only has to report it when it jumps
    readonly property real pos: {
        var p = (md.pos || 0) + (md.playing ? (nowMs - (md.stamp || nowMs)) / 1000 : 0)
        return (md.end || 0) > 0 ? Math.min(p, md.end) : p
    }
    readonly property real frac: (md.end || 0) > 0 ? U.clamp(pos / md.end, 0, 1) : 0

    // ── state ───────────────────────────────────────────────────────────────────
    property bool pinned: false
    property bool boost: false
    property bool leaving: false
    property bool hoverBlock: false
    property bool pulling: false
    property real pullT: 0
    property int page: 0
    readonly property bool hover: hh.hovered && !hoverBlock
    readonly property var pages: music ? ["player", "calendar", "info", "system"] : ["calendar", "info", "system"]
    readonly property int pi: page % pages.length
    readonly property string cur: pages[pi]
    readonly property bool expanded: pinned || hover || boost || leaving
    readonly property bool noticed: nt.active === true
    readonly property bool hasDays: wx.ok === true && wx.days !== undefined && wx.days.length > 0
    readonly property real restW: noticed ? 252 : baseW
    readonly property real expH: cur === "player" ? 186 : ((cur === "calendar" || cur === "system") ? 168 : (hasDays ? 168 : 126))

    // where the pill wants to be …
    readonly property real tw: pulling ? restW + (openW - restW) * pullT : (expanded ? openW : restW)
    readonly property real th: pulling ? baseH + (expH - baseH) * pullT : (expanded ? expH : baseH)
    // … and where it is right now (a spring glides it there)
    property real cw: tw
    property real ch: th
    Behavior on cw {
        enabled: !root.pulling
        SpringAnimation { spring: root.kSpring; damping: root.expanded ? root.kDampOpen : root.kDampClose; epsilon: 0.08 }
    }
    Behavior on ch {
        enabled: !root.pulling
        SpringAnimation { spring: root.kSpring + 0.4; damping: root.expanded ? root.kDampOpen + 0.04 : root.kDampClose; epsilon: 0.08 }
    }
    // 0 = resting pill, 1 = open
    readonly property real prog: U.clamp((ch - baseH) / (126 - baseH), 0, 1)
    readonly property real rad: Math.min(ch / 2, ch < 60 ? 19 + 11 * prog : 30)

    onHoverChanged: {
        if (hover) {
            leaving = false
            leaveTimer.stop()
        } else {
            leaving = true
            leaveTimer.restart()
        }
    }
    onExpandedChanged: backend.setExpanded(expanded)
    onCurChanged: backend.setPage(cur)
    // flip to the next (+1) or previous (-1) page
    function go(dir) {
        page = (pi + dir + pages.length) % pages.length
        poke(3000)
    }
    Timer { id: leaveTimer; interval: 450; onTriggered: root.leaving = false }
    Timer { id: boostTimer; interval: 4000; onTriggered: root.boost = false }
    function poke(ms) {
        boost = true
        boostTimer.interval = ms
        boostTimer.restart()
    }

    Connections {
        target: backend
        function onTrackChanged() { root.page = 0; root.poke(4000) }
        function onTogglePin() {
            root.pinned = !root.pinned
            if (!root.pinned) { root.hoverBlock = true; root.leaving = false; root.boost = false }
        }
    }

    // the clock: one cheap tick a second; the playback position ticks faster only while it is on screen
    Timer {
        interval: 1000; running: true; repeat: true; triggeredOnStart: true
        onTriggered: root.clockNow = new Date()
    }
    Timer {
        interval: root.expanded ? 33 : 250
        running: root.music && root.md.playing === true
        repeat: true
        onTriggered: root.nowMs = Date.now()
    }

    // ── the window's input region follows the pill (the rest of the canvas is click-through) ──
    function syncMask() {
        var w = pulling ? openW : Math.max(tw, cw)
        var h = pulling ? 186 : Math.max(th, ch)
        backend.setInputRect((width - w) / 2 - 10, m - 4, w + 20, h + 14)
    }
    onTwChanged: { syncMask(); maskTimer.restart() }
    onThChanged: { syncMask(); maskTimer.restart() }
    onPullingChanged: syncMask()
    Timer { id: maskTimer; interval: 900; onTriggered: root.syncMask() }
    Component.onCompleted: { syncMask(); backend.setExpanded(false); backend.setPage(cur) }

    // ── small reusable pieces ───────────────────────────────────────────────────
    component Lbl: Text {
        font.family: "Segoe UI"
        color: "#ffffff"
        textFormat: Text.PlainText
        renderType: Text.QtRendering
        verticalAlignment: Text.AlignVCenter
    }

    component Cover: Item {
        id: cv
        property string src: ""
        property color acc: "#6ee7ff"
        property real corner: 12
        Item {
            anchors.fill: parent
            layer.enabled: true
            layer.effect: MultiEffect { maskEnabled: true; maskSource: cvMask }
            Rectangle {
                anchors.fill: parent
                gradient: Gradient {
                    GradientStop { position: 0; color: U.mix(cv.acc, "#ffffff", 0.1) }
                    GradientStop { position: 1; color: U.mix(cv.acc, "#000000", 0.6) }
                }
                Lbl {
                    anchors.centerIn: parent
                    text: "♪"
                    font.pixelSize: cv.height * 0.5
                    color: Qt.rgba(1, 1, 1, 0.75)
                }
            }
            Image {
                anchors.fill: parent
                source: cv.src
                fillMode: Image.PreserveAspectCrop
                smooth: true
                mipmap: true
                asynchronous: true
                cache: false
                opacity: status === Image.Ready ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: 280 } }
            }
        }
        Item {
            id: cvMask
            anchors.fill: parent
            visible: false
            layer.enabled: true
            Rectangle { anchors.fill: parent; radius: cv.corner }
        }
        Rectangle {
            anchors.fill: parent
            radius: cv.corner
            color: "transparent"
            border.width: 1
            border.color: Qt.rgba(1, 1, 1, 0.16)
        }
    }

    component Equalizer: Item {
        id: eq
        property color acc: "#6ee7ff"
        property bool playing: true
        property int bars: 6
        readonly property real gap: 2
        readonly property real bw: Math.max(2, (width - (bars - 1) * gap) / bars)
        Repeater {
            model: eq.bars
            Rectangle {
                id: bar
                required property int index
                property real amp: eq.height * 0.25
                x: index * (eq.bw + eq.gap)
                width: eq.bw
                radius: width / 2
                color: eq.acc
                anchors.bottom: parent.bottom
                height: Math.max(2, eq.playing ? amp : eq.height * 0.22)
                Behavior on height { enabled: !eq.playing; NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }
                SequentialAnimation on amp {
                    running: eq.playing && eq.visible
                    loops: Animation.Infinite
                    NumberAnimation { to: eq.height * (0.55 + 0.45 * Math.abs(Math.sin(bar.index * 1.7 + 1))); duration: 240 + bar.index * 47; easing.type: Easing.InOutSine }
                    NumberAnimation { to: eq.height * 0.2; duration: 280 + bar.index * 61; easing.type: Easing.InOutSine }
                }
            }
        }
    }

    // ⏮ ⏯ ⏭ drawn as vector shapes so they stay crisp at any scale
    component Glyph: Shape {
        id: gl
        property string kind: "play"
        property color col: "#ffffff"
        property real s: 8
        width: 40
        height: 40
        anchors.centerIn: parent
        preferredRendererType: Shape.CurveRenderer
        function pt(x, y) { return Qt.point(20 + x * s, 20 + y * s) }
        readonly property var p1: {
            switch (kind) {
            case "play":  return [pt(-0.7, -1), pt(-0.7, 1), pt(1.1, 0)]
            case "pause": return [pt(-0.95, -1), pt(-0.2, -1), pt(-0.2, 1), pt(-0.95, 1)]
            case "next":  return [pt(-0.8, -0.9), pt(-0.8, 0.9), pt(0.5, 0)]
            default:      return [pt(0.8, -0.9), pt(0.8, 0.9), pt(-0.5, 0)]
            }
        }
        readonly property var p2: {
            switch (kind) {
            case "pause": return [pt(0.2, -1), pt(0.95, -1), pt(0.95, 1), pt(0.2, 1)]
            case "next":  return [pt(0.55, -0.9), pt(0.85, -0.9), pt(0.85, 0.9), pt(0.55, 0.9)]
            case "prev":  return [pt(-0.85, -0.9), pt(-0.55, -0.9), pt(-0.55, 0.9), pt(-0.85, 0.9)]
            default:      return []
            }
        }
        ShapePath { fillColor: gl.col; strokeColor: "transparent"; PathPolyline { path: gl.p1 } }
        ShapePath { fillColor: gl.col; strokeColor: "transparent"; PathPolyline { path: gl.p2 } }
    }

    component RoundButton: Item {
        id: rb
        property color fill: "#222"
        property bool live: true
        signal clicked()
        default property alias content: holder.data
        scale: ma.pressed ? 0.88 : (ma.containsMouse ? 1.07 : 1.0)
        Behavior on scale { NumberAnimation { duration: 140; easing.type: Easing.OutBack } }
        Rectangle { anchors.fill: parent; radius: width / 2; color: rb.fill }
        Item { id: holder; anchors.fill: parent }
        MouseArea {
            id: ma
            anchors.fill: parent
            enabled: rb.live
            hoverEnabled: true
            onClicked: rb.clicked()
        }
    }

    // ‹ › page buttons on the two sides of the open card
    component Chevron: Item {
        id: cvr
        property int dir: 1                 // 1 = points right (next), -1 = points left (previous)
        property bool live: true
        signal clicked()
        width: 20
        height: 44
        opacity: cma.containsMouse ? 1 : 0.6
        Behavior on opacity { NumberAnimation { duration: 140 } }
        Rectangle {
            anchors.centerIn: parent
            width: 20; height: 20; radius: 10
            scale: cma.pressed ? 0.85 : 1
            Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutBack } }
            color: cma.containsMouse ? Qt.rgba(1, 1, 1, 46 / 255) : Qt.rgba(1, 1, 1, 18 / 255)
        }
        Shape {
            anchors.centerIn: parent
            width: 12; height: 12
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeColor: "#ffffff"
                strokeWidth: 1.8
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                PathPolyline { path: [Qt.point(cvr.dir > 0 ? 4.5 : 7.5, 2.5), Qt.point(cvr.dir > 0 ? 8 : 4, 6), Qt.point(cvr.dir > 0 ? 4.5 : 7.5, 9.5)] }
            }
        }
        MouseArea {
            id: cma
            anchors.fill: parent
            enabled: cvr.live
            hoverEnabled: true
            onClicked: cvr.clicked()
        }
    }

    // one hardware tile: label, big value, optional load bar
    component Stat: Item {
        id: st
        property string label: ""
        property string value: "—"
        property real frac: -1              // 0..1 draws the bar, < 0 hides it
        property color acc: "#30d158"
        property color dim: "#6b74a6"
        Rectangle {
            anchors.fill: parent; radius: 14
            color: Qt.rgba(1, 1, 1, 12 / 255)
            border.width: 1; border.color: Qt.rgba(1, 1, 1, 20 / 255)
        }
        Lbl { x: 11; y: 6; width: parent.width - 22; height: 14; text: st.label; color: st.dim; font.pointSize: 7; font.weight: Font.DemiBold }
        Lbl { x: 11; y: 20; width: parent.width - 22; height: 24; text: st.value; font.pointSize: 13; font.weight: Font.Bold; elide: Text.ElideRight }
        Rectangle {
            visible: st.frac >= 0
            x: 11; y: parent.height - 13; width: parent.width - 22; height: 4; radius: 2
            color: Qt.rgba(1, 1, 1, 34 / 255)
            Rectangle {
                width: Math.max(4, parent.width * Math.min(1, Math.max(0, st.frac))); height: parent.height; radius: 2
                color: st.acc
                Behavior on width { NumberAnimation { duration: 450; easing.type: Easing.OutCubic } }
            }
        }
    }

    // a title that is too long for its box scrolls slowly, with a pause at both ends
    component Marquee: Item {
        id: mq
        property string text: ""
        property color color: "#ffffff"
        property real size: 11
        property int weight: Font.Bold
        property bool live: true
        clip: true
        Lbl {
            id: tl
            height: parent.height
            text: mq.text
            color: mq.color
            font.pointSize: mq.size
            font.weight: mq.weight
            readonly property real over: Math.max(0, implicitWidth - mq.width + 24)
            onOverChanged: if (over <= 0) x = 0
            SequentialAnimation on x {
                running: tl.over > 0 && mq.live && mq.visible
                loops: Animation.Infinite
                PauseAnimation { duration: 1300 }
                NumberAnimation { to: -tl.over; duration: Math.max(1, tl.over / 28 * 1000); easing.type: Easing.InOutSine }
                PauseAnimation { duration: 1300 }
                NumberAnimation { to: 0; duration: Math.max(1, tl.over / 28 * 1000); easing.type: Easing.InOutSine }
            }
        }
    }

    // ── soft shadow ─────────────────────────────────────────────────────────────
    Repeater {
        model: 4
        Rectangle {
            required property int index
            x: card.x - index * 3
            y: card.y - index * 3 + 5
            width: root.cw + index * 6
            height: root.ch + index * 6
            radius: root.rad + index * 3
            color: Qt.rgba(0, 0, 0, (30 - index * 7) / 255)
        }
    }

    // ── the pill ────────────────────────────────────────────────────────────────
    Item {
        id: card
        x: (root.width - width) / 2
        y: root.m
        width: root.cw
        height: root.ch

        HoverHandler {
            id: hh
            onHoveredChanged: if (!hovered) root.hoverBlock = false
        }

        // mouse wheel or two-finger touchpad scroll flips the pages (down / right = next)
        WheelHandler {
            id: wh
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            property real acc: 0
            onWheel: (event) => {
                if (!root.expanded || root.pulling) return
                if (wheelCool.running) { wheelCool.restart(); return }   // still scrolling after a flip: wait for it to stop
                var d = Math.abs(event.angleDelta.x) > Math.abs(event.angleDelta.y) ? -event.angleDelta.x : -event.angleDelta.y
                acc += d
                wheelIdle.restart()
                if (Math.abs(acc) >= 100) {
                    root.go(acc > 0 ? 1 : -1)
                    acc = 0
                    wheelCool.restart()
                }
            }
        }
        Timer { id: wheelIdle; interval: 220; onTriggered: wh.acc = 0 }
        Timer { id: wheelCool; interval: 260 }

        // glass body
        Rectangle {
            id: body
            anchors.fill: parent
            radius: root.rad
            readonly property color tint: root.music ? root.accent : "#2a2f55"
            border.width: 1
            border.color: U.alpha(U.mix("#ffffff", tint, 0.5), 52 / 255)
            gradient: Gradient {
                GradientStop { position: 0; color: U.mix("#05050a", body.tint, 0.12 + 0.18 * root.prog) }
                GradientStop { position: 1; color: U.mix("#000000", body.tint, 0.03 + 0.07 * root.prog) }
            }
        }

        // everything inside is clipped to the rounded body by a GPU mask
        Item {
            id: clipper
            anchors.fill: parent
            layer.enabled: true
            layer.effect: MultiEffect { maskEnabled: true; maskSource: bodyMask }

            // the glow of the cover's colour from the lower left
            Shape {
                anchors.fill: parent
                visible: root.music
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: "transparent"
                    fillGradient: RadialGradient {
                        centerX: card.width * 0.22
                        centerY: card.height * 1.05
                        centerRadius: Math.max(card.width, card.height) * 0.75
                        focalX: centerX
                        focalY: centerY
                        GradientStop { position: 0; color: U.alpha(root.accent, (38 + 32 * root.prog) / 255) }
                        GradientStop { position: 1; color: U.alpha(root.accent, 0) }
                    }
                    startX: 0; startY: 0
                    PathLine { x: card.width; y: 0 }
                    PathLine { x: card.width; y: card.height }
                    PathLine { x: 0; y: card.height }
                }
            }
            // glass sheen along the top edge
            Rectangle {
                width: parent.width
                height: Math.min(parent.height, 26)
                gradient: Gradient {
                    GradientStop { position: 0; color: Qt.rgba(1, 1, 1, 30 / 255) }
                    GradientStop { position: 1; color: Qt.rgba(1, 1, 1, 0) }
                }
            }

            // ── compact ─────────────────────────────────────────────────────────
            Item {
                id: compact
                width: root.cw
                height: root.ch
                opacity: 1 - Math.min(1, root.prog * 2.4)
                visible: opacity > 0.01

                Rectangle {   // the orange dot
                    x: compact.width - 14 - 3.2
                    y: compact.height / 2 - 3.2
                    width: 6.4; height: 6.4; radius: 3.2
                    color: root.cDot
                }

                // a short event (battery, clipboard …)
                Item {
                    anchors.fill: parent
                    opacity: root.noticed ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 220 } }
                    Lbl {
                        x: 12; width: 24; height: parent.height
                        horizontalAlignment: Text.AlignHCenter
                        text: root.nt.icon || ""
                        font.pointSize: 11
                    }
                    Lbl {
                        x: 42; width: parent.width - 42 - 28; height: parent.height
                        text: root.nt.text || ""
                        font.pointSize: 9
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }
                }

                // music: round cover, equaliser, hairline of progress
                Item {
                    anchors.fill: parent
                    opacity: (!root.noticed && root.music) ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 220 } }
                    Cover {
                        x: 8; y: compact.height / 2 - 12; width: 24; height: 24
                        corner: 12
                        src: root.md.art || ""
                        acc: root.accent
                    }
                    Equalizer {
                        x: compact.width - 56; y: compact.height / 2 - 7; width: 30; height: 14
                        acc: root.accent
                        playing: root.md.playing === true
                        bars: 6
                    }
                    Item {
                        visible: (root.md.end || 0) > 0
                        x: 16; y: compact.height - 2.5; width: compact.width - 32; height: 1.5
                        Rectangle { anchors.fill: parent; color: Qt.rgba(1, 1, 1, 22 / 255) }
                        Rectangle { width: parent.width * root.frac; height: parent.height; color: U.alpha(root.accent, 210 / 255) }
                    }
                }

                // time | weather
                Item {
                    anchors.fill: parent
                    opacity: (!root.noticed && !root.music) ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 220 } }
                    Lbl {
                        visible: root.wx.ok !== true
                        x: 0; width: parent.width - 20; height: parent.height
                        horizontalAlignment: Text.AlignHCenter
                        text: Qt.formatTime(root.clockNow, "HH:mm")
                        font.pointSize: 11
                        font.weight: Font.Bold
                    }
                    Item {
                        visible: root.wx.ok === true
                        anchors.fill: parent
                        Lbl {
                            x: 13; width: 48; height: parent.height
                            text: Qt.formatTime(root.clockNow, "HH:mm")
                            font.pointSize: 9
                            font.weight: Font.DemiBold
                        }
                        Rectangle { x: 63; y: parent.height / 2 - 7; width: 1; height: 14; color: Qt.rgba(1, 1, 1, 40 / 255) }
                        Image {
                            x: 71; y: parent.height / 2 - 9; width: 18; height: 18
                            source: root.wx.icon || ""
                            smooth: true; mipmap: true; asynchronous: true
                        }
                        Lbl {
                            x: 93; width: 36; height: parent.height
                            text: (root.wx.t !== undefined ? root.wx.t : "") + "°"
                            color: root.cMed
                            font.pointSize: 9
                            font.weight: Font.DemiBold
                        }
                    }
                }
            }

            // ── expanded ────────────────────────────────────────────────────────
            Item {
                id: exp
                x: (root.cw - root.openW) / 2
                width: root.openW
                height: root.ch
                opacity: root.prog > 0.3 ? Math.pow((root.prog - 0.3) / 0.7, 2) : 0
                visible: opacity > 0.01
                readonly property bool live: root.prog > 0.9

                // page: player
                Item {
                    id: pgPlayer
                    anchors.fill: parent
                    opacity: root.cur === "player" ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 220 } }
                    transform: Translate { y: root.cur === "player" ? 0 : 8; Behavior on y { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } } }

                    Cover {
                        x: 22; y: 22; width: 68; height: 68
                        corner: 14
                        src: root.md.art || ""
                        acc: root.accent
                    }
                    Marquee {
                        x: 106; y: 24; width: root.openW - 106 - 64; height: 22
                        text: root.md.title || "—"
                        size: 11
                        live: exp.live && root.cur === "player"
                    }
                    Lbl {
                        x: 106; y: 47; width: root.openW - 106 - 64; height: 16
                        text: root.md.artist || ""
                        color: U.mix(root.cMed, root.accent, 0.25)
                        font.pointSize: 9
                        elide: Text.ElideRight
                    }
                    Lbl {
                        x: 106; y: 64; width: root.openW - 106 - 64; height: 15
                        text: root.md.album || ""
                        color: root.cDim
                        font.pointSize: 8
                        elide: Text.ElideRight
                    }
                    Equalizer {
                        x: root.openW - 72; y: 28; width: 36; height: 12
                        acc: root.accent
                        playing: root.md.playing === true
                        bars: 8
                        visible: root.cur === "player"
                    }

                    // progress
                    Item {
                        id: progBlock
                        readonly property bool has: (root.md.end || 0) > 0
                        visible: has
                        Lbl {
                            x: 18; y: 99; width: 42; height: 16
                            horizontalAlignment: Text.AlignRight
                            text: U.fmt(root.pos)
                            color: root.cMed
                            font.pointSize: 7
                        }
                        Lbl {
                            x: root.openW - 60; y: 99; width: 44; height: 16
                            text: "-" + U.fmt((root.md.end || 0) - root.pos)
                            color: root.cMed
                            font.pointSize: 7
                        }
                        Rectangle {
                            id: track
                            x: 68; y: 105; width: root.openW - 68 - 66; height: 4; radius: 2
                            color: Qt.rgba(1, 1, 1, 34 / 255)
                            Rectangle {
                                width: Math.max(4, parent.width * root.frac); height: parent.height; radius: 2
                                color: root.accent
                            }
                            Rectangle {
                                x: parent.width * root.frac - width / 2; y: parent.height / 2 - height / 2
                                width: seekMa.pressed ? 11 : 8.4; height: width; radius: width / 2
                                color: "#ffffff"
                                Behavior on width { NumberAnimation { duration: 120 } }
                            }
                            MouseArea {
                                id: seekMa
                                x: 0; y: -9; width: parent.width; height: parent.height + 18
                                enabled: exp.live && root.cur === "player"
                                onClicked: (e) => {
                                    root.poke(3000)
                                    backend.seek(U.clamp(e.x / width, 0, 1) * root.md.end)
                                }
                            }
                        }
                    }

                    // ⏮ ⏯ ⏭
                    readonly property real by: progBlock.has ? 152 : 142
                    RoundButton {
                        x: root.openW / 2 - 66 - 17; y: pgPlayer.by - 17; width: 34; height: 34
                        fill: U.alpha(U.mix("#000000", root.accent, 0.16), 220 / 255)
                        live: exp.live && root.cur === "player"
                        onClicked: { root.poke(3000); backend.control("previous") }
                        Glyph { kind: "prev"; s: 6.1; col: U.mix(root.accent, "#ffffff", 0.15) }
                    }
                    RoundButton {
                        x: root.openW / 2 - 24; y: pgPlayer.by - 24; width: 48; height: 48
                        fill: U.alpha(U.mix("#000000", root.accent, 0.28), 235 / 255)
                        live: exp.live && root.cur === "player"
                        onClicked: { root.poke(3000); backend.control("toggle") }
                        Glyph {
                            kind: "pause"; s: 8.6; col: U.mix(root.accent, "#ffffff", 0.15)
                            opacity: root.md.playing === true ? 1 : 0
                            scale: root.md.playing === true ? 1 : 0.6
                            Behavior on opacity { NumberAnimation { duration: 160 } }
                            Behavior on scale { NumberAnimation { duration: 200; easing.type: Easing.OutBack } }
                        }
                        Glyph {
                            kind: "play"; s: 8.6; col: U.mix(root.accent, "#ffffff", 0.15)
                            opacity: root.md.playing === true ? 0 : 1
                            scale: root.md.playing === true ? 0.6 : 1
                            Behavior on opacity { NumberAnimation { duration: 160 } }
                            Behavior on scale { NumberAnimation { duration: 200; easing.type: Easing.OutBack } }
                        }
                    }
                    RoundButton {
                        x: root.openW / 2 + 66 - 17; y: pgPlayer.by - 17; width: 34; height: 34
                        fill: U.alpha(U.mix("#000000", root.accent, 0.16), 220 / 255)
                        live: exp.live && root.cur === "player"
                        onClicked: { root.poke(3000); backend.control("next") }
                        Glyph { kind: "next"; s: 6.1; col: U.mix(root.accent, "#ffffff", 0.15) }
                    }
                }

                // page: calendar — the day big on the left, the month on the right with today in red
                Item {
                    id: pgCal
                    anchors.fill: parent
                    opacity: root.cur === "calendar" ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 220 } }
                    transform: Translate { y: root.cur === "calendar" ? 0 : 8; Behavior on y { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } } }

                    Lbl { x: 24; y: 18; width: 120; height: 14; text: (root.today.month || "").toUpperCase(); color: root.cRed; font.pointSize: 8; font.weight: Font.Bold }
                    Lbl { x: 24; y: 32; width: 120; height: 14; text: root.today.year || ""; color: root.cDim; font.pointSize: 8 }
                    Lbl { x: 24; y: 50; width: 120; height: 56; text: root.today.day || ""; font.pointSize: 40; font.weight: Font.Bold }
                    Lbl { x: 24; y: root.ch - 38; width: 120; height: 16; text: root.today.weekday || ""; color: root.cDim; font.pointSize: 9 }

                    Item {
                        id: grid
                        readonly property real gx: 150
                        readonly property real cwid: (root.openW - 150 - 34) / 7
                        Repeater {
                            model: ["M", "T", "W", "T", "F", "S", "S"]
                            Lbl {
                                required property int index
                                required property string modelData
                                x: grid.gx + index * grid.cwid; y: 16; width: grid.cwid; height: 14
                                horizontalAlignment: Text.AlignHCenter
                                text: modelData
                                color: index >= 5 ? U.alpha(root.cRed, 190 / 255) : root.cDim
                                font.pointSize: 7
                                font.weight: Font.DemiBold
                            }
                        }
                        Repeater {
                            model: backend.calendar
                            Item {
                                required property int index
                                required property var modelData
                                readonly property real cx: grid.gx + (index % 7) * grid.cwid
                                readonly property real cy: 34 + Math.floor(index / 7) * 17
                                Rectangle {
                                    visible: modelData.k === "today"
                                    x: parent.cx + grid.cwid / 2 - 8.5; y: parent.cy + 8 - 8.5
                                    width: 17; height: 17; radius: 8.5
                                    color: root.cRed
                                }
                                Lbl {
                                    x: parent.cx; y: parent.cy; width: grid.cwid; height: 16
                                    horizontalAlignment: Text.AlignHCenter
                                    text: modelData.d
                                    font.pointSize: 7
                                    font.weight: modelData.k === "today" ? Font.Bold : Font.Normal
                                    color: modelData.k === "other" ? U.alpha(root.cDim, 110 / 255)
                                         : modelData.k === "weekend" ? U.alpha(root.cRed, 170 / 255) : "#ffffff"
                                }
                            }
                        }
                    }
                }

                // page: clock + weather
                Item {
                    id: pgInfo
                    anchors.fill: parent
                    opacity: root.cur === "info" ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 220 } }
                    transform: Translate { y: root.cur === "info" ? 0 : 8; Behavior on y { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } } }

                    Lbl { id: bigTime; x: 24; y: 12; height: 50; text: Qt.formatTime(root.clockNow, "HH:mm"); font.pointSize: 30; font.weight: Font.Bold }
                    Lbl { x: 24 + bigTime.implicitWidth + 4; y: 26; height: 24; text: ":" + Qt.formatTime(root.clockNow, "ss"); color: root.cAcc2; font.pointSize: 12; font.weight: Font.DemiBold }
                    Lbl { x: 24; y: 62; width: 200; height: 20; text: root.today.date || ""; color: root.cMed; font.pointSize: 10 }

                    Item {
                        visible: root.wx.ok === true
                        anchors.fill: parent
                        Image {
                            x: root.openW - 170; y: 8; width: 58; height: 58
                            source: root.wx.icon || ""
                            smooth: true; mipmap: true; asynchronous: true
                        }
                        Lbl {
                            x: root.openW - 106; y: 12; width: 84; height: 34
                            horizontalAlignment: Text.AlignRight
                            text: (root.wx.t !== undefined ? root.wx.t : "") + (root.wx.unit || "°C")
                            font.pointSize: 20; font.weight: Font.Bold
                        }
                        Lbl { x: root.openW - 136; y: 46; width: 114; height: 16; horizontalAlignment: Text.AlignRight; text: root.wx.words || ""; color: root.cMed; font.pointSize: 8 }
                        Lbl {
                            x: root.openW - 232; y: 64; width: 210; height: 18
                            horizontalAlignment: Text.AlignRight
                            text: (root.wx.city || "") + (root.wx.hasHiLo === true ? "  ↑" + root.wx.hi + "° ↓" + root.wx.lo + "°" : "")
                            color: root.cDim; font.pointSize: 8
                            elide: Text.ElideLeft
                        }
                        Repeater {
                            model: root.hasDays ? root.wx.days : []
                            Item {
                                required property int index
                                required property var modelData
                                readonly property real tw_: (root.openW - 44) / Math.max(1, root.wx.days.length)
                                x: 22 + index * tw_; y: 104; width: tw_ - 6; height: 66
                                opacity: root.ch > 140 ? 1 : 0
                                Behavior on opacity { NumberAnimation { duration: 240 } }
                                Rectangle {
                                    anchors.fill: parent; radius: 14
                                    color: Qt.rgba(1, 1, 1, 12 / 255)
                                    border.width: 1; border.color: Qt.rgba(1, 1, 1, 20 / 255)
                                }
                                Lbl { x: 0; y: 4; width: parent.width; height: 14; horizontalAlignment: Text.AlignHCenter; text: modelData.name; color: root.cMed; font.pointSize: 8 }
                                Image { x: parent.width / 2 - 12; y: 18; width: 24; height: 24; source: modelData.icon; smooth: true; mipmap: true; asynchronous: true }
                                Lbl { x: 0; y: 46; width: parent.width; height: 16; horizontalAlignment: Text.AlignHCenter; text: modelData.hi + "° / " + modelData.lo + "°"; font.pointSize: 8; font.weight: Font.DemiBold }
                            }
                        }
                    }
                    Lbl {
                        visible: root.wx.ok !== true
                        x: root.openW - 200; y: 20; width: 180; height: 30
                        horizontalAlignment: Text.AlignRight
                        text: "Weather…"; color: root.cDim; font.pointSize: 10
                    }
                }

                // page: hardware monitor
                Item {
                    id: pgSys
                    anchors.fill: parent
                    opacity: root.cur === "system" ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 220 } }
                    transform: Translate { y: root.cur === "system" ? 0 : 8; Behavior on y { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } } }

                    readonly property real tw_: (root.openW - 48 - 16) / 3
                    readonly property real th_: 64
                    readonly property var s_: root.sy
                    readonly property bool hasCpu: s_.cpu !== undefined
                    readonly property real net_: s_.net !== undefined ? s_.net : 0

                    Stat {
                        x: 24; y: 24; width: pgSys.tw_; height: pgSys.th_
                        label: "CPU"; dim: root.cDim
                        value: pgSys.hasCpu ? Math.round(pgSys.s_.cpu) + "%" : "—"
                        frac: pgSys.hasCpu ? pgSys.s_.cpu / 100 : -1
                        acc: U.load(frac)
                    }
                    Stat {
                        x: 24 + pgSys.tw_ + 8; y: 24; width: pgSys.tw_; height: pgSys.th_
                        label: "RAM"; dim: root.cDim
                        value: pgSys.hasCpu ? Math.round(pgSys.s_.mem) + "%" : "—"
                        frac: pgSys.hasCpu ? pgSys.s_.mem / 100 : -1
                        acc: U.load(frac)
                    }
                    Stat {
                        x: 24 + (pgSys.tw_ + 8) * 2; y: 24; width: pgSys.tw_; height: pgSys.th_
                        label: "GPU"; dim: root.cDim
                        value: (pgSys.hasCpu && pgSys.s_.gpu >= 0) ? Math.round(pgSys.s_.gpu) + "%" : "N/A"
                        frac: (pgSys.hasCpu && pgSys.s_.gpu >= 0) ? pgSys.s_.gpu / 100 : -1
                        acc: U.load(frac)
                    }
                    Stat {
                        x: 24; y: 24 + pgSys.th_ + 8; width: pgSys.tw_; height: pgSys.th_
                        label: "TEMP"; dim: root.cDim
                        value: (pgSys.hasCpu && pgSys.s_.tmp >= 0) ? Math.round(pgSys.s_.tmp) + "°C" : "N/A"
                        frac: (pgSys.hasCpu && pgSys.s_.tmp >= 0) ? (pgSys.s_.tmp - 30) / 60 : -1
                        acc: U.load(frac)
                    }
                    Stat {
                        x: 24 + pgSys.tw_ + 8; y: 24 + pgSys.th_ + 8; width: pgSys.tw_; height: pgSys.th_
                        label: "NETWORK"; dim: root.cDim
                        value: !pgSys.hasCpu ? "—" : (pgSys.net_ >= 1 ? pgSys.net_.toFixed(1) + " MB/s" : Math.round(pgSys.net_ * 1024) + " KB/s")
                        frac: -1
                    }
                    Stat {
                        x: 24 + (pgSys.tw_ + 8) * 2; y: 24 + pgSys.th_ + 8; width: pgSys.tw_; height: pgSys.th_
                        label: pgSys.hasCpu && pgSys.s_.plugged === true ? "BATTERY ⚡" : "BATTERY"; dim: root.cDim
                        value: !pgSys.hasCpu ? "—" : (pgSys.s_.batt >= 0 ? pgSys.s_.batt + "%" : "AC")
                        frac: (pgSys.hasCpu && pgSys.s_.batt >= 0) ? pgSys.s_.batt / 100 : -1
                        acc: U.load(1 - Math.max(0, frac))
                    }
                }

                // page dots at the top: the current one is long; click one to jump there
                Row {
                    id: dots
                    y: 7
                    anchors.horizontalCenter: parent.horizontalCenter
                    spacing: 6
                    Repeater {
                        model: root.pages.length
                        Rectangle {
                            required property int index
                            readonly property bool isCur: index === root.pi
                            width: isCur ? 16 : 5; height: 5; radius: 2.5
                            color: isCur ? "#ffffff" : Qt.rgba(1, 1, 1, 0.3)
                            Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                            Behavior on color { ColorAnimation { duration: 180 } }
                            MouseArea {
                                anchors.fill: parent
                                anchors.margins: -6
                                enabled: exp.live
                                onClicked: { root.page = parent.index; root.poke(3000) }
                            }
                        }
                    }
                }

                // ‹ › buttons on both sides: previous / next page
                Chevron {
                    x: 0; y: root.ch / 2 - height / 2
                    dir: -1
                    live: exp.live
                    onClicked: root.go(-1)
                }
                Chevron {
                    x: root.openW - width; y: root.ch / 2 - height / 2
                    dir: 1
                    live: exp.live
                    onClicked: root.go(1)
                }
            }
        }
        // the mask the clipper uses (a rounded rectangle the size of the pill)
        Item {
            id: bodyMask
            anchors.fill: parent
            visible: false
            layer.enabled: true
            Rectangle { anchors.fill: parent; radius: root.rad }
        }

        // click / pull down / pull up / move
        MouseArea {
            id: drag
            anchors.fill: parent
            z: -1
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            property bool active: false
            property string mode: ""
            property point p0
            property real dx0: 0
            property real dy0: 0
            onPressed: (e) => {
                if (e.button === Qt.RightButton) { backend.showMenu(root.pinned); return }
                active = true
                mode = ""
                p0 = backend.cursor()
                var o = backend.offset()
                dx0 = o.x
                dy0 = o.y
            }
            onPositionChanged: (e) => {
                if (!active) return
                var c = backend.cursor()
                var dx = c.x - p0.x
                var dy = c.y - p0.y
                if (mode === "" && Math.abs(dx) + Math.abs(dy) > 6) {
                    var shift = (e.modifiers & Qt.ShiftModifier) !== 0
                    var side = Math.abs(dx) > Math.abs(dy) * 1.6
                    var open = root.expanded && root.cw > root.openW * 0.9
                    if (shift) mode = "move"                       // Shift+drag always moves the island
                    else if (side) mode = open ? "swipe" : "move"  // sideways: flip pages on the open card, move the closed pill
                    else mode = "pull"                             // up / down: pull open / shut
                    if (mode === "pull" && !root.pinned) root.pulling = true
                }
                if (mode === "move") {
                    backend.moveTo(dx0 + dx, Math.max(0, dy0 + dy))
                } else if (mode === "pull") {
                    if (root.pinned) {                             // open: pull it up to shut it
                        if (dy < -28) {
                            root.pinned = false
                            root.boost = false
                            root.leaving = false
                            root.hoverBlock = true
                            mode = "done"
                        }
                    } else {                                       // shut: the further down, the more open
                        root.pullT = dy > 0 ? U.clamp(Math.pow(dy / 120, 0.9), 0, 1) : 0
                    }
                }
            }
            onReleased: (e) => {
                if (!active) return
                active = false
                var md_ = mode
                mode = ""
                if (md_ === "pull") {
                    if (root.pulling) {
                        root.pinned = root.pullT > 0.4             // pulled far enough: it stays open
                        root.pulling = false
                        root.leaving = true
                        leaveTimer.restart()
                    }
                    root.pullT = 0
                    return
                }
                if (md_ === "done") return
                if (md_ === "swipe") {
                    var c = backend.cursor()
                    var sx = c.x - p0.x
                    if (Math.abs(sx) > 40) root.go(sx < 0 ? 1 : -1)   // swipe left = next, right = previous
                    return
                }
                if (md_ === "move") { backend.saveOffset(); return }
                root.pinned = !root.pinned                          // a plain click
                if (!root.pinned) { root.leaving = true; leaveTimer.restart() }
            }
        }
    }
}
