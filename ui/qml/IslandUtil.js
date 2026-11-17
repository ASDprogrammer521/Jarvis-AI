.pragma library

// Small colour helpers shared by the island's QML (inline components can't see the root's functions).

function col(c) {
    return (typeof c === "string") ? Qt.color(c) : c
}

// linear mix of two colours, t = 0 -> a, t = 1 -> b
function mix(a, b, t) {
    a = col(a)
    b = col(b)
    return Qt.rgba(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t, a.a + (b.a - a.a) * t)
}

// the same colour with another alpha (0..1)
function alpha(c, a) {
    c = col(c)
    return Qt.rgba(c.r, c.g, c.b, a)
}

function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v))
}

// 83 -> "1:23"
function fmt(sec) {
    sec = Math.max(0, Math.floor(sec))
    var s = sec % 60
    return Math.floor(sec / 60) + ":" + (s < 10 ? "0" : "") + s
}
