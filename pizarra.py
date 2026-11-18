#!/usr/bin/env python3
"""Pizarra: dibuja encima de toda la pantalla (Hyprland / Wayland).

Uso:
    pizarra.py            abre la pizarra (si ya está abierta, no hace nada)
    pizarra.py --toggle   abre la pizarra, o la cierra si ya está abierta

Ratón:
    clic izquierdo   dibujar con la herramienta activa
    clic derecho     borrar (arrastra encima de los trazos)
    rueda            grosor del trazo
    clic izquierdo en el borde de una figura (o muy cerca) y arrastrar: moverla
    mantener pulsado dentro de una figura (sin mover el ratón) y arrastrar: moverla
    Shift            líneas/flechas a 15°, círculo y cuadrado perfectos;
                     al mover, solo en horizontal o en vertical

Teclado:
    1 / P  lápiz      2 / L  línea      3 / A  flecha
    4 / O  círculo    5 / R  rectángulo
    flechas          mover la última figura movida 1 px (Shift: 10 px)
    + / -  grosor     H  ocultar barra  Supr  borrar todo
    T      estilo de las figuras nuevas: a mano marcado → a mano → limpio
    Ctrl+Z deshacer   Ctrl+Shift+Z / Ctrl+Y  rehacer
    Esc    salir
"""
import math
import os
import random
import signal
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
import cairo  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell  # noqa: E402

PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "pizarra.pid")

TOOLS = ["pen", "line", "arrow", "ellipse", "rect"]
TOOL_KEYS = {
    "1": "pen", "p": "pen",
    "2": "line", "l": "line",
    "3": "arrow", "a": "arrow",
    "4": "ellipse", "o": "ellipse",
    "5": "rect", "r": "rect",
}
ARROW_KEYS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}
COLORS = [
    (0.94, 0.27, 0.27),  # rojo
    (0.98, 0.80, 0.18),  # amarillo
    (0.25, 0.80, 0.45),  # verde
    (0.30, 0.62, 1.00),  # azul
    (1.00, 1.00, 1.00),  # blanco
]
WIDTHS = [2, 4, 7, 12]
ACCENT = (0.30, 0.62, 1.00)
ERASER_RADIUS = 10
MAX_UNDO = 200
GRAB_RADIUS = 8  # px alrededor del borde de una figura que la agarran al instante
HOLD_MS = 250    # tiempo pulsando dentro de una figura para agarrarla
HOLD_SLOP = 8    # px que se puede mover el ratón mientras tanto
# Estilo de las figuras: 0 limpio, 1 a mano, 2 a mano marcado (el nivel de «roughness»)
STYLE_ORDER = [2, 1, 0]  # orden al pulsar T o el botón de la barra
DEFAULT_STYLE = 2  # lo más intenso


# ---------------------------------------------------------------- geometría

def dist_seg(p, a, b):
    """Distancia del punto p al segmento ab."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def arrow_head(p0, p1, width):
    """Los dos extremos de la punta de flecha que termina en p1."""
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    size = max(14, width * 4)
    spread = math.pi / 7
    return [
        (p1[0] - size * math.cos(ang - spread), p1[1] - size * math.sin(ang - spread)),
        (p1[0] - size * math.cos(ang + spread), p1[1] - size * math.sin(ang + spread)),
    ]


def ellipse_box(p0, p1):
    cx, cy = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    rx, ry = abs(p1[0] - p0[0]) / 2, abs(p1[1] - p0[1]) / 2
    return cx, cy, rx, ry


def segments(shape):
    """Contorno de una figura como lista de segmentos (para el borrador)."""
    t, pts = shape["tool"], shape["pts"]
    if t == "pen":
        if len(pts) == 1:
            return [(pts[0], pts[0])]
        return list(zip(pts, pts[1:]))
    p0, p1 = pts
    if t == "line":
        return [(p0, p1)]
    if t == "arrow":
        h1, h2 = arrow_head(p0, p1, shape["width"])
        return [(p0, p1), (h1, p1), (h2, p1)]
    if t == "rect":
        a, b, c, d = p0, (p1[0], p0[1]), p1, (p0[0], p1[1])
        return [(a, b), (b, c), (c, d), (d, a)]
    if t == "ellipse":
        cx, cy, rx, ry = ellipse_box(p0, p1)
        n = 72
        ring = [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n))
                for i in range(n + 1)]
        return list(zip(ring, ring[1:]))
    return []


def hits(shape, p, radius):
    tol = radius + shape["width"] / 2
    return any(dist_seg(p, a, b) <= tol for a, b in segments(shape))


def inside(shape, p):
    """True si p está dentro de un círculo o rectángulo (para poder agarrarlo por el centro)."""
    t = shape["tool"]
    if t not in ("rect", "ellipse"):
        return False
    p0, p1 = shape["pts"]
    if t == "rect":
        return (min(p0[0], p1[0]) <= p[0] <= max(p0[0], p1[0])
                and min(p0[1], p1[1]) <= p[1] <= max(p0[1], p1[1]))
    cx, cy, rx, ry = ellipse_box(p0, p1)
    if rx < 1 or ry < 1:
        return False
    return ((p[0] - cx) / rx) ** 2 + ((p[1] - cy) / ry) ** 2 <= 1


def bbox(shape):
    """Caja (x0, y0, x1, y1) que contiene la figura, incluido el grosor."""
    pts = [q for seg in segments(shape) for q in seg]
    m = shape["width"] / 2
    return (min(x for x, _ in pts) - m, min(y for _, y in pts) - m,
            max(x for x, _ in pts) + m, max(y for _, y in pts) + m)


def translated(shape, dx, dy):
    """Copia de la figura desplazada (dx, dy). No modifica la original,
    así el historial de deshacer conserva la posición anterior."""
    return {**shape, "pts": [(x + dx, y + dy) for x, y in shape["pts"]]}


def constrain(tool, p0, p):
    """Con Shift: ángulos de 15° para líneas, proporción 1:1 para figuras."""
    dx, dy = p[0] - p0[0], p[1] - p0[1]
    if tool in ("line", "arrow"):
        step = math.pi / 12
        ang = round(math.atan2(dy, dx) / step) * step
        length = math.hypot(dx, dy)
        return (p0[0] + length * math.cos(ang), p0[1] + length * math.sin(ang))
    if tool in ("rect", "ellipse"):
        side = max(abs(dx), abs(dy))
        return (p0[0] + math.copysign(side, dx), p0[1] + math.copysign(side, dy))
    return p


# ---------------------------------------------------------------- estilo a mano
# Versión reducida del algoritmo de rough.js (el estilo de Excalidraw): cada trazo
# se dibuja dos veces con pequeñas desviaciones. Cada figura guarda una semilla,
# así su forma no tiembla al redibujar y se conserva idéntica al moverla.

class Rough:
    def __init__(self, seed, roughness):
        self.rng = random.Random(seed)
        self.r = roughness

    def off(self, x, gain=1.0):
        """Desviación aleatoria en [-x, x], escalada por la rugosidad."""
        return self.r * gain * (self.rng.random() * 2 * x - x)

    def between(self, lo, hi):
        return self.r * (self.rng.random() * (hi - lo) + lo)

    def line(self, ops, a, b, overlay=False):
        """Una pasada de línea a mano: Bézier con extremos desviados y algo de arco."""
        (x1, y1), (x2, y2) = a, b
        lsq = (x2 - x1) ** 2 + (y2 - y1) ** 2
        length = math.sqrt(lsq)
        if length < 200:
            gain = 1.0
        elif length > 500:
            gain = 0.4
        else:
            gain = -0.0016668 * length + 1.233334
        offset = 2.0
        if offset * offset * 100 > lsq:
            offset = length / 10
        o = offset / 2 if overlay else offset
        diverge = 0.2 + self.rng.random() * 0.2
        mdx = self.off(2 * (y2 - y1) / 200, gain)
        mdy = self.off(2 * (x1 - x2) / 200, gain)
        r = lambda: self.off(o, gain)  # noqa: E731
        ops.append(("M", (x1 + r(), y1 + r())))
        ops.append(("C",
                    (mdx + x1 + (x2 - x1) * diverge + r(), mdy + y1 + (y2 - y1) * diverge + r()),
                    (mdx + x1 + 2 * (x2 - x1) * diverge + r(), mdy + y1 + 2 * (y2 - y1) * diverge + r()),
                    (x2 + r(), y2 + r())))

    def double_line(self, ops, a, b):
        self.line(ops, a, b)
        self.line(ops, a, b, overlay=True)

    @staticmethod
    def curve(ops, pts):
        """Curva suave (Catmull-Rom) que pasa por pts[1] … pts[-2]."""
        ops.append(("M", pts[1]))
        for i in range(1, len(pts) - 2):
            p0, p1, p2, p3 = pts[i - 1:i + 3]
            ops.append(("C",
                        (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6),
                        (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6),
                        p2))

    def ellipse(self, ops, cx, cy, rx, ry):
        size = math.sqrt(2 * math.pi * math.sqrt((rx * rx + ry * ry) / 2))
        steps = math.ceil(max(9, 9 / math.sqrt(200) * size))
        inc = 2 * math.pi / steps
        rx += self.off(rx * 0.05)
        ry += self.off(ry * 0.05)
        overlap = inc * self.between(0.1, self.between(0.4, 1))
        for o, ov in ((1, overlap), (1.5, 0)):
            start = self.off(0.5) - math.pi / 2
            pt = lambda k, ang: (self.off(o) + cx + k * rx * math.cos(ang),  # noqa: E731
                                 self.off(o) + cy + k * ry * math.sin(ang))
            pts = [pt(0.9, start - inc)]
            ang = start
            while ang < 2 * math.pi + start - 0.01:
                pts.append(pt(1, ang))
                ang += inc
            pts += [pt(1, start + 2 * math.pi + ov * 0.5), pt(0.98, start + ov), pt(0.9, start + ov * 0.5)]
            self.curve(ops, pts)

    def rounded_rect(self, ops, x0, y0, x1, y1):
        w, h = x1 - x0, y1 - y0
        rad = min(0.25 * min(w, h), 32)
        sides = [((x0 + rad, y0), (x1 - rad, y0)), ((x1, y0 + rad), (x1, y1 - rad)),
                 ((x1 - rad, y1), (x0 + rad, y1)), ((x0, y1 - rad), (x0, y0 + rad))]
        corners = [((x1 - rad, y0), (x1, y0), (x1, y0 + rad)),
                   ((x1, y1 - rad), (x1, y1), (x1 - rad, y1)),
                   ((x0 + rad, y1), (x0, y1), (x0, y1 - rad)),
                   ((x0, y0 + rad), (x0, y0), (x0 + rad, y0))]
        for a, b in sides:
            self.double_line(ops, a, b)
        if rad < 1:
            return
        for p0, q, p2 in corners:
            for o in (1.0, 0.5):
                j = lambda p: (p[0] + self.off(o), p[1] + self.off(o))  # noqa: E731
                p0j, qj, p2j = j(p0), j(q), j(p2)
                ops.append(("M", p0j))
                ops.append(("C",
                            (p0j[0] + 2 / 3 * (qj[0] - p0j[0]), p0j[1] + 2 / 3 * (qj[1] - p0j[1])),
                            (p2j[0] + 2 / 3 * (qj[0] - p2j[0]), p2j[1] + 2 / 3 * (qj[1] - p2j[1])),
                            p2j))


def rough_ops(s):
    """Trazos a mano de una figura (en caché mientras no cambie)."""
    key = (tuple(s["pts"]), s["width"], s["rough"])
    cached = s.get("_rough")
    if cached and cached[0] == key:
        return cached[1]
    g, ops = Rough(s["seed"], s["rough"]), []
    t, (p0, p1) = s["tool"], s["pts"]
    if t in ("line", "arrow"):
        g.double_line(ops, p0, p1)
        if t == "arrow" and p0 != p1:
            for h in arrow_head(p0, p1, s["width"]):
                g.double_line(ops, h, p1)
    elif t == "rect":
        g.rounded_rect(ops, min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1]))
    elif t == "ellipse":
        g.ellipse(ops, *ellipse_box(p0, p1))
    s["_rough"] = (key, ops)
    return ops


def draw_ops(cr, ops):
    for op in ops:
        if op[0] == "M":
            cr.move_to(*op[1])
        else:
            cr.curve_to(*op[1], *op[2], *op[3])
    cr.stroke()


# ---------------------------------------------------------------- dibujo

def draw_shape(cr, s):
    cr.set_source_rgb(*s["color"])
    cr.set_line_width(s["width"])
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    t, pts = s["tool"], s["pts"]

    if t == "pen":
        cr.move_to(*pts[0])
        if len(pts) < 3:
            for p in pts[1:] or pts:
                cr.line_to(*p)
        else:
            # curva suavizada: cuadráticas por los puntos medios
            for i in range(1, len(pts) - 1):
                q = pts[i]
                e = ((pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2)
                s0 = cr.get_current_point()
                cr.curve_to(s0[0] + 2 / 3 * (q[0] - s0[0]), s0[1] + 2 / 3 * (q[1] - s0[1]),
                            e[0] + 2 / 3 * (q[0] - e[0]), e[1] + 2 / 3 * (q[1] - e[1]),
                            e[0], e[1])
            cr.line_to(*pts[-1])
        cr.stroke()
        return

    if s.get("rough"):
        draw_ops(cr, rough_ops(s))
        return

    p0, p1 = pts
    if t in ("line", "arrow"):
        cr.move_to(*p0)
        cr.line_to(*p1)
        cr.stroke()
        if t == "arrow" and p0 != p1:
            h1, h2 = arrow_head(p0, p1, s["width"])
            cr.move_to(*h1)
            cr.line_to(*p1)
            cr.line_to(*h2)
            cr.stroke()
    elif t == "rect":
        cr.rectangle(min(p0[0], p1[0]), min(p0[1], p1[1]), abs(p1[0] - p0[0]), abs(p1[1] - p0[1]))
        cr.stroke()
    elif t == "ellipse":
        cx, cy, rx, ry = ellipse_box(p0, p1)
        cr.save()
        cr.translate(cx, cy)
        cr.scale(max(rx, 0.5), max(ry, 0.5))
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw_icon(cr, kind, x, y, s):
    """Iconos de la barra, dibujados con líneas dentro de un cuadro s×s."""
    i = s * 0.27
    ix, iy, iw = x + i, y + i, s - 2 * i
    cr.set_source_rgb(0.92, 0.92, 0.95)
    cr.set_line_width(2)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if kind == "pen":
        cr.move_to(ix, iy + iw * 0.75)
        cr.curve_to(ix + iw * 0.3, iy - iw * 0.1, ix + iw * 0.6, iy + iw * 1.1, ix + iw, iy + iw * 0.25)
    elif kind == "line":
        cr.move_to(ix, iy + iw)
        cr.line_to(ix + iw, iy)
    elif kind == "arrow":
        cr.move_to(ix, iy + iw)
        cr.line_to(ix + iw, iy)
        cr.move_to(ix + iw * 0.45, iy)
        cr.line_to(ix + iw, iy)
        cr.line_to(ix + iw, iy + iw * 0.55)
    elif kind == "ellipse":
        cr.arc(ix + iw / 2, iy + iw / 2, iw / 2, 0, 2 * math.pi)
    elif kind == "rect":
        cr.rectangle(ix, iy + iw * 0.1, iw, iw * 0.8)
    elif kind == "undo":
        cx, cy, r = ix + iw / 2, iy + iw * 0.6, iw * 0.45
        cr.arc(cx, cy, r, math.pi, 2.25 * math.pi)
        cr.move_to(cx - r - iw * 0.22, cy - iw * 0.25)
        cr.line_to(cx - r, cy + iw * 0.05)
        cr.line_to(cx - r + iw * 0.25, cy - iw * 0.2)
    elif kind == "clear":
        cr.move_to(ix - 1, iy + iw * 0.15)
        cr.line_to(ix + iw + 1, iy + iw * 0.15)
        cr.move_to(ix + iw * 0.35, iy)
        cr.line_to(ix + iw * 0.65, iy)
        cr.stroke()
        rounded_rect(cr, ix + iw * 0.12, iy + iw * 0.15, iw * 0.76, iw * 0.85, 2)
    cr.stroke()


def make_arrow_cursor(display):
    """Flecha tipo puntero, pero azul, con borde blanco y la cola en ángulo."""
    size = 32
    ox, oy = 3, 3  # la punta (hotspot)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    # punta, borde izquierdo, muesca, cola inclinada, ala derecha
    poly = [(0, 0), (0, 19), (5, 14.5), (9, 22.5), (12.5, 21), (8.5, 13), (14.5, 13)]

    def path(dx, dy):
        cr.move_to(ox + dx + poly[0][0], oy + dy + poly[0][1])
        for x, y in poly[1:]:
            cr.line_to(ox + dx + x, oy + dy + y)
        cr.close_path()

    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    # sombra suave
    path(1.2, 1.5)
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.fill()
    # cuerpo azul con borde blanco
    path(0, 0)
    cr.set_source_rgb(*ACCENT)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(1.6)
    cr.stroke()
    # brillo en el lado izquierdo
    cr.move_to(ox + 2.2, oy + 5)
    cr.line_to(ox + 2.2, oy + 13.5)
    cr.set_source_rgba(1, 1, 1, 0.45)
    cr.set_line_width(1.2)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.stroke()
    try:
        return Gdk.Cursor.new_from_surface(display, surf, ox, oy)
    except Exception:
        return Gdk.Cursor.new_from_name(display, "crosshair")


# ---------------------------------------------------------------- ventana

class Pizarra(Gtk.Window):
    def __init__(self):
        super().__init__(title="pizarra")
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "pizarra")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                     GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self, edge, True)
        GtkLayerShell.set_exclusive_zone(self, -1)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.EXCLUSIVE)

        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)

        self.shapes = []
        self.undo_stack = []
        self.redo_stack = []
        self.current = None
        self.erasing = False
        self.erased_in_drag = False
        self.tool = "pen"
        self.color = COLORS[3]  # azul
        self.width = 4
        self.style = DEFAULT_STYLE
        self.mouse = (-100, -100)
        self.show_bar = True
        self.bar_rect = (0, 0, 0, 0)
        self.bar_items = []
        self.cursor_kind = None
        self.selected = None  # última figura agarrada (se mueve también con las flechas)
        self.drag = None      # arrastre en curso: punto inicial y figura original
        self.hold = None      # pulsación sobre una figura, esperando HOLD_MS

        area = Gtk.DrawingArea()
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        area.connect("draw", self.on_draw)
        area.connect("button-press-event", self.on_press)
        area.connect("button-release-event", self.on_release)
        area.connect("motion-notify-event", self.on_motion)
        area.connect("scroll-event", self.on_scroll)
        self.connect("key-press-event", self.on_key)
        self.connect("destroy", Gtk.main_quit)
        self.add(area)
        self.area = area

        self.show_all()
        display = self.get_display()
        self.cursors = {
            "draw": make_arrow_cursor(display),
            "default": Gdk.Cursor.new_from_name(display, "default"),
        }
        self.cursors["move"] = (Gdk.Cursor.new_from_name(display, "move")
                                or Gdk.Cursor.new_from_name(display, "fleur")
                                or self.cursors["default"])
        self.set_cursor("draw")

    # ------------------------------------------------------------ utilidades
    def set_cursor(self, kind):
        if kind != self.cursor_kind and self.area.get_window() is not None:
            self.area.get_window().set_cursor(self.cursors[kind])
            self.cursor_kind = kind

    def snapshot(self):
        self.undo_stack.append(list(self.shapes))
        del self.undo_stack[:-MAX_UNDO]
        self.redo_stack.clear()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.shapes)
            self.shapes = self.undo_stack.pop()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.shapes)
            self.shapes = self.redo_stack.pop()

    def clear(self):
        if self.shapes:
            self.snapshot()
            self.shapes = []

    def selected_index(self):
        for i, s in enumerate(self.shapes):
            if s is self.selected:
                return i
        return None

    def shape_at(self, p):
        """La figura más reciente bajo el punto p (o None)."""
        for s in reversed(self.shapes):
            if hits(s, p, ERASER_RADIUS) or inside(s, p):
                return s
        return None

    def shape_near(self, p):
        """La figura más reciente cuyo borde está a GRAB_RADIUS px de p (o None)."""
        for s in reversed(self.shapes):
            if hits(s, p, GRAB_RADIUS):
                return s
        return None

    def start_drag(self, shape, p):
        self.current = None
        self.selected = shape
        self.drag = {"start": p, "orig": shape, "moved": False}
        self.set_cursor("move")

    def cancel_hold(self):
        if self.hold:
            GLib.source_remove(self.hold["timer"])
            self.hold = None

    def on_hold(self):
        """Se cumplió el tiempo de pulsación: la figura pasa a moverse con el ratón."""
        h, self.hold = self.hold, None
        if h and any(s is h["shape"] for s in self.shapes):
            self.start_drag(h["shape"], h["start"])  # descarta el trazo empezado
            self.area.queue_draw()
        return False

    def move_selected(self, dx, dy):
        i = self.selected_index()
        if i is None:
            return
        self.snapshot()
        self.shapes = list(self.shapes)
        self.selected = self.shapes[i] = translated(self.shapes[i], dx, dy)

    def set_width(self, delta):
        self.width = max(1, min(40, self.width + delta))

    def erase_at(self, p):
        keep = [s for s in self.shapes if not hits(s, p, ERASER_RADIUS)]
        if len(keep) != len(self.shapes):
            if not self.erased_in_drag:
                self.snapshot()
                self.erased_in_drag = True
            self.shapes = keep

    # ------------------------------------------------------------ barra
    def layout_bar(self, screen_w):
        tool_s, color_s, gap, sep, pad = 40, 30, 4, 14, 8
        entries = [("tool", t, tool_s) for t in TOOLS]
        entries += [("sep", None, sep)]
        entries += [("color", c, color_s) for c in COLORS]
        entries += [("size", None, color_s), ("style", None, tool_s), ("sep", None, sep),
                    ("undo", None, tool_s), ("clear", None, tool_s)]
        total = sum(e[2] for e in entries) + gap * (len(entries) - 1) + 2 * pad
        bar_h = tool_s + 2 * pad
        bx, by = (screen_w - total) / 2, 14
        self.bar_rect = (bx, by, total, bar_h)
        self.bar_items = []
        x = bx + pad
        for kind, value, s in entries:
            self.bar_items.append((kind, value, x, by + (bar_h - s) / 2, s))
            x += s + gap

    def in_bar(self, p):
        if not self.show_bar:
            return False
        x, y, w, h = self.bar_rect
        return x <= p[0] <= x + w and y <= p[1] <= y + h

    def bar_item_at(self, p):
        for item in self.bar_items:
            kind, value, x, y, s = item
            if kind != "sep" and x <= p[0] <= x + s and y <= p[1] <= y + s:
                return item
        return None

    def click_bar(self, p):
        item = self.bar_item_at(p)
        if item is None:
            return
        kind, value = item[0], item[1]
        if kind == "tool":
            self.tool = value
        elif kind == "color":
            self.color = value
        elif kind == "size":
            bigger = [w for w in WIDTHS if w > self.width]
            self.width = bigger[0] if bigger else WIDTHS[0]
        elif kind == "style":
            self.next_style()
        elif kind == "undo":
            self.undo()
        elif kind == "clear":
            self.clear()

    def next_style(self):
        i = STYLE_ORDER.index(self.style)
        self.style = STYLE_ORDER[(i + 1) % len(STYLE_ORDER)]

    def draw_bar(self, cr):
        x, y, w, h = self.bar_rect
        rounded_rect(cr, x, y, w, h, 12)
        cr.set_source_rgba(0.11, 0.11, 0.14, 0.92)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.12)
        cr.set_line_width(1)
        cr.stroke()

        hover = self.bar_item_at(self.mouse) if self.in_bar(self.mouse) else None
        for item in self.bar_items:
            kind, value, ix, iy, s = item
            if kind == "sep":
                cr.set_source_rgba(1, 1, 1, 0.15)
                cr.set_line_width(1)
                cr.move_to(ix + s / 2, y + 14)
                cr.line_to(ix + s / 2, y + h - 14)
                cr.stroke()
                continue
            active = kind == "tool" and value == self.tool
            if active or item is hover:
                rounded_rect(cr, ix, iy, s, s, 8)
                if active:
                    cr.set_source_rgba(*ACCENT, 0.45)
                else:
                    cr.set_source_rgba(1, 1, 1, 0.10)
                cr.fill()
            if kind == "color":
                cr.arc(ix + s / 2, iy + s / 2, s * 0.30, 0, 2 * math.pi)
                cr.set_source_rgb(*value)
                cr.fill()
                if value == self.color:
                    cr.arc(ix + s / 2, iy + s / 2, s * 0.43, 0, 2 * math.pi)
                    cr.set_source_rgb(0.95, 0.95, 0.98)
                    cr.set_line_width(2)
                    cr.stroke()
            elif kind == "size":
                cr.arc(ix + s / 2, iy + s / 2, s * 0.43, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.25)
                cr.set_line_width(1)
                cr.stroke()
                r = min(self.width / 2, s * 0.40)
                cr.arc(ix + s / 2, iy + s / 2, max(r, 1), 0, 2 * math.pi)
                cr.set_source_rgb(*self.color)
                cr.fill()
            elif kind == "style":
                self.draw_style_icon(cr, ix, iy, s)
            else:
                draw_icon(cr, value if kind == "tool" else kind, ix, iy, s)

        hint = "Clic der: borrar  ·  Arrastrar borde: mover  ·  Shift: recto  ·  T: estilo  ·  Rueda: grosor  ·  Ctrl+Z  ·  H: barra  ·  Esc: salir"
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(11.5)
        ext = cr.text_extents(hint)
        tx, ty = x + (w - ext.width) / 2 - ext.x_bearing, y + h + 18
        rounded_rect(cr, tx - 10, ty - 14, ext.width + 20, 20, 8)
        cr.set_source_rgba(0.11, 0.11, 0.14, 0.75)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.move_to(tx, ty)
        cr.show_text(hint)

    def draw_style_icon(self, cr, x, y, s):
        """Una línea dibujada con el estilo activo; los puntos indican el nivel."""
        cr.set_source_rgb(0.92, 0.92, 0.95)
        cr.set_line_width(1.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        a, b = (x + s * 0.22, y + s * 0.62), (x + s * 0.78, y + s * 0.30)
        if self.style:
            ops = []
            g = Rough(11, self.style * 2.5)  # exagerado: el icono es pequeño
            g.double_line(ops, a, b)
            draw_ops(cr, ops)
        else:
            cr.move_to(*a)
            cr.line_to(*b)
            cr.stroke()
        for k in range(self.style):
            cr.arc(x + s / 2 + (k - (self.style - 1) / 2) * 6, y + s * 0.80, 1.6, 0, 2 * math.pi)
            cr.fill()

    # ------------------------------------------------------------ eventos
    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        # borde fino para saber que la pizarra está activa
        cr.set_source_rgba(*ACCENT, 0.6)
        cr.set_line_width(3)
        cr.rectangle(1.5, 1.5, w - 3, h - 3)
        cr.stroke()

        for s in self.shapes:
            draw_shape(cr, s)
        if self.current:
            draw_shape(cr, self.current)

        if self.selected_index() is not None:
            x0, y0, x1, y1 = bbox(self.selected)
            cr.rectangle(x0 - 6, y0 - 6, x1 - x0 + 12, y1 - y0 + 12)
            cr.set_source_rgba(*ACCENT, 0.9)
            cr.set_line_width(1.5)
            cr.set_dash([6, 4])
            cr.stroke()
            cr.set_dash([])

        if self.erasing:
            cr.arc(*self.mouse, ERASER_RADIUS, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.set_line_width(1.5)
            cr.stroke()

        if self.show_bar:
            self.layout_bar(w)
            self.draw_bar(cr)
        return True

    def on_press(self, widget, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return True  # ignora doble/triple clic
        p = (ev.x, ev.y)
        if ev.button == 1:
            self.selected = None
            if self.in_bar(p):
                self.click_bar(p)
            elif (near := self.shape_near(p)) is not None:
                self.start_drag(near, p)  # en el borde: se agarra al instante
            else:
                shape = self.shape_at(p)
                if shape is not None:
                    self.hold = {"start": p, "shape": shape,
                                 "timer": GLib.timeout_add(HOLD_MS, self.on_hold)}
                pts = [p] if self.tool == "pen" else [p, p]
                self.current = {"tool": self.tool, "color": self.color,
                                "width": self.width, "pts": pts,
                                "rough": self.style, "seed": random.randrange(2 ** 31)}
        elif ev.button == 3:
            self.cancel_hold()
            self.erasing = True
            self.erased_in_drag = False
            self.erase_at(p)
        widget.queue_draw()
        return True

    def on_motion(self, widget, ev):
        p = (ev.x, ev.y)
        self.mouse = p
        if self.hold and math.hypot(p[0] - self.hold["start"][0],
                                    p[1] - self.hold["start"][1]) > HOLD_SLOP:
            self.cancel_hold()  # se movió antes de tiempo: es un trazo normal
        if self.current:
            cur = self.current
            if cur["tool"] == "pen":
                last = cur["pts"][-1]
                if math.hypot(p[0] - last[0], p[1] - last[1]) >= 1.5:
                    cur["pts"].append(p)
            else:
                if ev.state & Gdk.ModifierType.SHIFT_MASK:
                    p = constrain(cur["tool"], cur["pts"][0], p)
                cur["pts"][1] = p
        elif self.drag:
            d = self.drag
            dx, dy = p[0] - d["start"][0], p[1] - d["start"][1]
            if ev.state & Gdk.ModifierType.SHIFT_MASK:
                if abs(dx) >= abs(dy):
                    dy = 0
                else:
                    dx = 0
            i = self.selected_index()
            if i is not None and (dx or dy or d["moved"]):
                if not d["moved"]:
                    self.snapshot()
                    d["moved"] = True
                self.shapes = list(self.shapes)
                self.selected = self.shapes[i] = translated(d["orig"], dx, dy)
        elif self.erasing:
            self.erase_at(p)
        elif self.in_bar(p):
            self.set_cursor("default")
        else:
            self.set_cursor("move" if self.shape_near(p) is not None else "draw")
        widget.queue_draw()
        return True

    def on_release(self, widget, ev):
        if ev.button == 1:
            self.cancel_hold()
        if ev.button == 1 and self.drag:
            self.drag = None
        elif ev.button == 1 and self.current:
            cur = self.current
            self.current = None
            p0, p1 = cur["pts"][0], cur["pts"][-1]
            if cur["tool"] == "pen" or math.hypot(p1[0] - p0[0], p1[1] - p0[1]) >= 3:
                self.snapshot()
                self.shapes.append(cur)
        elif ev.button == 3:
            self.erasing = False
        widget.queue_draw()
        return True

    def on_scroll(self, widget, ev):
        if ev.direction == Gdk.ScrollDirection.UP:
            self.set_width(1)
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            self.set_width(-1)
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:
            ok, _dx, dy = ev.get_scroll_deltas()
            if ok and dy:
                self.set_width(-1 if dy > 0 else 1)
        widget.queue_draw()
        return True

    def on_key(self, widget, ev):
        name = (Gdk.keyval_name(ev.keyval) or "").lower()
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        shift = ev.state & Gdk.ModifierType.SHIFT_MASK
        if name == "escape":
            self.quit()
            return True
        if ctrl:
            if name == "z":
                self.redo() if shift else self.undo()
            elif name == "y":
                self.redo()
        elif name in TOOL_KEYS:
            self.tool = TOOL_KEYS[name]
        elif name in ARROW_KEYS and not self.drag:
            step = 10 if shift else 1
            ux, uy = ARROW_KEYS[name]
            self.move_selected(ux * step, uy * step)
        elif name in ("plus", "equal", "kp_add"):
            self.set_width(1)
        elif name in ("minus", "kp_subtract"):
            self.set_width(-1)
        elif name == "t":
            self.next_style()
        elif name == "h":
            self.show_bar = not self.show_bar
        elif name == "delete":
            self.clear()
        self.area.queue_draw()
        return True

    def quit(self, *_):
        Gtk.main_quit()
        return False


# ---------------------------------------------------------------- arranque

def running_pid():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"pizarra" not in f.read():
                return None
        return pid
    except (OSError, ValueError):
        return None


def main():
    pid = running_pid()
    if pid is not None:
        if "--toggle" in sys.argv[1:]:
            os.kill(pid, signal.SIGTERM)
        return

    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    try:
        win = Pizarra()
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            add_signal = GLibUnix.signal_add
        except (ImportError, ValueError):
            add_signal = GLib.unix_signal_add
        for sig in (signal.SIGTERM, signal.SIGINT):
            add_signal(GLib.PRIORITY_DEFAULT, sig, win.quit)
        Gtk.main()
    finally:
        if running_pid() == os.getpid():
            os.remove(PIDFILE)


if __name__ == "__main__":
    main()
