#!/usr/bin/env python3
"""
plaud-linux :: "Destaques da reunião" panel (GTK3)

The window the pencil opens. The official client calls it RecordingHighlight
and reaches it the same way -- `toggleWindowDisplay(WindowEnum.RecordingHighlight)`
at renderer/assets/index-DotEOTW4.js:288-300 is the pencil's entire handler.

Rebuilt from the official component rather than approximated. Geometry, icon
paths, colour tokens and layout all come from app.asar; every constant below
names the file it was read from. Confirmed against
docs/plaud-desktop/media/image24.png.

Layout, top to bottom (index-CoA7AblW.js Header/Content/Footer):

  header   logo | ... overflow menu | separator | shrink control
  content  editable title, then the note entry
  footer   pause, stop | waveform, timer | flag, crop

This window does NOT own a Recorder and does NOT touch the network. It is a
view over the Overlay that opened it: pause, stop and screenshot call the
Overlay's existing handlers rather than reimplementing them, so there is one
implementation of each and one _shot_pending guard, not two.

Deliberately a normal toplevel, NOT a gtk-layer-shell surface. The pill is a
layer surface because it must be placed, stacked and sticky without user
interaction. This window is the opposite: the official one is
`type: "panel"`, `resizable: true` at 420x360 (main/index-CvwdKAqY.js:44-75),
and a layer surface cannot be moved or resized by the user at all -- wlr-layer-shell
positions by anchor+margin only. Reproducing the official behaviour therefore
means a normal window here. set_keep_above() is NOT used to compensate: it is a
silent no-op on Wayland (CLAUDE.md Conventions), so the panel stacks normally.
"""
import json
import os
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf  # noqa: E402

import cairo  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from datetime import datetime  # noqa: E402

try:
    from . import paths
    from . import overlay as ov
except ImportError:
    import paths
    import overlay as ov


# --- official window geometry, main/index-CvwdKAqY.js:44-48 -----------------
WINDOW_WIDTH = 420
WINDOW_HEIGHT = 360
MIN_WIDTH = 312
MIN_HEIGHT = 240
THUMBNAIL_HEIGHT = 90  # PictureMark cap in the official recording panel.

# Pinned so the compositor has a stable identity to match, the same job
# GtkLayerShell.set_namespace("plaud-overlay") does for the pill. Without it
# the class is whatever argv[0] happened to be.
WMCLASS = "plaud-highlights"


# --- official icon geometry -------------------------------------------------
#
# Verbatim from renderer/assets/, same treatment as overlay.py's LOGO_PATH and
# PENCIL_PATH: copied, not redrawn by eye.
#
# SvgIconFlag -- index-CoA7AblW.js:855. viewBox 0 0 20 20, fill none,
# stroke currentColor, strokeLinejoin round, no strokeWidth attribute so the
# SVG default of 1 applies (same as SvgIconHighlightsNote in overlay.py).
FLAG_PATH = (
    "M5.14583 17.1668L4.3125 3.41682C8.0625 3.00123 10.9792 4.66558 15.1458 4.25015"
    "L15.5625 11.7502C11.3958 12.166 8.77819 10.5662 5.97917 10.9168"
)

# SvgIconScreenshot -- index-CoA7AblW.js:1655. TWO paths, viewBox 0 0 24 24,
# fill currentColor, no stroke at all. This is a solid-fill icon, unlike the
# flag -- so overlay.py's hand-drawn _draw_crop (two stroked L's, authored on a
# 20x20 grid before this path was recovered) is NOT the official shape. This
# module uses the real one.
SCREENSHOT_PATHS = (
    "M7.5002 16.5H21.3002V17.7H17.7002V21.3H16.5002V17.7H6.3002V7.5H2.7002V6.3H6.3002V2.7H7.5002V16.5Z",
    "M17.7002 15.3H16.5002V7.5H8.7002V6.3H17.7002V15.3Z",
)

# SvgIconPause / SvgIconPlay -- CancelRecordingMenu-DhllEhXY.js:8-9.
# viewBox 0 0 20 20, fill currentColor, no stroke. Again the real paths rather
# than overlay.py's hand-drawn bars and triangle.
PAUSE_PATH = (
    "M7.32227 15.5796H6.32227L6.32227 4.42041H7.32227L7.32227 15.5796Z"
    "M13.6777 15.5796L12.6777 15.5796L12.6777 4.42041H13.6777L13.6777 15.5796Z"
)
PLAY_PATH = (
    "M15.31 9.5875L5.75 4.0675C5.6 3.9775 5.4 3.9775 5.25 4.0675C5.1 4.1575 5 4.3175 5 4.4975"
    "V15.5375C5 15.7175 5.1 15.8775 5.25 15.9675C5.4 16.0575 5.6 16.0575 5.75 15.9675L6 15.8275"
    "V5.3575L14.06 10.0075L6.85 14.1775V15.3275L15.31 10.4475C15.46 10.3575 15.56 10.1875"
    " 15.56 10.0175C15.56 9.8375 15.46 9.6775 15.31 9.5875Z"
)

# SvgIconShrink -- index-CoA7AblW.js:1896. viewBox 0 0 20 20, stroke
# currentColor, two subpaths, no strokeWidth so the default 1 applies.
SHRINK_PATHS = (
    "M8.99963 14.5001V11H5.49963M8.75018 11.2501L5 15.0005",
    "M14.5 8.99977H11V5.49964M11.2501 8.75032L15.0004 5",
)

# Colour tokens, renderer/assets/index-CYM9K6Ws.css :root (light).
# The panel is light regardless of the desktop theme, for the same reason the
# pill is -- see overlay.py's CSS comment.
LABELS_PRIMARY = (0.0, 0.0, 0.0)            # --labels-primary  #000
LABELS_SECONDARY = (0x3d / 255.0,) * 3      # --labels-secondary #3d3d3d
LABELS_TERTIARY = (0x7a / 255.0,) * 3       # --labels-tertiary #7a7a7a
LABELS_DISABLED = (0xa3 / 255.0,) * 3       # --labels-disabled #a3a3a3

CSS = b"""
/* --background-secondary #f9f9f9, --separator-default #ebebeb, and the
   HighlightIconButton's own "border border-separator-default bg-white". */
/* On the window AND on the root box. The class is applied to both because
   styling only the box leaves the GtkWindow itself painting the desktop
   theme's own window background behind it -- measured, and it read as
   transparent over a light desktop, letting another window show through the
   panel's empty middle. */
.plaud-panel, window.plaud-panel { background-color: #f9f9f9; }
.plaud-panel-header { border-bottom: 1px solid #ebebeb; }
/* HighlightIconButton, index-CoA7AblW.js:1664:
   "border border-separator-default bg-white px-[clamp(8px,...,16px)] py-2".
   The clamp resolves to 16px at the panel's 420px default width. */
.plaud-footerbtn {
  background: #fff;
  border: 1px solid #ebebeb;
  border-radius: 5px;
  padding: 8px 16px;
  min-width: 0;
  min-height: 0;
}
.plaud-footerbtn:hover  { background: #f2f2f2; }
.plaud-footerbtn:active { background: #ebebeb; }
/* isPauseRecording => "pointer-events-none bg-secondary text-labels-disabled".
   --secondary is oklch(97% 0 0) ~= #f7f7f7. GTK's :disabled is the
   pointer-events-none half; this rule is the bg-secondary half. */
.plaud-footerbtn:disabled { background: #f7f7f7; border-color: #ebebeb; }
/* SingleIconButton, as in the pill: transparent until hovered. */
.plaud-headerbtn {
  background: transparent;
  color: #3d3d3d;
  border: none;
  border-radius: 5px;
  padding: 6px;                    /* p-1.5 */
  min-width: 0;
  min-height: 0;
}
.plaud-headerbtn:hover  { background: #ebebeb; }
.plaud-headerbtn:active { background: #e0e0e0; }
.plaud-vsep { background: #ebebeb; min-width: 1px; }
/* TitleInput: the big editable title, whose placeholder text is set in
   _build_content (i18n key highlight_title_placeholder). */
/* `entry.` qualified, not a bare class. GTK3 styles the entry's own `entry`
   node, and a bare .class loses to it on specificity: the title rendered
   #f5f5f5 -- the theme's entry background -- as a visible pale rectangle
   inside the #f9f9f9 panel. Measured by sampling the render, which is the
   only way it was going to be noticed. */
entry.plaud-title {
  background: transparent;
  border: none;
  box-shadow: none;
  padding: 0;
  font-size: 17px;
  color: #000;
}
/* The note entry. Its placeholder carries a leading SvgIconHighlightsNote,
   drawn beside it rather than inside the entry -- see _build_content. */
entry.plaud-note {
  background: transparent;
  border: none;
  box-shadow: none;
  padding: 0;
  font-size: 13px;                 /* text-body */
  color: #000;
}
entry.plaud-note placeholder, entry.plaud-title placeholder { color: #7a7a7a; }
.plaud-highlight-text, .plaud-highlight-text text { background: #f9f9f9; color: #000; font-size: 13px; }
.plaud-timer { color: #7a7a7a; font-size: 11px; }   /* text-footnote */
"""

_css_screens = set()


def _fill_paths(cr, paths, size, viewbox, colour):
    """Fill one or more SVG paths, scaled from `viewbox` to `size`."""
    cr.set_source_rgb(*colour)
    for d in paths:
        ov._svg_path(cr, d, size * 20.0 / viewbox)
    cr.fill()


def _stroke_paths(cr, paths, size, viewbox, colour):
    """Stroke one or more SVG paths at the SVG default width of 1."""
    cr.set_source_rgb(*colour)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_line_width(size / viewbox)
    for d in paths:
        # Each subpath is stroked on its own: the flag and the shrink glyph are
        # open paths, and replaying them into one context would join the end of
        # one to the start of the next with a visible connecting line.
        ov._svg_path(cr, d, size * 20.0 / viewbox)
        cr.stroke()


class _PanelWaveform(ov._Waveform):
    """The pill's meter with the panel's own bar width.

    index-CoA7AblW.js:1858 renders SoundWaveform with
    barClassName "bg-labels-tertiary w-[1px]" -- one pixel, and in the tertiary
    grey rather than the pill's --labels-primary black.
    """
    BAR_W = 1

    def _draw(self, w, cr):
        # The parent picks its colour itself -- one set_source_rgb(*INK) at the
        # top of its _draw -- so it cannot be passed in. It builds every bar as
        # a path and makes a single fill() at the end, which is what makes this
        # work: swapping the module constant for the duration of the call means
        # that one fill lands in the tertiary token, and the bar geometry stays
        # defined in exactly one place.
        #
        # Recolouring afterwards was tried and is wrong. push_group +
        # OPERATOR_IN paints the whole group extent, rendering the meter as a
        # solid grey block -- invisible in the first capture at 941px, obvious
        # at the real 420px. That is why PROVE APPEARANCE, NOT POSITION is a
        # rule here.
        #
        # try/finally because a raising _draw would otherwise leave the pill's
        # own meter grey for the rest of the session.
        real, ov.INK = ov.INK, LABELS_TERTIARY
        try:
            super()._draw(w, cr)
        finally:
            ov.INK = real


class _Screenshot(Gtk.Image):
    """A bounded preview whose size request never prevents window shrinking."""
    def __init__(self, path):
        super().__init__()
        self._source = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 1280, 1280, True)
        self._preview_size = None
        self.set_hexpand(True)
        self.set_alignment(0.0, 0.5)
        self._rescale(self, None)
        self.connect("size-allocate", self._rescale)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_get_preferred_width(self):
        if not hasattr(self, "_source"):
            return 1, 340
        natural = round(self._source.get_width() *
                        min(1.0, THUMBNAIL_HEIGHT / self._source.get_height()))
        return 1, min(340, max(1, natural))

    def do_get_preferred_height_for_width(self, width):
        if not hasattr(self, "_source"):
            return 1, 1
        _, height = self._dimensions(width)
        return height, height

    def _dimensions(self, width):
        ratio = min(1.0, max(1, width) / self._source.get_width(),
                    THUMBNAIL_HEIGHT / self._source.get_height())
        return (max(1, round(self._source.get_width() * ratio)),
                max(1, round(self._source.get_height() * ratio)))

    def _rescale(self, _widget, allocation):
        size = self._dimensions(allocation.width if allocation else 340)
        if size != self._preview_size:
            self._preview_size = size
            self.set_from_pixbuf(self._source.scale_simple(*size, GdkPixbuf.InterpType.BILINEAR))


class Panel(Gtk.Window):
    """The RecordingHighlight window.

    Owns no Recorder. `overlay` is the live Overlay whose recording this
    annotates, and every control here dispatches to the handler that already
    exists there.
    """

    # Footer metrics, index-CoA7AblW.js:1839-1893.
    PAD = 16          # p-4 on the bar
    GAP = 8           # gap-2 within each group
    ICON = 24         # every footer icon renders at width=24 height=24
    STOP_SQ = 14.4    # size-[14.4px] rounded-[1px] bg-labels-tertiary
    HEADER_ICON = 20  # SvgIconShrink width=20 height=20
    LOGO = 28         # size-7

    def __init__(self, overlay):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.overlay = overlay
        self.set_title("Destaques da reunião")
        self._initial_size = self._load_window_size()
        self._placed = False
        self._geometry_ready = False
        self._last_size = None
        self._float_id = self._restore_id = self._save_size_id = None
        self.set_default_size(*self._initial_size)
        self.connect("size-allocate", self._on_size_allocate)
        self.set_size_request(MIN_WIDTH, MIN_HEIGHT)
        self.set_resizable(True)
        # The official window is `type: "panel"` and floats at 420x360. A
        # tiling compositor does not care what size a toplevel asks for:
        # measured on Hyprland, this window came up 941x508 -- half the monitor
        # -- and `hyprctl clients` reported floating=False.
        #
        # Two requests, because neither is sufficient alone. set_type_hint is
        # the portable one and is what a stacking WM (or GNOME) honours, but it
        # is an xdg-shell no-op on Hyprland (measured: still floating=False
        # with the hint set). So the app id is pinned as well, giving the
        # compositor something stable to match a rule against, and _float()
        # below asks Hyprland directly for that class.
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        # Under Wayland the compositor's "class" is the xdg-shell app_id, which
        # GTK3 takes from g_prgname -- NOT from set_wmclass(), which is X11-only
        # and was measured doing nothing here (class stayed "probe.py" with it
        # set). Without this the class is argv[0], so no rule could match.
        GLib.set_prgname(WMCLASS)
        self.connect("map-event", self._on_map)

        self._apply_css()
        self._pause_path = PAUSE_PATH
        # One flag at a time -- see the SEAM comment above _on_flag.
        self._flag_pending = False
        self._build_ui()

        # The panel is a view, so it must reflect state it does not own: the
        # recording can be paused from the pill while this window is open. One
        # timer drives the clock, the waveform and the flag/crop enable state,
        # at the same 500ms the pill's own tick uses.
        self._tick_id = GLib.timeout_add(500, self._tick)
        # Same asymmetry the pill's tick has: destroy() does not touch GLib
        # sources, so without this the timer keeps firing on a dead widget and
        # holds both this Panel and the Overlay alive for the life of the
        # process.
        self.connect("destroy", self._remove_tick)
        self._sync()

    def _on_map(self, *_):
        # Deferred, not called straight from map-event: GTK emits map-event
        # before the surface is committed, so at that instant the compositor
        # has no window of this class to match and the dispatch is a silent
        # no-op (measured: floating stayed False from the handler, and the
        # identical command from a timer 1.2s later floated it correctly).
        if not self._placed and self._float_id is None:
            self._float_id = GLib.timeout_add(150, self._float)
        return False

    def _float(self):
        """Ask Hyprland to float this window at its remembered or default size.

        A window has no compositor identity until its surface is committed, so
        this runs after the map rather than during it -- see _on_map.

        `hyprctl` is shelled out to rather than configured, because a
        windowrule in ~/.config/hypr would be a file the user has to install
        for the app to look right -- and CLAUDE.md keeps user config out of the
        app. Entirely best-effort: on any other compositor the binary is absent
        and the window simply floats or tiles as that compositor decides. It
        must never be able to take the panel down, let alone the recording.

        The Lua form is not decoration. Hyprland 0.56 replaced the config
        parser, and the documented
        `hyprctl dispatch setfloating class:^x$` now fails outright here --
        "')' expected near 'class'". Dispatchers are `hl.dsp.*` objects taking
        ONE table argument; a positional selector is silently accepted and
        ignored, which fails while looking like it worked. `repl`, not `eval`:
        eval prints "ok" whatever happened, so it cannot be checked.
        A runtime `hl.window_rule` was tried first and did NOT float a window
        mapped after it -- measured -- so the dispatch runs after the first map instead.
        """
        self._float_id = None
        if not self.get_realized():
            return False
        self._placed = True
        width, height = self._initial_size
        monitor = self.get_display().get_monitor_at_window(self.get_window())
        if monitor is not None:
            area = monitor.get_workarea()
            width = min(width, max(MIN_WIDTH, area.width - 48))
            height = min(height, max(MIN_HEIGHT, area.height - 48))
        self.resize(width, height)
        try:
            result = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True,
                                    text=True, timeout=2, check=True)
            candidates = [client for client in json.loads(result.stdout)
                          if client.get("pid") == os.getpid()
                          and client.get("class") == WMCLASS
                          and client.get("title") == self.get_title()
                          and client.get("mapped")]
            if len(candidates) == 1:
                address = candidates[0]["address"]
                if not isinstance(address, str) or not address.startswith("0x"):
                    raise ValueError("Invalid compositor address")
                int(address[2:], 16)
                sel = f'window="address:{address}"'
                lua = (f'hl.dispatch(hl.dsp.window.float({{action="enable", {sel}}}))\n'
                       f'hl.dispatch(hl.dsp.window.resize('
                       f'{{x={width}, y={height}, {sel}}}))')
                subprocess.run(["hyprctl", "repl", lua], capture_output=True, timeout=2)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        self._restore_id = GLib.timeout_add(300, self._finish_restore)
        return False

    @staticmethod
    def _load_window_size():
        try:
            size = json.loads((paths.STATE / "highlights-window.json").read_text())
            width, height = size["width"], size["height"]
            if (type(width) is int and type(height) is int
                    and MIN_WIDTH <= width <= 16384 and MIN_HEIGHT <= height <= 16384):
                return width, height
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return WINDOW_WIDTH, WINDOW_HEIGHT

    def _finish_restore(self):
        self._restore_id = None
        if self.get_realized():
            self._geometry_ready = True
            self._last_size = tuple(self.get_size())
        return False

    def _on_size_allocate(self, _window, event):
        # Wayland delivers final GTK dimensions through allocation, not the
        # X11 configure-event path. Hyprland may retain maximized state after
        # floating, so save allocations once initial placement settles.
        if self._geometry_ready:
            self._last_size = (event.width, event.height)
            if self._save_size_id is not None:
                GLib.source_remove(self._save_size_id)
            self._save_size_id = GLib.timeout_add(300, self._save_window_size)
        return False

    def _save_window_size(self):
        self._save_size_id = None
        if self._last_size is not None:
            width, height = self._last_size
            if width >= MIN_WIDTH and height >= MIN_HEIGHT:
                try:
                    ov.audio.write_json(paths.STATE / "highlights-window.json",
                                        {"width": width, "height": height})
                except OSError:
                    pass
        return False

    def _resize_from_edge(self, _handle, event, edge):
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        self.begin_resize_drag(edge, event.button, int(event.x_root),
                               int(event.y_root), event.time)
        return True

    def _resize_frame(self, content):
        # GTK-owned handles work even when the compositor disables resizing
        # on its own borders. Only these strips claim pointer events.
        frame = Gtk.Grid()
        frame.get_style_context().add_class("plaud-panel")
        frame.attach(content, 1, 1, 1, 1)
        self._resize_handles = {}
        for col, row, name in ((0, 0, "NORTH_WEST"), (1, 0, "NORTH"),
                               (2, 0, "NORTH_EAST"), (0, 1, "WEST"),
                               (2, 1, "EAST"), (0, 2, "SOUTH_WEST"),
                               (1, 2, "SOUTH"), (2, 2, "SOUTH_EAST")):
            edge = getattr(Gdk.WindowEdge, name)
            handle = Gtk.EventBox()
            handle.set_size_request(6 if col != 1 else -1, 6 if row != 1 else -1)
            handle.set_hexpand(col == 1)
            handle.set_vexpand(row == 1)
            handle.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            handle.connect("button-press-event", self._resize_from_edge, edge)
            cursor = name.lower().replace("north", "n").replace("south", "s").replace("west", "w").replace("east", "e").replace("_", "") + "-resize"
            handle.connect("realize", lambda widget, name=cursor: widget.get_window().set_cursor(
                Gdk.Cursor.new_from_name(widget.get_display(), name)))
            frame.attach(handle, col, row, 1, 1)
            self._resize_handles[edge] = handle
        return frame

    def _apply_css(self):
        # Per-screen, exactly as overlay.py does it and for the same reason:
        # the provider is attached to a screen that outlives every Panel, so
        # opening the panel twice would stack two identical providers that
        # nothing ever removes.
        screen = self.get_screen()
        key = hash(screen)
        if key in _css_screens:
            return
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _css_screens.add(key)

    # --- construction ------------------------------------------------------

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("plaud-panel")
        self.get_style_context().add_class("plaud-panel")
        self.add(self._resize_frame(root))
        root.pack_start(self._build_header(), False, False, 0)
        root.pack_start(self._build_content(), True, True, 0)
        root.pack_start(self._build_footer(), False, False, 0)
        self.show_all()

    def _build_header(self):
        """logo | ... menu | separator | shrink -- index-CoA7AblW.js Header."""
        # "py-1 pr-2 pl-3.5", i.e. 4px vertical, 8px right, 14px left.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.get_style_context().add_class("plaud-panel-header")
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(14)
        bar.set_margin_end(8)

        logo = self._icon(self.LOGO, lambda w, cr, s: ov._draw_logo(w, cr, s))
        bar.pack_start(logo, False, False, 0)

        # gap-1 between the three right-hand items.
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.btn_menu = self._header_btn(
            self._icon(self.HEADER_ICON, self._draw_more), "Mais opções", self._on_menu)
        right.pack_start(self.btn_menu, False, False, 0)

        # Separator orientation="vertical" className="h-4"
        vsep = Gtk.Box()
        vsep.get_style_context().add_class("plaud-vsep")
        vsep.set_size_request(1, 16)
        vsep.set_valign(Gtk.Align.CENTER)
        right.pack_start(vsep, False, False, 0)

        self.btn_shrink = self._header_btn(
            self._icon(self.HEADER_ICON, self._draw_shrink),
            "Ocultar painel", self._on_shrink)
        right.pack_start(self.btn_shrink, False, False, 0)
        bar.pack_end(right, False, False, 0)
        return bar

    def _build_content(self):
        """The editable title, then the note entry with its pencil glyph."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # TitleInput -- index-CoA7AblW.js:1536-1544. "Destaques da reunião" is
        # this input's PLACEHOLDER (i18n key highlight_title_placeholder), not a
        # static header label: the official panel has no separate title element,
        # which is why the string renders grey in image24.png rather than black.
        self.title = Gtk.Entry()
        self.title.get_style_context().add_class("plaud-title")
        self.title.set_placeholder_text("Destaques da reunião")
        self.title.set_has_frame(False)
        box.pack_start(self.title, False, False, 0)

        # The empty state, index-CoA7AblW.js:349: SvgIconHighlightsNote at 20px
        # in text-labels-tertiary, then the placeholder span. The glyph sits
        # beside the entry rather than inside it because a GtkEntry primary icon
        # is a themed icon name, not a Cairo drawing -- and the drawn path is
        # the whole point (an icon-name lookup would render whatever the desktop
        # theme happens to have, which is the emoji-colour failure again).
        # EmptyState is cn("flex items-center gap-1", ...) = 4px, not 8.
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.pack_start(self._icon(20, self._draw_note_glyph), False, False, 0)
        self.note = Gtk.Entry()
        self.note.get_style_context().add_class("plaud-note")
        self.note.set_placeholder_text("Anote ideias ou pontos-chave na hora")
        self.note.set_has_frame(False)
        self.note.connect("activate", self._on_note_activate)
        row.pack_start(self.note, True, True, 0)
        self.entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._entry_widgets = {}
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._entries_scroll = scroll
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.pack_start(self.entries, False, False, 0)
        content.pack_start(row, False, False, 0)
        scroll.add(content)
        box.pack_start(scroll, True, True, 0)
        return box

    def _sync_entries(self):
        rec = self.overlay.rec
        records = [(kind, item) for kind, values in (
            ("note", rec.notes), ("shot", rec.screenshots),
            ("flag", getattr(rec, "flags", []))) for item in values
            if isinstance(item, dict)]
        records.sort(key=lambda pair: (pair[1].get("t", 0), pair[1].get("at", "")))
        current = {id(item) for _, item in records}
        for key in list(self._entry_widgets):
            if key not in current:
                self._entry_widgets.pop(key)[0].destroy()
        for position, (kind, item) in enumerate(records):
            key = id(item)
            if key not in self._entry_widgets:
                row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                header = Gtk.Box(spacing=8)
                t = int(item.get("t", 0))
                stamp = Gtk.Label(label=f"{t // 60:02d}:{t % 60:02d}", xalign=0)
                stamp.get_style_context().add_class("plaud-timer")
                header.pack_start(stamp, kind != "shot", kind != "shot", 0)
                remove = Gtk.Button(label="×")
                remove.get_style_context().add_class("plaud-headerbtn")
                remove.set_tooltip_text("Remover destaque")
                remove.connect("clicked", self._delete_entry, kind, item)
                if kind != "shot":
                    header.pack_end(remove, False, False, 0)
                row.pack_start(header, False, False, 0)
                editor = None
                if kind == "shot":
                    picture = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                    picture.set_halign(Gtk.Align.START)
                    remove.set_valign(Gtk.Align.START)
                    try:
                        preview = _Screenshot(item["path"])
                        preview.set_hexpand(False)
                        picture.pack_start(preview, False, False, 0)
                    except (GLib.Error, KeyError):
                        fallback = Gtk.Label(label="Não foi possível exibir a captura")
                        fallback.set_line_wrap(True)
                        fallback.set_max_width_chars(24)
                        picture.pack_start(fallback, False, False, 0)
                    picture.pack_start(remove, False, False, 0)
                    row.pack_start(picture, False, False, 0)
                else:
                    editor = Gtk.TextView()
                    editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                    editor.get_style_context().add_class("plaud-highlight-text")
                    editor.get_buffer().set_text(self._entry_text(kind, item))
                    editor.get_buffer().connect("changed", self._edit_entry, item)
                    editor.connect("key-press-event", self._on_editor_key_press)
                    row.pack_start(editor, False, False, 0)
                self.entries.pack_start(row, False, False, 0)
                self._entry_widgets[key] = (row, editor)
                row.show_all()
            row, editor = self._entry_widgets[key]
            self.entries.reorder_child(row, position)
            if editor is not None and kind == "flag" and not item.get("edited"):
                buffer = editor.get_buffer()
                text = self._entry_text(kind, item)
                if buffer.get_text(*buffer.get_bounds(), True) != text:
                    self._updating_entry = True
                    try:
                        buffer.set_text(text)
                    finally:
                        self._updating_entry = False

    @staticmethod
    def _entry_text(kind, item):
        if kind == "note" or "text" in item:
            return item.get("text", "")
        return item.get("preview_status", "Marcado. Aguardando análise…")

    def _edit_entry(self, buffer, item):
        if getattr(self, "_updating_entry", False):
            return
        item["text"] = buffer.get_text(*buffer.get_bounds(), True)
        item["edited"] = True
        self.overlay.rec._save_meta()

    def _on_editor_key_press(self, _editor, event):
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            return False
        modifiers = (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK |
                     Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK |
                     Gdk.ModifierType.META_MASK)
        if event.state & modifiers:
            return False
        self.note.grab_focus()
        GLib.idle_add(self._scroll_to_composer)
        return True

    def _scroll_to_composer(self):
        if self.get_realized():
            adjustment = self._entries_scroll.get_vadjustment()
            adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
        return False

    def _delete_entry(self, _button, kind, item):
        values = getattr(self.overlay.rec, {"note": "notes", "shot": "screenshots", "flag": "flags"}[kind])
        values[:] = [entry for entry in values if entry is not item]
        self.overlay.rec._save_meta()
        self._sync_entries()

    def _build_footer(self):
        """pause, stop | waveform, timer | flag, crop -- the Footer component."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.set_margin_top(self.PAD)
        bar.set_margin_bottom(self.PAD)
        bar.set_margin_start(self.PAD)
        bar.set_margin_end(self.PAD)

        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=self.GAP)
        self.pause_icon = self._icon(
            self.ICON, lambda w, cr, s: _fill_paths(
                cr, (self._pause_path,), s, 20.0, LABELS_TERTIARY))
        self.btn_pause = self._footer_btn(self.pause_icon, "Pausar", self._on_pause)
        left.pack_start(self.btn_pause, False, False, 0)
        self.btn_stop = self._footer_btn(
            self._stop_square(), "Parar e enviar", self._on_stop)
        left.pack_start(self.btn_stop, False, False, 0)
        bar.pack_start(left, False, False, 0)

        # "flex shrink-0 items-center gap-2 px-0.5" -- waveform then timer.
        mid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=self.GAP)
        mid.set_valign(Gtk.Align.CENTER)
        # px-0.5 -- transcribed in the comment above but never applied.
        mid.set_margin_start(2)
        mid.set_margin_end(2)
        # SoundWaveform with barClassName "bg-labels-tertiary w-[1px]". The
        # pill's meter is reused rather than rebuilt -- same component in the
        # official client too -- but the panel passes it a THINNER bar: w-[1px]
        # here against the pill's w-0.5 (2px). Subclassed rather than
        # parameterised, because BAR_W is the only difference and overlay.py is
        # not ours to widen for a second caller.
        self.wave = _PanelWaveform(ov.Overlay.WAVE_W, ov.Overlay.WAVE_H)
        mid.pack_start(self.wave, False, False, 0)
        # <p className="text-footnote text-labels-tertiary">, e.g. "03:21".
        self.timer = Gtk.Label(label="00:00")
        self.timer.get_style_context().add_class("plaud-timer")
        mid.pack_start(self.timer, False, False, 0)
        bar.set_center_widget(mid)

        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=self.GAP)
        self.btn_flag = self._footer_btn(
            self._icon(self.ICON, self._draw_flag), "Marcar", self._on_flag)
        right.pack_start(self.btn_flag, False, False, 0)
        self.btn_shot = self._footer_btn(
            self._icon(self.ICON, self._draw_screenshot),
            "Capturar tela", self._on_shot)
        right.pack_start(self.btn_shot, False, False, 0)
        bar.pack_end(right, False, False, 0)
        return bar

    # --- widget helpers ----------------------------------------------------

    def _icon(self, size, draw):
        a = Gtk.DrawingArea()
        a.set_size_request(size, size)
        a.set_halign(Gtk.Align.CENTER)
        a.set_valign(Gtk.Align.CENTER)
        a.connect("draw", draw, size)
        return a

    def _footer_btn(self, child, tooltip, cb):
        b = Gtk.Button()
        b.add(child)
        b.get_style_context().add_class("plaud-footerbtn")
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_valign(Gtk.Align.CENTER)
        b.set_tooltip_text(tooltip)
        b.connect("clicked", cb)
        return b

    def _header_btn(self, child, tooltip, cb):
        b = Gtk.Button()
        b.add(child)
        b.get_style_context().add_class("plaud-headerbtn")
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_valign(Gtk.Align.CENTER)
        b.set_tooltip_text(tooltip)
        b.connect("clicked", cb)
        return b

    def _stop_square(self):
        """size-6 box holding a 14.4px rounded-[1px] bg-labels-tertiary block."""
        a = Gtk.DrawingArea()
        a.set_size_request(self.ICON, self.ICON)
        a.set_halign(Gtk.Align.CENTER)

        def draw(_w, cr, _s):
            cr.set_source_rgb(*LABELS_TERTIARY)
            off = (self.ICON - self.STOP_SQ) / 2.0
            # rounded-[1px]: a 1px radius on a 14.4px square, so this is a
            # square with the corners just taken off, not a rounded button.
            r, s = 1.0, self.STOP_SQ
            cr.new_sub_path()
            cr.arc(off + s - r, off + r, r, -1.5708, 0)
            cr.arc(off + s - r, off + s - r, r, 0, 1.5708)
            cr.arc(off + r, off + s - r, r, 1.5708, 3.1416)
            cr.arc(off + r, off + r, r, 3.1416, 4.7124)
            cr.close_path()
            cr.fill()
        a.connect("draw", draw, self.ICON)
        return a

    # --- glyphs ------------------------------------------------------------

    def _draw_flag(self, _w, cr, size):
        _stroke_paths(cr, (FLAG_PATH,), size, 20.0, self._ink(self.btn_flag))

    def _draw_screenshot(self, _w, cr, size):
        _fill_paths(cr, SCREENSHOT_PATHS, size, 24.0, self._ink(self.btn_shot))

    def _draw_shrink(self, _w, cr, size):
        # className "text-labels-secondary" = #3d3d3d. The tertiary token was
        # reused here on the claim that #7a7a7a was "close enough"; it is not
        # -- 122 vs 61 of 255 is roughly double the luminance. Sampling
        # image24.png finds #3e3e3e at this glyph, which is secondary.
        _stroke_paths(cr, SHRINK_PATHS, size, 20.0, LABELS_SECONDARY)

    def _draw_note_glyph(self, _w, cr, size):
        cr.set_source_rgb(*LABELS_TERTIARY)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_width(ov.PENCIL_STROKE * size / 20.0)
        ov._svg_path(cr, ov.PENCIL_PATH, size)
        cr.stroke()

    def _draw_more(self, _w, cr, size):
        """CancelRecordingMenu's trigger: the "..." overflow dot row.

        Header wraps SvgIconMore in SingleIconButton variant="light", whose
        branch applies no text-labels-* class, so currentColor falls through
        to black -- not tertiary. Sampling image24.png agrees: the dot core
        is #000000, not #7a7a7a.

        The centres are read off SvgIconMore's three subpaths rather than
        spaced by eye: 6.25 / 10.0 / 13.75 at r=1.25 on a 20 viewBox. An
        earlier 5.5 / 10.0 / 14.5 spread the outer dots 20% too far apart.
        """
        cr.set_source_rgb(*LABELS_PRIMARY)
        k = size / 20.0
        for x in (6.25, 10.0, 13.75):
            cr.arc(x * k, 10 * k, 1.25 * k, 0, 6.2832)
            cr.fill()

    def _ink(self, btn):
        """The disabled colour swap the official client does with a class.

        isPauseRecording composes "text-labels-disabled" onto the flag and
        screenshot buttons (index-CoA7AblW.js:1865,1885). GTK draws the
        DrawingArea child itself, so the colour has to be chosen here rather
        than inherited -- currentColor has no counterpart in Cairo.
        """
        return LABELS_TERTIARY if btn.get_sensitive() else LABELS_DISABLED

    # --- state -------------------------------------------------------------

    def _tick(self):
        self._sync()
        return True

    def _remove_tick(self, *_):
        for attr in ("_float_id", "_restore_id", "_save_size_id"):
            source = getattr(self, attr, None)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, attr, None)
        self._save_window_size()
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _sync(self):
        """Mirror the Recorder's state onto the footer.

        Called on a timer rather than on our own button presses alone, because
        the pill can pause the same recording behind this window's back.
        """
        self._sync_entries()
        rec = self.overlay.rec
        state = rec.state
        elapsed = rec.elapsed()
        self.timer.set_text(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        self.wave.set_active(state == "recording")

        want = PLAY_PATH if state == "paused" else PAUSE_PATH
        if want is not self._pause_path:
            self._pause_path = want
            self.pause_icon.queue_draw()
            self.btn_pause.set_tooltip_text(
                "Retomar" if state == "paused" else "Pausar")

        # The flag and the crop are inert while paused, matching
        # index-CoA7AblW.js:1865,1885 and docs/plaud-desktop/README.md:184-190.
        # For the crop this is our own addition of a rule the official client
        # already has; for the flag it is load-bearing, because the ring buffer
        # keeps filling during a pause (CAN-309 plan, Experiment 4) and a flag
        # pressed just after resume would return audio the recording does not
        # contain.
        live = state == "recording"
        for btn in (self.btn_flag, self.btn_shot):
            if btn.get_sensitive() != live:
                btn.set_sensitive(live)
                btn.get_child().queue_draw()   # repaint in the disabled ink
                btn.set_tooltip_text(
                    {"flag": "Marcar", "shot": "Capturar tela"}[
                        "flag" if btn is self.btn_flag else "shot"]
                    if live else "Retome a gravação para adicionar destaques")

    # --- handlers ----------------------------------------------------------
    #
    # Every one of these dispatches into the Overlay. The panel deliberately
    # holds no duplicate of pause/stop/screenshot logic: _on_shot in particular
    # carries a _shot_pending guard, a spawn-and-watch chain and a kill-on-
    # destroy path, and a second copy would mean two guards protecting one
    # subprocess.

    def _on_pause(self, *_):
        self.overlay._on_pause()
        self._sync()

    def _on_stop(self, *_):
        # The recording is ending, so the panel goes with it. destroy() first:
        # _do_stop() destroys the Overlay and hands the session to the upload
        # thread, and a Panel still holding a tick on a destroyed Overlay would
        # keep ticking against a stopped Recorder.
        self.destroy()
        self.overlay._on_stop()

    def _on_shot(self, *_):
        self.overlay._on_shot()

    # ---- the flag ----------------------------------------------------------
    #
    # WHAT THIS BUTTON IS. The flag (SvgIconFlag, tooltip "Highlight",
    # Alt+Shift+H) is what triggers Plaud's AI to summarize the audio that just
    # played. It is NOT the pencil -- see docs/plans/CAN-309-annotation-panel.md
    # section 0, which corrects that exact confusion.
    #
    # dump_ogg() returns None for an empty buffer. That is the client-side skip
    # at recordingService-Mile2cgU.js:1033 -- honour it and send nothing,
    # rather than uploading silence.
    #
    # WHAT IS ALREADY CORRECT AND MUST BE KEPT.
    #   - `elapsed` is sampled FIRST, before any other work. The mark belongs to
    #     the moment the user pressed, and everything after this line can block.
    #   - the button is insensitive while paused (see _sync), so this cannot run
    #     against a ring buffer that has been filling through a pause.
    #   - the one-at-a-time guard, mirroring _shot_pending: two presses a second
    #     apart would otherwise build colliding snippet paths.
    #   - a flag after _do_stop() must be refused. rec.state is the check that
    #     already exists for exactly this in _shot_done, and the reason is the
    #     same: past that point the lists belong to the upload thread, so
    #     attaching would promise the user a mark that is never sent.

    def _on_flag(self, *_):
        elapsed = self.overlay.rec.elapsed()
        if self._flag_pending:
            return
        if self.overlay.rec.state != "recording":
            return
        self._flag_pending = True
        try:
            self._flag_dump(elapsed)
        finally:
            self._flag_pending = False

    def _flag_dump(self, elapsed):
        """Dump the last 40 s from the ring and register it as a flag.

        Lands beside the screenshots rather than in `segdir`, because segdir is
        emptied and removed by _concat() -- a snippet written there would be
        deleted before the upload thread ever read it.
        """
        rec = self.overlay.rec
        buf = rec.mark_buffer
        if buf is None:
            # No ring: the capture never started one for this segment. Say so
            # rather than reporting a mark that nothing will send -- reporting
            # success for work that did not happen is the shape of false green
            # this project has been burned by before.
            self.overlay._notify("Plaud", "Marcação falhou: sem buffer de áudio")
            return
        ts = datetime.now().strftime("%H%M%S")
        out = self.overlay.shots_dir / f"flag_{int(elapsed):06d}_{ts}.ogg"
        # dump_ogg() blocks on an ffmpeg transcode of at most 40 s of PCM --
        # measured well under a second, and the capture itself is a separate
        # process, so the recording does not pause while this runs.
        got = buf.dump_ogg(out)
        if got is None:
            # Empty ring: the official client bails the same way rather than
            # uploading silence (recordingService-Mile2cgU.js:1033).
            self.overlay._notify("Plaud", "Marcação vazia: nada foi capturado ainda")
            return
        rec.add_flag(got, elapsed)
        self.overlay._start_flag_preview(rec.flags[-1])
        self._sync_entries()

    def _on_note_activate(self, *_):
        # Same contract as the dialog this replaces: sample the clock BEFORE
        # anything else, because the note belongs to the moment the user decided
        # to write it, not to when the text was committed. Here the gap is a
        # keystroke rather than a modal dialog, but the reason is unchanged and
        # so is the order.
        elapsed = self.overlay.rec.elapsed()
        text = self.note.get_text().strip()
        if not text:
            return
        self.overlay.rec.add_note(text, elapsed)
        self.note.set_text("")
        self._sync_entries()
        self.note.grab_focus()
        GLib.idle_add(self._scroll_to_composer)

    def _on_menu(self, *_):
        self.overlay._on_kebab(self.btn_menu)

    def _on_shrink(self, *_):
        # handleScale, index-CoA7AblW.js:1918-1935: shows the mini widget and
        # calls hideRecordingHighlightWindow(). Our pill is never hidden while
        # the panel is open, so the equivalent is simply closing this window --
        # the recording continues, which is the property that matters.
        self.destroy()


def open_for(overlay):
    """Open the panel over `overlay`, or present the one already open.

    Returns the Panel. Does NOT run a GTK loop: tray.run_tray() owns the one
    Gtk.main() (CLAUDE.md Threading), and a second loop here would nest.
    """
    existing = getattr(overlay, "_panel", None)
    if existing is not None:
        existing.present()
        return existing
    p = Panel(overlay)
    overlay._panel = p
    # Reopening must build a fresh Panel rather than present a destroyed one.
    p.connect("destroy", lambda *_: setattr(overlay, "_panel", None))
    # The panel must not outlive the recording it annotates: _do_stop() destroys
    # the Overlay, and without this the panel would linger over a finished
    # session with a frozen timer and buttons wired to a stopped Recorder.
    overlay.connect("destroy", lambda *_: p.destroy())
    p.present()
    return p


if __name__ == "__main__":
    # UI only -- this path records but never uploads, exactly as
    # `python3 -m plaud_linux.overlay` does.
    mode = sys.argv[1] if len(sys.argv) > 1 else "system"
    o = ov.run(mode=mode)
    open_for(o)
    Gtk.main()
