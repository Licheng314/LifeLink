"""Antialiased timer graphics and per-pixel-alpha Windows popup, no image dependency."""
from __future__ import annotations

import ctypes as c
import math
import queue
import struct
import tkinter as tk
import zlib
from ctypes import wintypes as w
from dataclasses import dataclass

WIDTH = 172
EXPAND_CENTER = (154, 29)
BUTTON_RADIUS = 16
BODY_CENTER = (64, 65)
BODY_RADIUS = 61
RING_RADIUS = 55
BUTTON_SPACING = 36
CARD = "#1e293b"
TRACK = "#334155"
WHITE = "#f8fafc"
AMBER = "#f59e0b"
CORAL = "#fb7185"
MINT = "#22c55e"
BUTTON_CORAL = "#e45f75"
CORNER_RADIUS = 4
GOAL_RECT = (3, 137, 125, 159)
NEXT_RECT = (3, 165, 125, 187)


@dataclass(frozen=True)
class TimerVisual:
    mode: str
    label: str
    clock: str
    progress: float
    goal: str = ""
    paused: bool = False
    complete: bool = False
    ready: bool = False
    color: str = ""
    intervals: tuple = ()
    now_fraction: float = 0
    visible: bool = True


def ring_sweep(mode: str, progress: float) -> tuple[float, float]:
    """GDI+ starts at 3 o'clock, positive angles rotate clockwise."""
    fraction = max(0.0, min(1.0, progress))
    return -90.0, (-1 if mode == "sedentary" else 1) * min(359.99, 360 * fraction)


def action_rects(mode: str, complete: bool = False) -> dict[str, tuple[int, int, int, int]]:
    names = ["expand", "leave"] if mode == "sedentary" else ["expand", "pause", "stop"]
    if mode == "interval":
        names = ["expand"]
    if complete:
        names = ["expand", "stop"]
    result = {}
    for index, name in enumerate(names):
        x, y = EXPAND_CENTER[0], EXPAND_CENTER[1] + index * BUTTON_SPACING
        result[name] = (x - BUTTON_RADIUS, y - BUTTON_RADIUS, x + BUTTON_RADIUS, y + BUTTON_RADIUS)
    if complete:
        result["next"] = NEXT_RECT
    return result


def _bind(dll, name, result, args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


class Raster:
    """GDI+ draws premultiplied BGRA with antialiasing directly at display size."""
    _gdip = None
    _token = None

    def __init__(self, width, height):
        if self.__class__._gdip is None:
            g = c.WinDLL("gdiplus")
            class Startup(c.Structure):
                _fields_ = [("version", w.UINT), ("callback", c.c_void_p), ("background", w.BOOL), ("codecs", w.BOOL)]
            token = c.c_size_t()
            _bind(g, "GdiplusStartup", c.c_int, [c.POINTER(c.c_size_t), c.POINTER(Startup), c.c_void_p])(
                c.byref(token), c.byref(Startup(1, None, False, False)), None)
            self.__class__._gdip, self.__class__._token = g, token
        self.g = self._gdip
        self.width, self.height = width, height
        self.bitmap, self.graphics = c.c_void_p(), c.c_void_p()
        self.resources = []
        self.call("GdipCreateBitmapFromScan0", [c.c_int, c.c_int, c.c_int, c.c_int, c.c_void_p, c.POINTER(c.c_void_p)],
                  width, height, 0, 0xE200B, None, c.byref(self.bitmap))
        self.call("GdipGetImageGraphicsContext", [c.c_void_p, c.POINTER(c.c_void_p)], self.bitmap, c.byref(self.graphics))
        for name, value in (("GdipSetSmoothingMode", 4), ("GdipSetPixelOffsetMode", 4), ("GdipSetTextRenderingHint", 4)):
            self.call(name, [c.c_void_p, c.c_int], self.graphics, value)
        self.call("GdipGraphicsClear", [c.c_void_p, w.DWORD], self.graphics, 0)

    def call(self, name, args, *values):
        status = _bind(self.g, name, c.c_int, args)(*values)
        if status:
            raise OSError(f"{name} failed: {status}")

    @staticmethod
    def color(value):
        return 0xFF000000 | int(value.lstrip("#"), 16)

    def brush(self, color):
        result = c.c_void_p()
        self.call("GdipCreateSolidFill", [w.DWORD, c.POINTER(c.c_void_p)], self.color(color), c.byref(result))
        self.resources.append(("GdipDeleteBrush", result))
        return result

    def pen(self, color, width):
        result = c.c_void_p()
        self.call("GdipCreatePen1", [w.DWORD, c.c_float, c.c_int, c.POINTER(c.c_void_p)], self.color(color), width, 2, c.byref(result))
        self.resources.append(("GdipDeletePen", result))
        return result

    def ellipse(self, box, color):
        x, y, x2, y2 = box
        self.call("GdipFillEllipse", [c.c_void_p, c.c_void_p, *([c.c_float] * 4)],
                  self.graphics, self.brush(color), x, y, x2 - x, y2 - y)

    def rect(self, box, color, radius=0):
        x, y, x2, y2 = box
        brush = self.brush(color)
        fill = lambda a, b, d, e: self.call("GdipFillRectangle", [c.c_void_p, c.c_void_p, *([c.c_float] * 4)], self.graphics, brush, a, b, d, e)
        if not radius:
            fill(x, y, x2 - x, y2 - y)
        else:
            fill(x + radius, y, x2 - x - radius * 2, y2 - y)
            fill(x, y + radius, x2 - x, y2 - y - radius * 2)
            for a, b in ((x, y), (x2 - radius * 2, y), (x, y2 - radius * 2), (x2 - radius * 2, y2 - radius * 2)):
                self.ellipse((a, b, a + radius * 2, b + radius * 2), color)

    def line(self, x1, y1, x2, y2, color=WHITE, width=1):
        self.call("GdipDrawLine", [c.c_void_p, c.c_void_p, *([c.c_float] * 4)],
                  self.graphics, self.pen(color, width), x1, y1, x2, y2)

    def ring(self, progress, mode, color):
        x, y = BODY_CENTER
        r = RING_RADIUS
        args = [c.c_void_p, c.c_void_p, *([c.c_float] * 6)]
        self.call("GdipDrawArc", args, self.graphics, self.pen(TRACK, 4), x - r, y - r, r * 2, r * 2, 0, 360)
        start, sweep = ring_sweep(mode, progress)
        if sweep:
            self.call("GdipDrawArc", args, self.graphics, self.pen(color, 4), x - r, y - r, r * 2, r * 2, start, sweep)

    def text(self, text, box, size, color=WHITE, bold=False, centered=True, family_name="Microsoft YaHei UI"):
        class RectF(c.Structure):
            _fields_ = [(key, c.c_float) for key in ("x", "y", "width", "height")]
        family, font, fmt = c.c_void_p(), c.c_void_p(), c.c_void_p()
        self.call("GdipCreateFontFamilyFromName", [w.LPCWSTR, c.c_void_p, c.POINTER(c.c_void_p)], family_name, None, c.byref(family))
        self.resources.append(("GdipDeleteFontFamily", family))
        self.call("GdipCreateFont", [c.c_void_p, c.c_float, c.c_int, c.c_int, c.POINTER(c.c_void_p)], family, size, int(bold), 2, c.byref(font))
        self.resources.append(("GdipDeleteFont", font))
        self.call("GdipCreateStringFormat", [c.c_int, c.c_ushort, c.POINTER(c.c_void_p)], 0x1000, 0, c.byref(fmt))
        self.resources.append(("GdipDeleteStringFormat", fmt))
        self.call("GdipSetStringFormatAlign", [c.c_void_p, c.c_int], fmt, 1 if centered else 0)
        self.call("GdipSetStringFormatLineAlign", [c.c_void_p, c.c_int], fmt, 1)
        self.call("GdipSetStringFormatTrimming", [c.c_void_p, c.c_int], fmt, 3)
        x, y, x2, y2 = box
        rect = RectF(x, y, x2 - x, y2 - y)
        # Explicit UTF-16 length is necessary for non-BMP goal text.
        length = len(text.encode("utf-16-le")) // 2
        self.call("GdipDrawString", [c.c_void_p, w.LPCWSTR, c.c_int, c.c_void_p, c.POINTER(RectF), c.c_void_p, c.c_void_p],
                  self.graphics, text, length, font, c.byref(rect), fmt, self.brush(color))

    def pixels(self):
        class Rect(c.Structure):
            _fields_ = [(key, c.c_int) for key in ("x", "y", "width", "height")]
        class Bits(c.Structure):
            _fields_ = [("width", w.UINT), ("height", w.UINT), ("stride", c.c_int), ("format", c.c_int), ("scan", c.c_void_p), ("reserved", c.c_size_t)]
        data = Bits()
        self.call("GdipBitmapLockBits", [c.c_void_p, c.POINTER(Rect), w.UINT, c.c_int, c.POINTER(Bits)],
                  self.bitmap, c.byref(Rect(0, 0, self.width, self.height)), 1, 0xE200B, c.byref(data))
        try:
            return b"".join(c.string_at(data.scan + y * data.stride, self.width * 4) for y in range(self.height))
        finally:
            self.call("GdipBitmapUnlockBits", [c.c_void_p, c.POINTER(Bits)], self.bitmap, c.byref(data))

    def close(self):
        for name, resource in reversed(self.resources):
            _bind(self.g, name, c.c_int, [c.c_void_p])(resource)
        self.call("GdipDeleteGraphics", [c.c_void_p], self.graphics)
        self.call("GdipDisposeImage", [c.c_void_p], self.bitmap)


def png(width, height, pixels):
    """Convert premultiplied BGRA to a standard RGBA PNG for Tk/tests."""
    rgba = bytearray(len(pixels))
    rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = pixels[2::4], pixels[1::4], pixels[0::4], pixels[3::4]
    # Most card pixels are opaque. Only edge/text alpha needs unpremultiplying.
    for position, alpha in enumerate(pixels[3::4]):
        if 0 < alpha < 255:
            i = position * 4
            for channel in range(3):
                rgba[i + channel] = min(255, rgba[i + channel] * 255 // alpha)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def render_compact(state: TimerVisual):
    height = 47 if state.mode == "interval" and not state.visible else 130 if state.mode in {"sedentary", "interval"} else 192 if state.complete else 164
    draw = Raster(WIDTH, height)
    color = state.color or (AMBER if state.mode == "sedentary" else MINT if state.label.startswith("休息") else CORAL)
    try:
        x, y = BODY_CENTER
        if state.visible:
            draw.ellipse((x - BODY_RADIUS, y - BODY_RADIUS, x + BODY_RADIUS, y + BODY_RADIUS), CARD)
            draw.ring(state.progress, state.mode, color)
            draw.text(state.label, (10, 36, 118, 60), 11, color, bold=True)
            draw.text(state.clock, (7, 59, 121, 95), 22 if len(state.clock) > 5 else 26, bold=True, family_name="Segoe UI")
            if state.mode == "interval":
                y_axis = 104 if state.clock else 80
                left, width = 28, 72
                draw.line(left, y_axis, left + width, y_axis, TRACK)
                for start, end, segment_color in state.intervals:
                    draw.rect((left + width * start, y_axis - 1, left + width * end, y_axis + 1), segment_color)
                now_x = left + width * state.now_fraction
                draw.line(now_x, y_axis - 3, now_x, y_axis + 3, "#ef4444", 2)
        actions = action_rects(state.mode, state.complete)
        for name, box in actions.items():
            a, b, d, e = box
            if name == "next":
                draw.rect(box, BUTTON_CORAL, CORNER_RADIUS)
                draw.text("开始下一轮", box, 10)
                continue
            draw.ellipse(box, CARD)
            cx, cy = (a + d) / 2, (b + e) / 2
            draw_icon(draw, "play" if name == "pause" and (state.paused or state.ready) else name, cx, cy, size=14)
        if state.mode == "pomodoro":
            # Align the goal and completion action with the orb, not its side controls.
            draw.rect(GOAL_RECT, CARD, CORNER_RADIUS)
            draw.text(state.goal, (8, 137, 120, 159), 10, centered=False)
        return WIDTH, height, draw.pixels(), actions
    finally:
        draw.close()


def draw_icon(draw, name, cx, cy, size=8, color=WHITE):
    """A single geometric icon vocabulary, independent of font glyph metrics."""
    scale = size / 8
    def line(x1, y1, x2, y2, width=1):
        draw.line(cx + x1 * scale, cy + y1 * scale, cx + x2 * scale, cy + y2 * scale, color, width * scale)
    def rect(x1, y1, x2, y2):
        draw.rect((cx + x1 * scale, cy + y1 * scale, cx + x2 * scale, cy + y2 * scale), color)
    if name == "expand":
        for row in range(4):
            line(-4 + row, -1.5 + row, 4 - row, -1.5 + row)
    elif name == "stop":
        rect(-4, -4, 4, 4)
    elif name == "pause":
        for offset in (-4, 2):
            rect(offset, -4, offset + 2, 4)
    elif name == "play":
        for col in range(5):
            line(-2 + col, -4 + col, -2 + col, 4 - col)
    elif name == "leave":
        line(-3, -4, -3, 4)
        line(-3, -4, 1, -4)
        line(-3, 4, 1, 4)
        line(-1, 0, 4, 0)
        line(2, -2, 4, 0)
        line(2, 2, 4, 0)
    elif name == "close":
        line(-4, -4, 4, 4, 1.2)
        line(-4, 4, 4, -4, 1.2)
    elif name == "minimize":
        line(-4, 0, 4, 0, 1.2)


class TimerButton(tk.Canvas):
    def __init__(self, parent, text, command, width=84, primary=False, height=24, icon=None, background=CARD, header=False):
        super().__init__(parent, width=width, height=height, highlightthickness=0,
                         background=background, cursor="hand2", takefocus=True)
        self.text, self.command, self.primary, self.hovered = text, command, primary, False
        self.button_width = width
        self.button_height, self.icon, self.header = height, icon, header
        self.surface_background = background
        self.bind("<ButtonRelease-1>", lambda _e: self.invoke())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.set(text)

    def invoke(self):
        return self.command()

    def _hover(self, hovered):
        self.hovered = hovered
        self.set(self.text)

    def set(self, text):
        if hasattr(self, "image") and text == self.text and getattr(self, "last_hover", None) == self.hovered:
            return
        self.text, self.last_hover = text, self.hovered
        draw = Raster(self.button_width, self.button_height)
        try:
            draw.rect((0, 0, self.button_width, self.button_height),
                      "#475569" if self.hovered else self.surface_background if self.header else BUTTON_CORAL if self.primary else TRACK,
                      CORNER_RADIUS)
            icon = {"Ⅱ": "pause", "▶": "play", "■": "stop"}.get(text, self.icon)
            if icon:
                draw_icon(draw, icon, self.button_width / 2, self.button_height / 2, size=8)
            else:
                draw.text(text, (0, 0, self.button_width, self.button_height), 11)
            self.image = tk.PhotoImage(master=self, data=png(self.button_width, self.button_height, draw.pixels()))
        finally:
            draw.close()
        self.delete("all")
        self.create_image(0, 0, image=self.image, anchor="nw")


class TimerCard(tk.Frame):
    """Small-radius card border; padding keeps child widgets clear of the corners."""
    def __init__(self, parent, background, border, border_width=1, **kwargs):
        super().__init__(parent, background=parent.cget("background"), **kwargs)
        self.card_color, self.border_color = background, border
        self.border_width = border_width
        self.border_canvas = tk.Canvas(self, highlightthickness=0, background=self.cget("background"))
        self.border_canvas.place(x=0, y=0, relwidth=1, relheight=1, bordermode="outside")
        self.tk.call("lower", self.border_canvas._w)
        self.bind("<Configure>", self._paint)

    def configure(self, cnf=None, **kwargs):
        repaint = False
        if "highlightbackground" in kwargs:
            self.border_color = kwargs.pop("highlightbackground")
            repaint = True
        if "background" in kwargs:
            self.card_color = kwargs.pop("background")
            repaint = True
        if repaint:
            self._paint()
        return super().configure(cnf, **kwargs)

    def _paint(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 10 or height < 10:
            return
        key = width, height, self.border_color, self.card_color
        if key == getattr(self, "paint_key", None):
            return
        self.paint_key = key
        draw = Raster(width, height)
        try:
            draw.rect((0, 0, width, height), self.border_color, 5)
            inset = self.border_width
            draw.rect((inset, inset, width - inset, height - inset), self.card_color, max(1, 5 - inset))
            self.border_image = tk.PhotoImage(master=self, data=png(width, height, draw.pixels()))
        finally:
            draw.close()
        self.border_canvas.delete("all")
        self.border_canvas.create_image(0, 0, anchor="nw", image=self.border_image)


class LayeredTimerWindow:
    """Keep true edge alpha; Tk color-key transparency cannot represent antialiasing."""
    def __init__(self, root, dispatch, hover):
        self.root, self.dispatch, self.hover = root, dispatch, hover
        self.u, self.g = c.WinDLL("user32", use_last_error=True), c.WinDLL("gdi32", use_last_error=True)
        self.commands = queue.SimpleQueue()
        self.actions = {}
        self.visible = False
        self.frame = None
        self.job = None
        self.drag = None
        self.pressed = None
        self.last_hover = None
        callback = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        class WindowClass(c.Structure):
            _fields_ = [("style", w.UINT), ("proc", callback), ("class_extra", c.c_int), ("window_extra", c.c_int),
                        ("instance", w.HINSTANCE), ("icon", w.HICON), ("cursor", w.HANDLE), ("brush", w.HBRUSH),
                        ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]
        self.callback = callback(self._proc)
        _bind(self.u, "DefWindowProcW", c.c_ssize_t, [w.HWND, w.UINT, w.WPARAM, w.LPARAM])
        self.name = f"LifeLinkTimer-{id(self)}"
        self.instance = _bind(c.WinDLL("kernel32"), "GetModuleHandleW", w.HINSTANCE, [w.LPCWSTR])(None)
        cursor = _bind(self.u, "LoadCursorW", w.HANDLE, [w.HINSTANCE, w.LPCWSTR])(None, c.cast(c.c_void_p(32512), w.LPCWSTR))
        cls = WindowClass(0, self.callback, 0, 0, self.instance, None, cursor, None, None, self.name)
        if not _bind(self.u, "RegisterClassW", w.ATOM, [c.POINTER(WindowClass)])(c.byref(cls)):
            raise c.WinError(c.get_last_error())
        self.hwnd = _bind(self.u, "CreateWindowExW", w.HWND,
                          [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c.c_int, c.c_int, c.c_int, c.c_int,
                           w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p])(
            0x80000 | 0x80 | 0x08000000, self.name, "Life Link Timer", 0x80000000,
            0, 0, WIDTH, 164, None, None, self.instance, None)
        if not self.hwnd:
            raise c.WinError(c.get_last_error())
        for name, result, args in (
            ("DefWindowProcW", c.c_ssize_t, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
            ("SetWindowPos", w.BOOL, [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT]),
            ("ShowWindow", w.BOOL, [w.HWND, c.c_int]), ("GetWindowRect", w.BOOL, [w.HWND, c.POINTER(w.RECT)]),
            ("GetCursorPos", w.BOOL, [c.POINTER(w.POINT)]), ("SetCapture", w.HWND, [w.HWND]),
            ("DestroyWindow", w.BOOL, [w.HWND]), ("UnregisterClassW", w.BOOL, [w.LPCWSTR, w.HINSTANCE]),
            ("GetDC", w.HDC, [w.HWND]), ("ReleaseDC", c.c_int, [w.HWND, w.HDC]),
            ("PeekMessageW", w.BOOL, [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]),
            ("DispatchMessageW", c.c_ssize_t, [c.POINTER(w.MSG)]),
            ("TranslateMessage", w.BOOL, [c.POINTER(w.MSG)]),
        ):
            _bind(self.u, name, result, args)

    def position(self):
        rect = w.RECT()
        self.u.GetWindowRect(self.hwnd, c.byref(rect))
        return rect.left, rect.top

    def anchor(self):
        x, y = self.position()
        return x + EXPAND_CENTER[0], y + EXPAND_CENTER[1]

    def hit(self, x, y):
        return next((name for name, (a, b, d, e) in self.actions.items() if a <= x <= d and b <= y <= e), None)

    def _proc(self, hwnd, message, wp, lp):
        if message in {0x201, 0x202, 0x200}:
            x, y = c.c_short(lp & 65535).value, c.c_short(lp >> 16 & 65535).value
            if message == 0x201:
                self.pressed = self.hit(x, y)
                self.drag = None if self.pressed else (x, y)
                self.u.SetCapture(hwnd)
            elif message == 0x200 and wp & 1 and self.drag:
                point = w.POINT()
                self.u.GetCursorPos(c.byref(point))
                self.u.SetWindowPos(hwnd, None, point.x - self.drag[0], point.y - self.drag[1], 0, 0, 0x15)
            elif message == 0x202:
                self.u.ReleaseCapture()
                if self.pressed and self.hit(x, y) == self.pressed:
                    self.commands.put(self.pressed)
                self.pressed, self.drag = None, None
            return 0
        if message == 0x21:  # MA_NOACTIVATE: do not steal typing focus.
            return 3
        return self.u.DefWindowProcW(hwnd, message, wp, lp)

    def show(self, frame, anchor, topmost):
        self.visible = True
        self.actions = frame[3]
        x, y = anchor[0] - EXPAND_CENTER[0], anchor[1] - EXPAND_CENTER[1]
        self.u.SetWindowPos(self.hwnd, c.c_void_p(-1 if topmost else -2), x, y, frame[0], frame[1], 0x10)
        self.update(frame)
        self.u.ShowWindow(self.hwnd, 4)
        if self.job is None:
            self._pump()

    def update(self, frame):
        self.frame, self.actions = frame, frame[3]
        width, height, pixels, _ = frame
        class Header(c.Structure):
            _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG), ("planes", w.WORD),
                        ("bits", w.WORD), ("compression", w.DWORD), ("length", w.DWORD),
                        ("x", w.LONG), ("y", w.LONG), ("used", w.DWORD), ("important", w.DWORD)]
        class Blend(c.Structure):
            _fields_ = [(key, w.BYTE) for key in ("op", "flags", "alpha", "format")]
        class Size(c.Structure):
            _fields_ = [("cx", w.LONG), ("cy", w.LONG)]
        dc = self.u.GetDC(None)
        memory = _bind(self.g, "CreateCompatibleDC", w.HDC, [w.HDC])(dc)
        if not memory:
            self.u.ReleaseDC(None, dc)
            raise c.WinError(c.get_last_error())
        header = Header(40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        scan = c.c_void_p()
        bitmap = _bind(self.g, "CreateDIBSection", w.HBITMAP, [w.HDC, c.c_void_p, w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD])(
            dc, c.byref(header), 0, c.byref(scan), None, 0)
        if not bitmap or not scan.value:
            _bind(self.g, "DeleteDC", w.BOOL, [w.HDC])(memory)
            self.u.ReleaseDC(None, dc)
            raise c.WinError(c.get_last_error())
        select = _bind(self.g, "SelectObject", w.HGDIOBJ, [w.HDC, w.HGDIOBJ])
        previous = select(memory, bitmap)
        try:
            c.memmove(scan, pixels, len(pixels))
            x, y = self.position()
            target, source, size, blend = w.POINT(x, y), w.POINT(0, 0), Size(width, height), Blend(0, 0, 255, 1)
            ok = _bind(self.u, "UpdateLayeredWindow", w.BOOL,
                       [w.HWND, w.HDC, c.POINTER(w.POINT), c.POINTER(Size), w.HDC, c.POINTER(w.POINT), w.DWORD, c.POINTER(Blend), w.DWORD])(
                self.hwnd, dc, c.byref(target), c.byref(size), memory, c.byref(source), 0, c.byref(blend), 2)
            if not ok:
                raise c.WinError(c.get_last_error())
        finally:
            select(memory, previous)
            _bind(self.g, "DeleteObject", w.BOOL, [w.HGDIOBJ])(bitmap)
            _bind(self.g, "DeleteDC", w.BOOL, [w.HDC])(memory)
            self.u.ReleaseDC(None, dc)

    def _pump(self):
        self.job = None
        if not self.visible:
            return
        message = w.MSG()
        while self.u.PeekMessageW(c.byref(message), self.hwnd, 0, 0, 1):
            self.u.TranslateMessage(c.byref(message))
            self.u.DispatchMessageW(c.byref(message))
        while not self.commands.empty():
            self.dispatch(self.commands.get_nowait())
        if self.visible:
            point = w.POINT()
            self.u.GetCursorPos(c.byref(point))
            x, y = self.position()
            local = point.x - x, point.y - y
            goal = self.frame and self.frame[1] > 130 and GOAL_RECT[0] <= local[0] <= GOAL_RECT[2] and GOAL_RECT[1] <= local[1] <= GOAL_RECT[3]
            hit = self.hit(*local)
            hovered = "goal" if goal else hit
            if hovered != self.last_hover:
                self.last_hover = hovered
                self.hover(hovered, point.x, point.y)
            self.job = self.root.after(30, self._pump)

    def hide(self):
        self.visible = False
        self.u.ShowWindow(self.hwnd, 0)
        if self.job is not None:
            self.root.after_cancel(self.job)
            self.job = None

    def destroy(self):
        self.hide()
        self.u.DestroyWindow(self.hwnd)
        self.u.UnregisterClassW(self.name, self.instance)
