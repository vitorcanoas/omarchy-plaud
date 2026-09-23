#!/usr/bin/env python3
"""
plaud-linux :: recording-main (the 280x215 main recording panel)

The official client's primary recording window, rebuilt from app.asar rather
than approximated. Every constant below names the file it was read from; see
docs/plaud-desktop/OFFICIAL-UI-SPEC.md sections 2.1-2.4, which is the design
tie-breaker for this module.

  main/index-BzODulx0.js   280 x 215, frameless, transparent, alwaysOnTop,
                           x = workArea.width - 280 - 10, y = 20
  renderer/assets/index-BxLo-e8r.js
                           RecordingMain() card, Header(), RecordPanel(),
                           RecordControl()

Layout, top to bottom:

  card     rounded-[10px] bg-background, 1px border-separator-default
  header   h-[52px], border-b, pr-2 pl-4:  logo | settings | highlight
  content  p-4 holding RecordControl -- the black bar carrying
           pause/resume | waveform + timer | stop | separator | more

This window does NOT own a Recorder and does NOT touch the network, exactly as
panel.py does not: it is a view over the Overlay that opened it, and pause,
stop and the note panel call that Overlay's existing handlers rather than
reimplementing them. One implementation of each, one set of guards.

A gtk-layer-shell surface, unlike panel.py and like overlay.py. The official
window is `alwaysOnTop`, not resizable, and placed at a computed screen
coordinate -- which is precisely the three things Wayland denies a normal
toplevel and layer-shell grants. panel.py is a normal window because ITS
official counterpart is `resizable: true` and user-movable; this one is not.

The idle and upload cards live in desktop_ui.py. Tray activation opens the
appropriate view without starting capture; the recording button starts it.
"""
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, Gdk, GLib, GtkLayerShell  # noqa: E402

import sys  # noqa: E402

try:
    from . import overlay as ov  # noqa: E402
except ImportError:
    import overlay as ov  # noqa: E402


# --- official window geometry, main/index-BzODulx0.js ------------------------
#
# 280x215 is the whole window including the transparent margin the card's
# shadow is drawn into. Unlike recording-mini -- where windowService names
# MINI_WINDOW_PADDING = 8 explicitly and the body is 36x144 inside a 52x160
# window -- no such padding constant exists for this window: the card fills it
# (`h-full`, spec 2.1) and the mac-only `px-5 pt-2 pb-5` wrapper is the only
# inset, applied on macOS alone. So on Linux the card IS 280x215.
WINDOW_WIDTH = 280
WINDOW_HEIGHT = 215

# `x = primaryDisplay.workArea.width - 280 - 10, y = 20` (spec 1). Expressed as
# layer-shell margins from the top-right, which is the same corner: the
# compositor subtracts any bar's exclusive zone before applying them, so this
# tracks the work area the way the official arithmetic does rather than
# assuming the screen's full height.
MARGIN_TOP = 20
MARGIN_RIGHT = 10

# --- official token colours, renderer/assets/index-CYM9K6Ws.css :root --------
#
# Light theme, as in overlay.py and panel.py and for the same measured reason:
# the official windows are light regardless of the desktop theme.
LABELS_PRIMARY = (0.0, 0.0, 0.0)            # --labels-primary   #000
LABELS_WHITE = (1.0, 1.0, 1.0)              # --labels-white     #fff
LABELS_TERTIARY = (0.478, 0.478, 0.478)     # --labels-tertiary  #7a7a7a

CSS = b"""
/* --background-primary #fff (the card), --separator-default #ebebeb.
   Applied to the window as well as the card for the reason panel.py records:
   styling only the box leaves the GtkWindow painting the desktop theme's own
   background behind it. */
.plaud-main, window.plaud-main {
  background-color: #fff;
  border-radius: 10px;             /* rounded-[10px], spec 2.1 */
  border: 1px solid #ebebeb;       /* border-[1px] border-separator-default */
}
/* The header carries the card's own background explicitly, not just the
   border. set_app_paintable(True) is needed for the rounded corners to be
   see-through, but it also stops GTK painting a background behind child
   widgets that do not declare one -- so the 52px header strip rendered
   TRANSPARENT and a window behind it showed through. Measured on a grim
   capture over a terminal: the header sampled rgb(105,143,181), the desktop,
   while the area below the bar was correctly #fff. Caught only because the
   capture was taken over a dark window; over a white desktop the bug is
   invisible, which is why appearance is proven against a known ground. */
.plaud-main-header {
  background-color: #fff;
  /* Matching the card's own rounded-[10px] on the two top corners. A square
     header over a rounded card leaves two lit notches where the header's
     corner overhangs the card's curve -- measured: the top-right corner still
     showed the window behind it after the background above was added. */
  border-radius: 10px 10px 0 0;
  border-bottom: 1px solid #ebebeb;
}
/* SingleIconButton, as in the pill and the panel: invisible until hovered. */
.plaud-mainbtn {
  background: transparent;
  border: none;
  border-radius: 5px;
  padding: 4px;                    /* p-1 */
  min-width: 0;
  min-height: 0;
}
.plaud-mainbtn:hover  { background: #ebebeb; }
.plaud-mainbtn:active { background: #e0e0e0; }
/* RecordControl, spec 2.4:
   "flex h-full w-full items-center rounded-[5px] bg-labels-primary p-1".
   bg-labels-primary is #000 in the light theme, so this bar is black and
   every glyph on it is --labels-white. */
.plaud-recordcontrol {
  background-color: #000;
  border-radius: 5px;
  padding: 4px;                    /* p-1 */
}
/* The bar's own icon buttons: same SingleIconButton chrome, but hovering has
   to lighten a black ground rather than darken a white one. */
.plaud-darkbtn {
  background: transparent;
  border: none;
  border-radius: 5px;
  padding: 4px;
  min-width: 0;
  min-height: 0;
}
.plaud-darkbtn:hover  { background: rgba(255,255,255,0.12); }
.plaud-darkbtn:active { background: rgba(255,255,255,0.20); }
/* Separator orientation="vertical" className="h-6 bg-labels-white/20". */
.plaud-darksep { background: rgba(255,255,255,0.20); min-width: 1px; }
/* data-testid="recording-timer", w-[62px]. text-footnote is 12px. */
.plaud-maintimer { color: #fff; font-size: 12px; }
"""

_css_screens = set()

# The class the compositor sees, so this surface is addressable in
# `hyprctl layers` -- the same reason overlay.py namespaces its own.
NAMESPACE = "plaud-recording-main"


class _DarkWaveform(ov._Waveform):
    """The pill's meter, drawn in --labels-white for the black RecordControl bar.

    Same swap panel.py's _PanelWaveform makes, for the same reason and with the
    same caveat: the parent picks its colour with one set_source_rgb(*INK) and
    makes a single fill() at the end, so swapping the module constant for the
    duration of the call is what recolours it. Recolouring the rendered result
    afterwards paints the whole group extent and renders the meter as a solid
    block -- measured in panel.py, not re-discovered here.
    """

    def _draw(self, w, cr):
        real, ov.INK = ov.INK, LABELS_WHITE
        try:
            super()._draw(w, cr)
        finally:
            ov.INK = real


def _draw_more(_w, cr, size):
    """SvgIconMore: three dots in a row, white on the RecordControl bar.

    The kebab that opens CancelRecordingMenu (spec 6.1). Drawn rather than
    reusing panel.py's own _draw_more, which is a bound method hardcoding the
    panel's tertiary grey.
    """
    cr.set_source_rgb(*LABELS_WHITE)
    k = size / 20.0
    for x in (5.0, 10.0, 15.0):
        cr.arc(x * k, 10 * k, 1.25 * k, 0, 2 * 3.141592653589793)
        cr.fill()


def _draw_settings(_w, cr, size):
    """SvgIconSettings: a gear, stroked in --labels-primary.

    APPROXIMATED, and marked as such rather than claimed: the official gear's
    own path was not extracted in this pass, so this is a drawn gear on the
    same 20x20 grid as every other glyph here -- eight teeth around a ring --
    not a copy of SvgIconSettings. Replace it with the real path when the icon
    file is read; everything else in this module is verbatim from the source.
    """
    import math
    cr.set_source_rgb(*LABELS_PRIMARY)
    k = size / 20.0
    cx = cy = 10 * k
    cr.set_line_width(1.4 * k)
    for i in range(8):
        a = i * math.pi / 4.0
        cr.move_to(cx + math.cos(a) * 5.4 * k, cy + math.sin(a) * 5.4 * k)
        cr.line_to(cx + math.cos(a) * 7.6 * k, cy + math.sin(a) * 7.6 * k)
    cr.stroke()
    cr.arc(cx, cy, 5.0 * k, 0, 2 * math.pi)
    cr.stroke()
    cr.arc(cx, cy, 2.0 * k, 0, 2 * math.pi)
    cr.stroke()


class RecordingMain(Gtk.Window):
    """The recording-main window: a view over a live Overlay.

    Built only while a recording exists, so it renders RecordControl (spec
    2.4) and never RecordPanel's Idle button -- see the module docstring for
    why that state is unreachable here.
    """

    # --- official element geometry, in px, all from spec 2.2 / 2.4 ---------
    HEADER_H = 52         # h-[52px]
    HEADER_PL = 16        # pl-4
    HEADER_PR = 8         # pr-2
    HEADER_GAP = 4        # gap-1
    LOGO = 20             # WsAvatar size={20} / the 20x20 SvgIconPlaud
    CONTENT_PAD = 16      # the p-4 wrapper around RecordControl (spec 0.1)
    ICON = 20             # the icon box inside a SingleIconButton
    BAR_PAD = 4           # p-1
    TIMER_W = 62          # w-[62px]
    STOP_SQ = 12          # h-3 w-3 rounded-[1px] bg-labels-white
    VSEP_H = 24           # h-6
    WAVE_W = 24
    WAVE_H = 20

    def __init__(self, overlay):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.ov = overlay
        self._tick_id = None

        self.set_decorated(False)
        self.set_resizable(False)
        # The official window is transparent:true and its card is the only
        # thing painted -- the rounded corners need the ground behind them to
        # be see-through, or the 10px radius reads as white notches.
        self.set_app_paintable(True)
        screen = self.get_screen()
        vis = screen.get_rgba_visual()
        if vis:
            self.set_visual(vis)
        self.set_size_request(WINDOW_WIDTH, WINDOW_HEIGHT)

        # Before the first realize, as in overlay.py: init_for_window() must
        # run ahead of show_all().
        self._init_layer_shell()
        self._apply_css()
        self._build_ui()

        self.connect("destroy", self._on_destroy)
        self._overlay_destroy_id = self.ov.connect("destroy", lambda *_: self.destroy())

    def _init_layer_shell(self):
        """Place, stack and stick this window, the way overlay.py does."""
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, NAMESPACE)
        # OVERLAY for the reason overlay.py records: TOP sits below fullscreen
        # surfaces, so the panel would vanish behind exactly the fullscreen
        # call this app exists to record.
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, MARGIN_TOP)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, MARGIN_RIGHT)
        # Unlike the pill, this window has buttons the user aims at, but it
        # still has no text entry -- the note entry lives in panel.py, a normal
        # toplevel that focuses itself. ON_DEMAND would let this surface take
        # keyboard focus away from the meeting being recorded for no gain.
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_exclusive_zone(self, 0)

    def _apply_css(self):
        # Per-screen, exactly as overlay.py and panel.py do it: the provider is
        # attached to a screen that outlives every window, so opening this
        # twice would otherwise stack two identical providers.
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
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("plaud-main")
        self.get_style_context().add_class("plaud-main")
        card.pack_start(self._build_header(), False, False, 0)
        card.pack_start(self._build_content(), True, True, 0)
        self.add(card)
        self.show_all()
        self._sync()
        self._tick_id = GLib.timeout_add(500, self._tick)

    def _build_header(self):
        """h-[52px] w-full items-center gap-1 border-b pr-2 pl-4 (spec 2.2)."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                      spacing=self.HEADER_GAP)
        row.get_style_context().add_class("plaud-main-header")
        row.set_size_request(-1, self.HEADER_H)
        row.set_margin_start(self.HEADER_PL)
        row.set_margin_end(self.HEADER_PR)

        # 1. Workspace indicator (flex-1). We have no team-workspace avatar to
        #    draw, which is the official fallback anyway: "otherwise a 20x20
        #    Plaud logo" (spec 2.2). So this is the logo branch, not a
        #    simplification of the avatar branch.
        logo = self._icon(self.LOGO, ov._draw_logo)
        logo.set_halign(Gtk.Align.START)
        row.pack_start(logo, True, True, 0)

        try:
            from . import settings
        except ImportError:
            import settings
        gear = self._iconbtn(self._icon(self.ICON, _draw_settings),
                             "Preferências", lambda *_: settings.open_settings())
        row.pack_start(gear, False, False, 0)

        # 5. Highlight button, "only while recording" -- which is the only
        #    state this window has. Opens the same panel the pill's pencil
        #    opens, through the Overlay's own handler.
        note = self._iconbtn(self._icon(self.ICON, ov._draw_pencil),
                             "Destaques", lambda *_: self.ov._on_note())
        row.pack_start(note, False, False, 0)
        return row

    def _build_content(self):
        """The p-4 wrapper (spec 0.1) holding RecordControl (spec 2.4)."""
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for setter in (wrap.set_margin_top, wrap.set_margin_bottom,
                       wrap.set_margin_start, wrap.set_margin_end):
            setter(self.CONTENT_PAD)
        # START, not CENTER. RecordControl is `h-full`, but the height it
        # fills is its own row inside the p-4 div -- below it the official
        # panel stacks a separator, <UploadProgress/> and <UploadHistory/>
        # (spec 2.3 item 4). We render none of those, so the space they would
        # occupy is genuinely empty here. Stretching the bar down through it
        # would invent a geometry the source does not describe; leaving the
        # bar where the official one sits, with the empty list below it, is
        # the honest reading.
        wrap.set_valign(Gtk.Align.START)
        wrap.pack_start(self._build_record_control(), False, False, 0)
        return wrap

    def _build_record_control(self):
        """The black bar, element order verbatim from the spec 2.4 JSX."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.get_style_context().add_class("plaud-recordcontrol")

        # [pause-or-resume icon button]
        self.pause_icon = self._icon(self.ICON, self._draw_pause_white)
        self.btn_pause = self._iconbtn(self.pause_icon, "Pausar",
                                       self._on_pause, dark=True)
        bar.pack_start(self.btn_pause, False, False, 0)

        # [SoundWaveform] [timer text, w-[62px]]
        self.wave = _DarkWaveform(self.WAVE_W, self.WAVE_H)
        bar.pack_start(self.wave, False, False, 0)
        self.timer = Gtk.Label(label="00:00")
        self.timer.get_style_context().add_class("plaud-maintimer")
        self.timer.set_size_request(self.TIMER_W, -1)
        # expand=True on the timer, but fill=False: the bar is `w-full` and
        # something has to absorb the slack, and the waveform+timer group is
        # what sits between the pause button and the stop button. fill=False
        # keeps the label itself at its w-[62px] and centres it in the slack
        # rather than stretching the text box across it.
        bar.pack_start(self.timer, True, False, 0)

        # [stop button: h-3 w-3 rounded-[1px] bg-labels-white square]
        bar.pack_start(self._iconbtn(self._stop_square(), "Parar",
                                     self._on_stop, dark=True),
                       False, False, 0)

        # <Separator orientation="vertical" className="h-6 bg-labels-white/20"/>
        vsep = Gtk.Box()
        vsep.get_style_context().add_class("plaud-darksep")
        vsep.set_size_request(1, self.VSEP_H)
        vsep.set_valign(Gtk.Align.CENTER)
        bar.pack_start(vsep, False, False, self.BAR_PAD)

        more = self._iconbtn(self._icon(self.ICON, _draw_more),
                             "Mais", lambda b: self.ov._on_kebab(b), dark=True)
        bar.pack_start(more, False, False, 0)
        return bar

    # --- widget helpers, the same three panel.py has -----------------------

    def _icon(self, size, draw):
        a = Gtk.DrawingArea()
        a.set_size_request(size, size)
        a.set_valign(Gtk.Align.CENTER)
        a.connect("draw", lambda w, cr: draw(w, cr, size))
        return a

    def _iconbtn(self, child, tooltip, cb, dark=False):
        b = Gtk.Button()
        b.add(child)
        b.get_style_context().add_class(
            "plaud-darkbtn" if dark else "plaud-mainbtn")
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_valign(Gtk.Align.CENTER)
        b.set_tooltip_text(tooltip)
        if cb is not None:
            b.connect("clicked", cb)
        return b

    def _stop_square(self):
        """The ICON-sized box holding a 12px rounded-[1px] white block."""
        a = Gtk.DrawingArea()
        a.set_size_request(self.ICON, self.ICON)
        a.set_valign(Gtk.Align.CENTER)

        def draw(_w, cr):
            # rounded-[1px] on a 12px square: a square with the corners just
            # taken off, not a rounded button -- as panel.py records for its
            # own 14.4px one.
            r, s = 1.0, self.STOP_SQ
            x = y = (self.ICON - s) / 2.0
            cr.set_source_rgb(*LABELS_WHITE)
            cr.new_sub_path()
            cr.arc(x + s - r, y + r, r, -1.5708, 0)
            cr.arc(x + s - r, y + s - r, r, 0, 1.5708)
            cr.arc(x + r, y + s - r, r, 1.5708, 3.1416)
            cr.arc(x + r, y + r, r, 3.1416, 4.7124)
            cr.close_path()
            cr.fill()
        a.connect("draw", draw)
        return a

    def _draw_pause_white(self, w, cr, size):
        """overlay.py's pause/resume glyphs, in --labels-white for this bar."""
        real, ov.INK = ov.INK, LABELS_WHITE
        try:
            (ov._draw_play if self.ov.rec.state == "paused"
             else ov._draw_pause)(w, cr, size)
        finally:
            ov.INK = real

    # --- live state --------------------------------------------------------

    def _tick(self):
        self._sync()
        return True

    def _sync(self):
        """Repaint everything that follows the Recorder's state.

        Reads the Overlay's Recorder rather than holding one, and touches no
        state of its own -- so this window and the pill can both be up and
        neither can disagree with the engine.
        """
        paused = self.ov.rec.state == "paused"
        self.wave.set_active(not paused)
        self.pause_icon.queue_draw()
        self.btn_pause.set_tooltip_text("Retomar" if paused else "Pausar")
        secs = self.ov.rec.elapsed()
        self.timer.set_text("%02d:%02d" % (secs // 60, secs % 60))

    def _remove_tick(self):
        # The tick outlives the window unless removed explicitly: destroy()
        # does not touch GLib sources, so a closed window would leave a timer
        # firing 2x/s on dead widgets and holding this window alive -- the same
        # leak overlay.py's _remove_tick exists to prevent.
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _on_destroy(self, *_):
        if getattr(self, "_overlay_destroy_id", None):
            self.ov.disconnect(self._overlay_destroy_id)
            self._overlay_destroy_id = None
        self._remove_tick()
        global _live
        if _live is self:
            _live = None

    # --- handlers: all of them delegate ------------------------------------

    def _on_pause(self, *_):
        # The Overlay's handler, not a second copy: it owns the notification,
        # its own icon swap and the state machine. _sync() then catches this
        # window up on the next tick, and immediately so the button does not
        # look dead for half a second.
        self.ov._on_pause()
        self._sync()

    def _on_stop(self, *_):
        # Likewise -- and this one matters more than the others: _do_stop()
        # carries the single-execution guard that keeps a double press from
        # uploading twice. Reimplementing stop here would be a second way into
        # the upload with no guard on it.
        self.ov._on_stop()


# The one live window, so the tray/pill cannot stack a second copy over the
# first. Module-level for the same reason panel.py keeps its own: this window
# outlives the click that made it.
_live = None


def open_for(overlay):
    """Show the recording-main window for a live Overlay. Returns it."""
    global _live
    if _live is not None:
        _live.present()
        return _live
    _live = RecordingMain(overlay)
    return _live


if __name__ == "__main__":
    # Standalone smoke test: python3 -m plaud_linux.recording_main [mode]
    # Records for real, so PLAUD_LINUX_HOME should point at a scratch dir.
    mode = sys.argv[1] if len(sys.argv) > 1 else "system"
    o = ov.run(mode=mode, on_stop=lambda rec: Gtk.main_quit())
    open_for(o)
    Gtk.main()
