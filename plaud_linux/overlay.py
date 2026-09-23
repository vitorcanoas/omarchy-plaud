#!/usr/bin/env python3
"""
plaud-linux :: floating overlay (GTK3 + gtk-layer-shell)

Mirrors the Plaud Desktop UX. The resting pill is the official client's own
mini window, rebuilt from its source rather than approximated: a 36x144 light
vertical strip holding the Plaud logo, a five-bar level meter, a separator, a
square stop button and the pencil that opens the note dialog. Geometry, colours
and icon paths all come from app.asar (see the constants below for the file each
one came from) and are confirmed against docs/plaud-desktop/media/image23.png.

  - click the pill to expand -> reveals Pause / Screenshot
  - Stop ends the recording and hands off to the upload flow

The official client splits these across two windows: the mini pill carries only
stop and the pencil, while pause, the elapsed timer and the screenshot tool live
in a separate note panel the pencil opens. We have no second window, so the two
controls it cannot hold are collapsed below the official stack instead of being
dropped -- the pill at rest is the official strip exactly, and the extra column
appears only on click.

On Hyprland the pill is a floating pinned toplevel, movable with SUPER+drag.
A pre-map rule keeps it above fullscreen without disabling pointer input.
Other compositors, or failed rule registration, retain the established
wlr-layer-shell anchor/margin placement.
Talks to the audio engine (lib/audio.py). No network here.
"""
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, Gdk, GLib, GtkLayerShell  # noqa: E402

import cairo  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from datetime import datetime  # noqa: E402

try:
    from . import audio  # noqa: E402
except ImportError:
    import audio  # noqa: E402

SHOTS_DIRNAME = "screenshots"
HYPRLAND = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))

# Screens already carrying the CSS provider, by GdkScreen pointer.
#
# Not a WeakSet, which was tried and is silently useless here: nothing holds a
# strong reference to the Gdk.Screen *wrapper*, so the entry is gone before the
# next line runs (measured: len() == 0 immediately after add()). Not the wrapper
# objects either, since PyGObject may hand out a fresh one per get_screen().
# The pointer is the stable identity, and a screen count is bounded by the
# user's monitors, so this set cannot grow without limit.
_css_screens = set()

CSS = b"""
/* Colours are the official client's own tokens, light theme, read from
   renderer/assets/index-CYM9K6Ws.css (:root block):
     --background-secondary #f9f9f9   pill body
     --labels-primary       #000      every glyph ("currentColor")
     --separator-default    #ebebeb   the rule above the stop button
     --gray-1               #ebebeb   SingleIconButton's hover:bg-gray-1
   The pill is light in the official client regardless of the desktop theme --
   measured on media/image23.png, a white pill on a dark desktop, whose body
   samples to exactly rgb(249,249,249). So this does NOT follow the GTK theme. */
.plaud-overlay {
  background-color: #f9f9f9;
  border-radius: 5px;              /* rounded-[5px], not a full pill */
  box-shadow: 0 0 1px 0 rgba(0,0,0,0.30), 0 2px 8px 0 rgba(0,0,0,0.10);
}
/* SingleIconButton: "cursor-pointer rounded-[5px] p-1" + hover:bg-gray-1.
   No background at rest -- the official buttons are invisible until hovered. */
.plaud-iconbtn {
  background: transparent;
  border: none;
  border-radius: 5px;
  padding: 4px;                    /* p-1 */
  min-width: 0;
  min-height: 0;
}
.plaud-iconbtn:hover  { background: #ebebeb; }
.plaud-iconbtn:active { background: #e0e0e0; }
.plaud-sep { background: #ebebeb; min-height: 1px; }
"""


# --- official icon geometry -------------------------------------------------
#
# These are the real paths from the official client, copied verbatim rather
# than redrawn by eye. Sources, all under
# docs/../plaud-reference/asar_out/out-global-online/renderer/assets/:
#   LOGO_PATH   icon_plaud-B5Bd9a-B.js      (SvgIconPlaud)
#   PENCIL_PATH SoundWaveform-AjyrfJBS.js   (SvgIconHighlightsNote)
# Both are authored on a 20x20 viewBox, so _svg_path() scales by size/20.
#
# The logo is filled AND stroked with the same colour at stroke-width 0.833333.
# That is not an outline: the stroke fattens the fill, and dropping it renders a
# visibly thinner arch than the official client. The <g clipPath> and its 20x20
# clip rect in the source are a no-op (the clip is the whole viewBox) and are
# not reproduced.
LOGO_PATH = (
    "M9.98633 4.47852C11.6717 4.47868 13.217 5.56921 13.7266 7.16016L16.3584 15.377H14.8691"
    "L12.2158 6.51074C12.0882 6.08409 11.6955 5.79102 11.25 5.79102H8.72168C8.27621 5.79108"
    " 7.88347 6.08413 7.75586 6.51074L5.10352 15.377H3.61328L6.24512 7.16016C6.75466 5.5691"
    " 8.30087 4.47852 9.98633 4.47852ZM9.98633 9.70703C10.599 9.70723 11.0956 10.2037 11.0957"
    " 10.8164C11.0957 11.4291 10.5991 11.9256 9.98633 11.9258C9.37342 11.9258 8.87598 11.4292"
    " 8.87598 10.8164C8.87607 10.2036 9.37352 9.70703 9.98633 9.70703Z"
)
LOGO_STROKE = 0.833333

# Stroke-only, strokeLinejoin round, and no strokeWidth attribute at all -- so
# the SVG default of 1 applies. Two subpaths: the pen body, then the baseline.
PENCIL_PATH = (
    "M4.08184 16.1419L4.08203 13.2424L9.66797 3.56787L11.971 4.89754L6.40502 14.5381"
    "L4.08184 16.1419ZM4.08184 16.1419H16.5327"
)
PENCIL_STROKE = 1.0

# The two source toggles, from system_audio-A3qqXoi5.js (SvgIconMicrophone and
# SvgSystemAudio). Both fill-only, fill="currentColor", no stroke and no
# fillRule -- so they render with _svg_path()'s fill alone, unlike the logo.
#
# NOTE the viewBoxes differ: the mic is authored on 20x20 like everything else
# above, but SvgSystemAudio is on 24x24. _svg_path() scales by size/20, so the
# system icon is drawn through _draw_sysaudio's own 24ths scaling instead. Get
# that wrong and it renders at 83% inside its button, which reads as "smaller
# icon" rather than as a bug.
MIC_PATH = (
    "M5.1665 11C5.16677 13.669 7.33045 15.8338 9.99951 15.834C12.6687 15.834 14.8332"
    " 13.6692 14.8335 11V9.66699H15.8335V11C15.8332 14.2214 13.221 16.834 9.99951"
    " 16.834C6.77816 16.8338 4.16677 14.2213 4.1665 11V9.66699H5.1665V11ZM9.99951"
    " 3.16602C12.1165 3.16602 13.8333 4.88306 13.8335 7V11C13.8332 13.1169 12.1164"
    " 14.834 9.99951 14.834C7.88273 14.8338 6.16677 13.1168 6.1665 11V7C6.16668"
    " 4.88317 7.88268 3.16619 9.99951 3.16602ZM9.99951 4.16699C8.43496 4.16717"
    " 7.16668 5.43545 7.1665 7V11C7.16677 12.5645 8.43502 13.8338 9.99951"
    " 13.834C11.5642 13.834 12.8332 12.5646 12.8335 11V7C12.8333 5.43534 11.5642"
    " 4.16699 9.99951 4.16699Z"
)
# Two subpaths: the speaker bars, then the monitor body with its stand.
SYSAUDIO_PATH_BARS = (
    "M18.6992 11H19.6992V18H18.6992V11ZM16.6992 12.5H17.6992V16.5H16.6992V12.5Z"
    "M20.6992 12.5H21.6992V16.5H20.6992V12.5Z"
)
SYSAUDIO_PATH_BODY = (
    "M15.6992 15.1113L13.4131 15.1123C12.6947 15.1125 12.0232 15.4688 11.6201"
    " 16.0635L9.83105 18.7021H14.1826L12.748 16.5195L13.584 15.9707L15.5283"
    " 18.9277C15.6292 19.0812 15.6378 19.2777 15.5508 19.4395C15.4635 19.6012"
    " 15.2942 19.7021 15.1104 19.7021H8.88867C8.70359 19.7021 8.533 19.6 8.44629"
    " 19.4365C8.35976 19.2731 8.37083 19.075 8.47461 18.9219L10.792 15.502C11.3811"
    " 14.6328 12.3631 14.1125 13.4131 14.1123L15.6992 14.1113V15.1113ZM18.5332"
    " 4.29492C19.1775 4.29498 19.7002 4.81761 19.7002 5.46191V10H18.7002V5.46191"
    "C18.7002 5.3699 18.6252 5.29498 18.5332 5.29492L5.46582 5.2959C5.37378 5.2959"
    " 5.29883 5.37085 5.29883 5.46289V13.9453C5.29883 14.0374 5.37377 14.1123"
    " 5.46582 14.1123H9.44434V15.1123H5.46582C4.82149 15.1123 4.29883 14.5896"
    " 4.29883 13.9453V5.46289C4.29883 4.81859 4.82152 4.29594 5.46582 4.2959"
    "L18.5332 4.29492Z"
)

_RE_TOK = re.compile(r"[A-Za-z]|-?[0-9.]+")

INK = (0.0, 0.0, 0.0)  # --labels-primary, light theme


def _svg_path(cr, d, size, viewbox=20.0):
    """Replay one SVG path `d` onto a Cairo context, scaled from its viewBox.

    Supports exactly the commands these paths use -- M/L/H/V/C/Z, absolute
    only. Deliberately not a general SVG parser: the inputs are the fixed
    constants above, and anything wider would be code nobody calls.

    `viewbox` defaults to 20 because every icon here is authored on 20x20
    except SvgSystemAudio, which the official bundle authors on 24x24.
    """
    cr.save()
    cr.scale(size / viewbox, size / viewbox)
    toks = _RE_TOK.findall(d)
    i = 0
    cmd = None
    x = y = sx = sy = 0.0
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
        n = lambda k: float(toks[i + k])
        if cmd == "M":
            x, y = n(0), n(1); sx, sy = x, y
            cr.move_to(x, y); i += 2
        elif cmd == "L":
            x, y = n(0), n(1); cr.line_to(x, y); i += 2
        elif cmd == "H":
            x = n(0); cr.line_to(x, y); i += 1
        elif cmd == "V":
            y = n(0); cr.line_to(x, y); i += 1
        elif cmd == "C":
            cr.curve_to(n(0), n(1), n(2), n(3), n(4), n(5))
            x, y = n(4), n(5); i += 6
        elif cmd in ("Z", "z"):
            cr.close_path(); x, y = sx, sy
        else:
            raise ValueError("unsupported SVG command %r" % cmd)
    cr.restore()


def _draw_logo(_w, cr, size):
    """SvgIconPlaud: fill, then stroke the same path in the same colour."""
    cr.set_source_rgb(*INK)
    _svg_path(cr, LOGO_PATH, size)
    cr.fill_preserve()
    cr.set_line_width(LOGO_STROKE * size / 20.0)
    cr.stroke()


# The three glyphs below have no counterpart in the official *mini* window --
# they belong to controls only our pill carries. They are drawn rather than set
# as emoji labels because an emoji font renders in its own colour and ignores
# INK, which put a bright orange pause button in a monochrome pill (measured).
# Shapes follow the official note panel's own bottom bar, docs/plaud-desktop/
# media/image24.png: two bars for pause, a triangle for resume, corner marks
# for crop. All are authored on the same 20x20 grid as the official icons.


def _draw_pause(_w, cr, size):
    cr.set_source_rgb(*INK)
    k = size / 20.0
    for x in (6.5, 11.5):
        cr.rectangle(x * k, 5 * k, 2 * k, 10 * k)
    cr.fill()


def _draw_play(_w, cr, size):
    cr.set_source_rgb(*INK)
    k = size / 20.0
    cr.move_to(7 * k, 4.5 * k)
    cr.line_to(15 * k, 10 * k)
    cr.line_to(7 * k, 15.5 * k)
    cr.close_path()
    cr.fill()


def _draw_crop(_w, cr, size):
    cr.set_source_rgb(*INK)
    k = size / 20.0
    cr.set_line_width(1.4 * k)
    cr.set_line_join(cairo.LINE_JOIN_MITER)
    # two overlapping L's, the crop-mark shape the official panel uses
    cr.move_to(6 * k, 2.5 * k)
    cr.line_to(6 * k, 14 * k)
    cr.line_to(17.5 * k, 14 * k)
    cr.stroke()
    cr.move_to(2.5 * k, 6 * k)
    cr.line_to(14 * k, 6 * k)
    cr.line_to(14 * k, 17.5 * k)
    cr.stroke()


def _draw_kebab(_w, cr, size):
    """SvgIconMore: three filled dots, the CancelRecordingMenu trigger
    (OFFICIAL-UI-SPEC.md §6.1). Vertical, matching the pill's own column."""
    cr.set_source_rgb(*INK)
    k = size / 20.0
    r = 1.3 * k
    for cy in (5, 10, 15):
        cr.new_sub_path()
        cr.arc(10 * k, cy * k, r, 0, 2 * math.pi)
        cr.fill()


def _draw_pencil(_w, cr, size):
    """SvgIconHighlightsNote: stroke only, round joins, width 1 at 20x20."""
    cr.set_source_rgb(*INK)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_line_width(PENCIL_STROKE * size / 20.0)
    _svg_path(cr, PENCIL_PATH, size)
    cr.stroke()


# The two source glyphs. `on` is carried on the widget rather than passed,
# because GTK's "draw" signal gives the handler no room for extra arguments and
# the alternative -- rebuilding the DrawingArea on every toggle -- would drop
# the widget the caller is holding.
#
# Off is drawn as the SAME glyph at reduced alpha, not as a different
# "muted" icon. That is forced by the evidence, not chosen for convenience:
# the official bundle ships exactly one mic icon and one system-audio icon,
# with no slashed or muted variant anywhere (grepped for MicOff/MicMute/
# VolumeOff/SpeakerMute across renderer/assets: zero hits). Inventing a
# slash here would be drawing something the real client does not have.
OFF_ALPHA = 0.28   # --labels-quaternary against #f9f9f9, close enough to read
                   # as "off" without reading as "disabled"


def _src_alpha(w):
    return 1.0 if getattr(w, "plaud_on", True) else OFF_ALPHA


def _draw_mic(w, cr, size):
    """SvgIconMicrophone: fill only, currentColor, 20x20 viewBox."""
    cr.set_source_rgba(*INK, _src_alpha(w))
    _svg_path(cr, MIC_PATH, size)
    cr.fill()


def _draw_sysaudio(w, cr, size):
    """SvgSystemAudio: two fill-only subpaths, on a 24x24 viewBox."""
    cr.set_source_rgba(*INK, _src_alpha(w))
    for d in (SYSAUDIO_PATH_BARS, SYSAUDIO_PATH_BODY):
        _svg_path(cr, d, size, viewbox=24.0)
        cr.fill()


class _Waveform(Gtk.DrawingArea):
    """The official five-bar level meter, redrawn with Cairo.

    SoundWaveform-AjyrfJBS.js renders five 2px `rounded-full` bars of
    --labels-primary in a 24x20 box, scaled vertically about their centre. The
    per-bar wobble constants below are that file's ANIMATION_CONFIGS verbatim
    (amplitude, duration in seconds, delay) -- the middle bar swings widest,
    which is what makes the row read as a level meter rather than a row of
    dashes.

    What is NOT reproduced is the volume input. The official bars are driven by
    a live `recordingVolume` atom; audio.py hands ffmpeg the capture and never
    reports a level, and CLAUDE.md's layer rule keeps overlay.py out of the
    audio path. So this animates at the client's own idle amplitude, which is
    honest about what it knows: it says "recording is live", not "the room is
    this loud". Wiring a real level would mean a new signal out of audio.py and
    is deliberately left out of a visual change.
    """
    BARS = [(0.07, 1.0, 0.0), (0.13, 0.9, 0.05), (0.2, 0.8, 0.0),
            (0.11, 0.95, 0.08), (0.06, 1.05, 0.03)]
    BAR_W = 2                # w-0.5
    # --waveform-volume, i.e. the already-mapped output of the component's
    # mapVolume(): MIN_HEIGHT + raw**0.4 * (1 - MIN_HEIGHT), MIN_HEIGHT 0.15.
    # The source's initial value is 0.20, but that is the pre-audio placeholder
    # -- a live pill sits higher. Calibrated instead against the official render
    # itself: the five bars in docs/plaud-desktop/media/image23.png measure 2px
    # wide, 5px apart, and 6 to 12px tall in the 20px box (measured by
    # thresholding the cropped row). With the wobble range below spanning
    # 0.57..1.25, a volume of 0.52 reproduces exactly that 6..12 spread.
    IDLE_VOLUME = 0.52

    def __init__(self, width, height):
        super().__init__()
        self.set_size_request(width, height)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self._w, self._h = width, height
        self._t = 0.0
        self._active = True
        self.connect("draw", self._draw)
        # 20 fps is enough for a 24px meter and costs far less than the
        # official client's requestAnimationFrame. The source is removed on
        # destroy for the same reason the tick is -- see _remove_tick.
        self._anim = GLib.timeout_add(50, self._step)
        self.connect("destroy", self._stop_anim)

    def set_active(self, on):
        self._active = on

    def _stop_anim(self, *_):
        if self._anim is not None:
            GLib.source_remove(self._anim)
            self._anim = None

    def _step(self):
        if self._active:
            self._t += 0.05
            self.queue_draw()
        return True

    def _draw(self, _w, cr):
        cr.set_source_rgb(*INK)
        # justify-around: each bar gets an equal margin either side, so the
        # gaps at the two edges are HALF the gaps between neighbours. Splitting
        # the free space evenly six ways instead would pull the outer bars
        # ~1px inwards (measured against the official row).
        free = self._w - len(self.BARS) * self.BAR_W
        slot = free / len(self.BARS)          # the margin pair each bar owns
        gap = slot / 2.0                      # half of it on each side
        cy = self._h / 2.0
        for i, (amp, dur, delay) in enumerate(self.BARS):
            if self._active:
                # The keyframes swing --wave-scale between 0.5+amp and 1.05+amp
                # over `dur` seconds; a cosine reproduces that ease-in-out.
                phase = (self._t - delay) / dur * 2 * math.pi
                lo, hi = 0.5 + amp, 1.05 + amp
                scale = lo + (hi - lo) * (0.5 - 0.5 * math.cos(phase))
            else:
                scale = 1.0  # frozen flat, as below the client's low-volume cut
            bh = max(self.BAR_W, self._h * self.IDLE_VOLUME * scale)
            x = gap + i * (self.BAR_W + slot)
            r = self.BAR_W / 2.0  # rounded-full
            y = cy - bh / 2.0
            cr.new_sub_path()
            cr.arc(x + r, y + r, r, math.pi, 0)
            cr.arc(x + r, y + bh - r, r, 0, math.pi)
            cr.close_path()
        cr.fill()


class Overlay(Gtk.Window):
    def __init__(self, mode="system", name=None, on_stop=None, on_state_change=None,
                 on_discard=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.on_stop = on_stop
        # Optional, same shape as on_stop: the tray has no window of its own to
        # read rec.state from, so pause/resume (the only state change besides
        # start/stop, both already visible to the caller through on_stop and the
        # return of start_session()) is reported here. None for every caller
        # that has no icon to update, e.g. `python3 -m plaud_linux.overlay`.
        self.on_state_change = on_state_change
        # CAN-315 discard flow: same shape as on_stop, but the session ends
        # WITHOUT an upload -- see _do_discard. None falls through to
        # Gtk.main_quit, matching on_stop's own default.
        self.on_discard = on_discard
        self.expanded = False
        self._shot_pending = False
        self._shot_watch = None
        self._shot_proc = None
        self._shot_out = (None, 0)
        self._freeze_proc = None
        self._freeze_start_id = None
        self._freeze_deadline_id = None
        self._freeze_attempts = 0
        self._freeze_ready_count = 0
        # True once on_stop() has been handed rec.screenshots: past that point a
        # late capture can no longer reach the upload, so attaching it would
        # promise the user a mark that is never sent.
        self._shots_taken = False
        # True once _do_stop() OR _do_discard() has begun. Shared between the
        # two terminal actions, not just _do_stop's own callers: a stop and a
        # discard racing each other is the same double-ending defect _do_stop's
        # own comment documents, just with a second door into it.
        self._stopping = False

        # Guard against re-entering _on_src_toggled while it is itself setting
        # a toggle's state. set_active() emits "toggled" synchronously, so the
        # insensitivity sync below would otherwise recurse through the handler.
        self._syncing_src = False

        self.rec = audio.Recorder(mode=mode, name=name)
        try:
            from . import settings
        except ImportError:
            import settings
        selected_mic = settings.get_mic_device()
        self.rec.mic_device = (None if selected_mic in (settings.MIC_AUTOMATIC, settings.MIC_OFF)
                               else selected_mic)
        self.shots_dir = self.rec.final_path.parent / SHOTS_DIRNAME / self.rec.session
        self.shots_dir.mkdir(parents=True, exist_ok=True)

        # --- window chrome: frameless, on-top, sticky across workspaces ---
        #
        # Choose placement before realization. A successful Hyprland rule makes
        # the pill movable; otherwise layer-shell retains a visible indicator.
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_accept_focus(False)
        if not self._init_movable():
            self._init_layer_shell()
        self.set_app_paintable(True)
        screen = self.get_screen()
        vis = screen.get_rgba_visual()
        if vis:
            self.set_visual(vis)

        self.mode = mode
        self._apply_css()
        self._build_ui()

        # Placement is established before capture starts: a compositor rule
        # for the movable pill, or anchor/margins for the layer-shell fallback.

        # start recording immediately (like Plaud: it just starts capturing)
        self.rec.start()
        self.started = datetime.now()

        self._tick_id = GLib.timeout_add(500, self._tick)
        # The tick outlives the window unless it is removed explicitly: destroy()
        # does not touch GLib sources, so every finished session would leave a
        # timer firing 2x/s on a dead widget, holding this Overlay and its
        # Recorder alive for the life of the process.
        self.connect("destroy", self._remove_tick)

    # Anchor offsets, in px from the monitor's working area (the compositor
    # subtracts any bar's exclusive zone before applying these).
    MARGIN_TOP = 48
    MARGIN_RIGHT = 40

    def _init_movable(self):
        """Register placement before map, or retain the working layer fallback.

        no_focus excludes floating windows from Hyprland's pointer hit test.
        Suppress initial/hover focus instead; deliberate clicks remain usable.
        Match only this title: panel.py changes the process-wide prgname, so
        changing/matching that identity would couple the pill to other windows.
        Version the compositor guard whenever the rule changes; an old global
        must never silently retain a previous input policy after an upgrade.
        """
        if not HYPRLAND:
            return False
        title = "plaud-recording-pill"
        lua = (
            'if not PLAUD_PILL_INPUT_V1 then\n'
            '  PLAUD_PILL_INPUT_V1 = hl.window_rule({name="plaud-pill-input-v1",\n'
            '    match={title="^plaud-recording-pill$"},\n'
            '    float=true, pin=true, no_focus=false,\n'
            '    no_initial_focus=true, no_follow_mouse=true,\n'
            '    no_dim=true, no_shadow=true, border_size=0, rounding=0,\n'
            '    tag="-default-opacity", opacity="1 1",\n'
            f'    move="(monitor_w-window_w-{self.MARGIN_RIGHT}) {self.MARGIN_TOP}"}})\n'
            'end\n'
            'print("PLAUD_PILL_READY")'
        )
        try:
            result = subprocess.run(["hyprctl", "repl", lua], capture_output=True,
                                    text=True, timeout=2)
            if result.returncode != 0 or "PLAUD_PILL_READY" not in result.stdout.splitlines():
                return False
        except (OSError, subprocess.SubprocessError):
            return False
        self.set_title(title)
        return True

    def _init_layer_shell(self):
        """Make this window a wlr-layer-shell surface: placed, on top, sticky."""
        GtkLayerShell.init_for_window(self)
        # Namespaced so the surface is identifiable in `hyprctl layers` and
        # addressable by a compositor layerrule.
        GtkLayerShell.set_namespace(self, "plaud-overlay")
        # OVERLAY, not TOP: TOP sits below fullscreen surfaces, so the pill
        # would vanish behind exactly the fullscreen video call this app exists
        # to record. OVERLAY is the layer panels use for things that must stay
        # visible, which is the recording indicator's whole purpose.
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, self.MARGIN_TOP)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, self.MARGIN_RIGHT)
        # The layer-shell counterpart of set_accept_focus(False). NONE means the
        # surface never takes keyboard focus, so the pill cannot steal typing
        # from the meeting it is recording. The note dialog is a normal
        # toplevel, not a layer surface, so it still focuses its entry.
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        # No exclusive zone: the pill floats *over* other windows rather than
        # reserving a strip and shoving tiled windows aside for the session.
        GtkLayerShell.set_exclusive_zone(self, 0)
        # set_monitor() is deliberately not called: left unset, the compositor
        # picks the output, and on Hyprland that is the focused one AT MAP TIME.
        # It is map-time only, and the pill does NOT follow focus afterwards --
        # measured with the pill up on HDMI-A-1: 1 surface on HDMI-A-1, 0 on
        # DP-2, and it stayed there. On a two-monitor desk the indicator can
        # therefore sit on a screen the user has since looked away from.
        #
        # Following focus was measured and rejected, not overlooked.
        # set_monitor() on a live surface does re-anchor correctly (HDMI-A-1 ->
        # DP-2, landing at the right top-right corner), but it costs a full
        # unmap/remap every time: 3 set_monitor() calls produced 3 unmaps and 3
        # maps. That is a visible flicker of the recording indicator on every
        # monitor switch, and wlr-layer-shell offers no cheaper re-anchor.
        # Worse, GTK sees no "focused monitor changed" event on Hyprland, so
        # following would mean polling `hyprctl` from inside overlay.py -- a
        # subprocess loop for the life of every recording, in the one module
        # CLAUDE.md says must stay a dumb pill. A still indicator on the wrong
        # monitor beats a flickering one on the right monitor; if this becomes
        # a real annoyance, the fix is a user-pinned monitor preference, not a
        # poll. Pinning a fixed monitor here is also NOT the fallback: it would
        # need the multi-monitor arithmetic that screen.get_width() got wrong.

    def _apply_css(self):
        # Attached to the *screen*, which outlives every Overlay -- so a second
        # session would stack a second identical provider that nothing ever
        # removes. CSS is a module constant, so one provider per screen is both
        # sufficient and the only way to keep this bounded once the tray
        # (CAN-310) starts building overlays repeatedly in one process.
        screen = self.get_screen()
        key = hash(screen)
        if key in _css_screens:
            return
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _css_screens.add(key)

    # --- official mini-window geometry, in px ------------------------------
    #
    # The window is 52x160 and the *body* is 36x144: windowService-7zXhwgFy.js
    # defines MINI_WINDOW_PADDING = 8 and then WIDTH = 36 + 2*8, HEIGHT =
    # 144 + 2*8. Those 8px are the transparent margin the shadow is drawn into
    # (#root carries m-2), not part of the visible pill. Confirmed against the
    # official screenshot: docs/plaud-desktop/media/image23.png measures the
    # white body at exactly 36px wide.
    BODY_W = 36
    BODY_H = 144
    PAD_X = 2            # px-0.5
    PAD_Y = 4            # py-1
    GAP = 4              # gap-1, at both nesting levels
    LOGO = 32            # h-8 w-8
    WAVE_ROW_H = 24      # the h-6 row
    WAVE_H = 20          # the h-5 waveform box inside it
    WAVE_W = 24          # w-6
    SEP_W = 24           # !w-6
    STOP = 12            # h-3 w-3
    PENCIL = 20          # h-5 w-5

    def _build_ui(self):
        # A vertical strip, not a horizontal bar. The official mini window is a
        # single column: logo, waveform, then a group of [separator, stop,
        # pencil]. Both levels use gap-1.
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.GAP)
        self.root.get_style_context().add_class("plaud-overlay")
        self.root.set_margin_top(self.PAD_Y)
        self.root.set_margin_bottom(self.PAD_Y)
        self.root.set_margin_start(self.PAD_X)
        self.root.set_margin_end(self.PAD_X)
        # Width is fixed at the official 36px; height is left to the content.
        #
        # BODY_H (144) is deliberately NOT forced here. The stack's own natural
        # height already reproduces the official spacing exactly -- measured
        # against docs/plaud-desktop/media/image23.png, element heights come out
        # 18/12/12/14 and the gaps between them 17/23/19, identical on both. But
        # the collapsed `controls` box still costs one `spacing` gap in the box,
        # so pinning the height to 144 parked that slack at the bottom (7px
        # above the logo against 22px below the pencil, where the official is a
        # symmetric 11/11). Sizing to content keeps the padding balanced, and
        # the resulting body measures within a pixel of the official 137px the
        # screenshot actually renders.
        self.root.set_size_request(self.BODY_W, -1)
        self.add(self.root)

        # 1. logo (h-8 w-8)
        self.logo = self._mk_icon(self.LOGO, _draw_logo)
        self.root.pack_start(self.logo, False, False, 0)

        # 2. waveform row (h-6, full width)
        self.wave = _Waveform(self.WAVE_W, self.WAVE_H)
        wave_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        wave_row.set_size_request(-1, self.WAVE_ROW_H)
        wave_row.set_center_widget(self.wave)
        self.root.pack_start(wave_row, False, False, 0)

        # 3. inner group: separator, stop, pencil -- gap-1 again
        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.GAP)
        sep = Gtk.Box()
        sep.get_style_context().add_class("plaud-sep")
        sep.set_size_request(self.SEP_W, 1)
        sep.set_halign(Gtk.Align.CENTER)
        group.pack_start(sep, False, False, 0)

        # The stop control is a plain filled square -- not a circle, not
        # rounded, and not red. In the official client it is a 12x12 block of
        # --labels-primary with m-1, inside a rounded-[5px] p-1 button whose
        # only background is the hover state.
        self.btn_stop = self._mk_iconbtn(
            self._mk_square(self.STOP), "Parar e enviar", self._on_stop)
        group.pack_start(self.btn_stop, False, False, 0)

        self.btn_note = self._mk_iconbtn(
            self._mk_icon(self.PENCIL, _draw_pencil), "Anotar", self._on_note)
        group.pack_start(self.btn_note, False, False, 0)
        self.root.pack_start(group, False, False, 0)

        # 4. our own controls, below the official stack and hidden by default.
        #
        # Pause and screenshot have no counterpart in the official mini window
        # -- there the pencil opens a separate note panel that carries pause,
        # timer and crop. We have no second window, so the pill is the only
        # place those controls can live. Keeping them collapsed means the
        # resting pill is exactly the official 36x144 strip, and the extra
        # column only appears when the user asks for it.
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.GAP)
        sep2 = Gtk.Box()
        sep2.get_style_context().add_class("plaud-sep")
        sep2.set_size_request(self.SEP_W, 1)
        sep2.set_halign(Gtk.Align.CENTER)
        self.controls.pack_start(sep2, False, False, 0)
        self._pause_draw = _draw_pause
        self.pause_icon = self._mk_icon(
            self.PENCIL, lambda w, cr, size: self._pause_draw(w, cr, size))
        self.btn_pause = self._mk_iconbtn(self.pause_icon, "Pausar", self._on_pause)
        self.btn_shot = self._mk_iconbtn(
            self._mk_icon(self.PENCIL, _draw_crop), "Capturar tela", self._on_shot)
        self.controls.pack_start(self.btn_pause, False, False, 0)
        self.controls.pack_start(self.btn_shot, False, False, 0)

        # The kebab (CancelRecordingMenu trigger, OFFICIAL-UI-SPEC.md §6.1).
        # Lives in the collapsed column for the same reason pause/shot do: the
        # resting pill has no room for a control the official mini window
        # doesn't carry either (there it's on the separate recording-main
        # window's RecordControl bar, which we don't have).
        self.btn_kebab = self._mk_iconbtn(
            self._mk_icon(self.PENCIL, _draw_kebab), "Mais opções", self._on_kebab)
        self.controls.pack_start(self.btn_kebab, False, False, 0)

        # 5. the two source toggles.
        #
        # They live here, in the expanded column, for the same reason pause and
        # screenshot do: the resting pill must stay the official 36x144 strip,
        # which has room for neither. This is a real divergence and worth
        # naming -- in the official client the equivalent switches are in
        # Preferencias > Gravacao (media/image6.png: "Microfone" and "Audio do
        # sistema", the latter a pill Switch), and the recording surface
        # carries no source control at all.
        #
        # Putting them one click from the recording instead is the point of
        # CAN-315: the complaint was that choosing sources meant answering a
        # modal BEFORE any audio existed. A settings window we do not have
        # would be no better. The recording starts immediately, and the
        # channels are changeable while it runs -- which is exactly what the
        # official client does, even though it hangs the controls elsewhere
        # (recordingService-Mile2cgU.js subscribes to both atoms live and
        # begins/pauses each channel mid-recording, without restarting).
        sep3 = Gtk.Box()
        sep3.get_style_context().add_class("plaud-sep")
        sep3.set_size_request(self.SEP_W, 1)
        sep3.set_halign(Gtk.Align.CENTER)
        self.controls.pack_start(sep3, False, False, 0)
        self.btn_sys = self._mk_srcbtn(_draw_sysaudio, self.mode != "mic")
        self.btn_mic = self._mk_srcbtn(_draw_mic, self.mode in ("mic", "meeting"))
        self.controls.pack_start(self.btn_sys, False, False, 0)
        self.controls.pack_start(self.btn_mic, False, False, 0)
        self._sync_src_ui()

        self.root.pack_start(self.controls, False, False, 0)

        # The expand affordance is the pill itself: the official client has no
        # "..." button, and adding one would put a control in the 36px column
        # that the real app does not have. A left click toggles instead --
        # which is also where the official client puts its own click handler.
        self.connect("button-press-event", self._toggle_expand)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)

        self.show_all()
        self.controls.hide()

    def _mk_icon(self, size, draw):
        """A DrawingArea that replays one official SVG path at `size` px."""
        a = Gtk.DrawingArea()
        a.set_size_request(size, size)
        a.set_halign(Gtk.Align.CENTER)
        a.connect("draw", draw, size)
        return a

    def _mk_square(self, size):
        """The stop glyph: a hard-edged filled square, m-1 all round."""
        a = Gtk.DrawingArea()
        a.set_size_request(size, size)
        a.set_halign(Gtk.Align.CENTER)
        a.set_margin_top(4)      # m-1
        a.set_margin_bottom(4)
        a.set_margin_start(4)
        a.set_margin_end(4)

        def draw(_w, cr):
            cr.set_source_rgb(*INK)
            cr.rectangle(0, 0, size, size)
            cr.fill()
        a.connect("draw", draw)
        return a


    def _set_pause_icon(self, draw):
        """Repoint the pause button at a different glyph.

        The handler is connected once and dispatches through this attribute, so
        swapping pause/play is an assignment rather than a disconnect+reconnect
        (which needs the exact original callable to unhook and is easy to get
        subtly wrong).
        """
        self._pause_draw = draw
        self.pause_icon.queue_draw()

    def _mk_iconbtn(self, child, tooltip, cb):
        """SingleIconButton: rounded-[5px] p-1, transparent until hovered."""
        b = Gtk.Button()
        b.add(child)
        b.get_style_context().add_class("plaud-iconbtn")
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_halign(Gtk.Align.CENTER)
        b.set_tooltip_text(tooltip)
        b.connect("clicked", cb)
        return b

    def _mk_srcbtn(self, draw, active):
        """One source toggle: the official glyph, dimmed when the source is off.

        A Gtk.ToggleButton rather than a Gtk.Switch. The official client does
        use a Switch for these (index-CrfkTyBe.js renders
        `<Switch checked={enableSystemAudioRecord} .../>`), but a Switch is a
        wide horizontal control and this column is 36px -- it would be the one
        widget in the pill that cannot keep the measured geometry. Same glyph,
        same button chrome as every other control here, state carried by the
        icon's own alpha.
        """
        icon = self._mk_icon(self.PENCIL, draw)
        icon.plaud_on = active
        b = Gtk.ToggleButton()
        b.add(icon)
        b.get_style_context().add_class("plaud-iconbtn")
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_halign(Gtk.Align.CENTER)
        b.set_active(active)
        b.plaud_icon = icon
        b.connect("toggled", self._on_src_toggled)
        return b

    def _sync_src_ui(self):
        """Repaint both toggles and lock whichever one is the last still on.

        The lock is the whole guard: with both off there is no ffmpeg command
        to run, so the alternatives are a capture that stops while the UI still
        says "recording" or a control that reports a state the engine refused.
        Making the last one unclickable is the only option that never lies.

        We DIVERGE from the official client here, and it is not an oversight.
        It permits both off: recordingService-Mile2cgU.js enters a "full
        silence" state (detectFullSilence), keeps the session and its clock
        running, and on re-enable pads the hole with recorder.fillSilence(gapMs)
        so the file stays in sync with the timer. That depends on a native
        recorder that can be told to emit silence. Our engine is ffmpeg
        segments concatenated with -c copy; it has no way to manufacture a gap,
        so reproducing the behaviour would mean an audio file whose length no
        longer matches the elapsed time every screenshot and note is stamped
        against. Blocking the last toggle keeps those honest.

        Also worth recording, because it makes the two controls look more alike
        here than they are in the real client: officially these settings are
        NOT symmetric. System audio is a boolean Switch (default on); the
        microphone is a device SELECTOR (micDeviceStateAtom, default "smart" =
        "Automatic") whose options include "Off". Our engine resolves exactly
        one microphone -- default_source() -- so a selector would list a single
        device plus "Off", which is a two-state control wearing a combobox.
        The mic toggle here is that selector collapsed to its only two
        reachable values; it is not a claim that the official mic control is a
        switch. Grow it into a real selector if this ever gains a settings
        window and more than one input device.
        """
        for b in (self.btn_sys, self.btn_mic):
            b.plaud_icon.plaud_on = b.get_active()
            b.plaud_icon.queue_draw()
        only_sys = self.btn_sys.get_active() and not self.btn_mic.get_active()
        only_mic = self.btn_mic.get_active() and not self.btn_sys.get_active()
        self.btn_sys.set_sensitive(not only_sys)
        self.btn_mic.set_sensitive(not only_mic)
        self.btn_sys.set_tooltip_text(
            "Áudio do sistema" if not only_sys
            else "Áudio do sistema — a última fonte não pode ser desligada")
        self.btn_mic.set_tooltip_text(
            "Microfone" if not only_mic
            else "Microfone — a última fonte não pode ser desligada")

    def _on_src_toggled(self, btn):
        if self._syncing_src:
            return
        system, mic = self.btn_sys.get_active(), self.btn_mic.get_active()
        self._syncing_src = True
        try:
            if not self.rec.set_sources(system, mic):
                # Refused -- only "neither" can do that, and _sync_src_ui keeps
                # it unreachable. Put the button back rather than leaving it
                # showing a state the engine is not in.
                btn.set_active(not btn.get_active())
                return
            self._sync_src_ui()
        finally:
            self._syncing_src = False
        self.mode = self.rec.mode
        # The mic can be asked for and not be there. Same fact main.py warns
        # about at session start, and the same reason: _resolve_src() drops to
        # the system-only branch and records on, so the user gets a recording
        # with none of their own voice and no error anywhere.
        if mic and not (getattr(self.rec, "_last_src", None)
                        or (None, None, None, None))[2]:
            self._notify("Plaud",
                         "⚠️ Microfone ligado, mas nenhum foi encontrado — "
                         "gravando sem a sua voz.")

    def _toggle_expand(self, *_):
        self.expanded = not self.expanded
        if self.expanded:
            self.controls.show_all()
        else:
            self.controls.hide()
        self.resize(1, 1)  # shrink to content
        return False

    def _tick(self):
        # No timer label to update: the official mini window renders no text at
        # all (verified in the RecordingMini component -- it consumes only
        # recordingVolume, recordingId and two guide flags). The elapsed time
        # lives in the note panel there, and in our notifications here.
        #
        # What the tick drives instead is the waveform, which is the official
        # pill's own "still recording" signal. Paused freezes the bars flat,
        # exactly as the client does below its low-volume threshold.
        self.wave.set_active(self.rec.state == "recording")
        return True

    def _remove_tick(self, *_):
        self._release_freeze()
        self._shot_pending = False
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None
        # Same reasoning for a screenshot still in flight: the watch holds self,
        # and a tool that never exits would keep it alive forever.
        if self._shot_watch is not None:
            GLib.source_remove(self._shot_watch)
            self._shot_watch = None
            self._shot_pending = False
            # Removing the watch only stops us listening; it does not stop the
            # tool. `flameshot gui` blocks holding a fullscreen grab until the
            # user acts, so without this the app can exit and leave an orphan
            # owning the screen with nothing left to dismiss it. The old
            # subprocess.run(timeout=120) killed the child on expiry; spawning
            # removed that guarantee, so take it back here.
            if self._shot_proc is not None:
                try:
                    self._shot_proc.kill()
                except Exception:
                    pass
                # slurp's stdout and stderr are pipes now, and nothing will
                # read them: the watch that would have is the one just removed.
                for pipe in (self._shot_proc.stdout, self._shot_proc.stderr):
                    try:
                        pipe.close()
                    except Exception:
                        pass
                self._shot_proc = None
            # The tool may already have exited and written the file, with its
            # callback merely queued -- removing the watch would then throw away
            # a capture that actually succeeded. The file is the evidence, here
            # as everywhere else in this path, so check it before discarding.
            #
            # Attaching is only safe while the list can still reach the upload:
            # _do_stop runs destroy() -- and so this -- before on_stop() hands
            # rec.screenshots to the upload thread, so a shot recovered here is
            # still sent. `_shots_taken` is what makes that a checked fact
            # rather than a property of the call order: once on_stop() has read
            # the list, attaching would tell the user "salvo" about a mark that
            # is never uploaded, which is the failure _shot_done already
            # refuses. Both paths now ask the same question.
            out, elapsed = self._shot_out
            if out is not None and out.exists() and out.stat().st_size > 0 and not self._shots_taken:
                self.rec.add_screenshot(out)
            else:
                self._notify("Plaud", "Screenshot descartado — a gravação já foi encerrada")

    def _on_pause(self, *_):
        if self.rec.state == "recording":
            self.rec.pause()
            self._set_pause_icon(_draw_play)
            self.btn_pause.set_tooltip_text("Retomar")
            if self.on_state_change:
                self.on_state_change(self.rec.state)
        elif self.rec.state == "paused":
            self.rec.resume()
            self._set_pause_icon(_draw_pause)
            self.btn_pause.set_tooltip_text("Pausar")
            if self.on_state_change:
                self.on_state_change(self.rec.state)

    def _on_note(self, *_):
        """The pencil. Opens the highlights panel -- that is its entire job.

        `toggleWindowDisplay(WindowEnum.RecordingHighlight)` at
        renderer/assets/index-DotEOTW4.js:288-300 is the official handler, and
        it opens a window; it does not itself annotate. This replaced a plain
        one-entry Gtk.Dialog, whose save-with-timestamp behaviour now lives in
        Panel._on_note_activate -- including sampling the clock before the text
        is read, for the same reason the dialog did.

        Imported here rather than at module scope because panel.py imports this
        module back, the same cycle main.py breaks the same way for tray.py.
        """
        try:
            from . import panel
        except ImportError:
            import panel
        panel.open_for(self)

    def _start_flag_preview(self, flag):
        try:
            from . import highlights
        except ImportError:
            import highlights
        highlights.start(self.rec, flag)

    def _on_shot(self, *_):
        # One capture at a time. Two presses in the same second would otherwise
        # build the same `out` path and race: the cancelled one would find the
        # other's file, report success and attach the same image twice. While it
        # blocked, the loop could not deliver a second press at all.
        if self._shot_pending or self.rec.state != "recording":
            return
        ts = datetime.now().strftime("%H%M%S")
        elapsed = self.rec.elapsed()
        out = self.shots_dir / f"shot_{elapsed:06d}_{ts}.png"
        self._shot_pending = True
        self._shot_out = (out, elapsed)
        self._freeze_attempts = 0
        self._freeze_ready_count = 0
        try:
            self._freeze_deadline_id = GLib.timeout_add(120000, self._capture_timeout)
        except Exception:
            self._shot_fail()
            return
        # Freeze the lesson BEFORE the user spends time selecting a region.
        # Audio and GTK continue running; only the displayed snapshot is frozen.
        # Wait for this child's surfaces on every output, including compositor
        # fade-in. A fixed 200ms alone captured a changing frame on one monitor.
        try:
            self._freeze_proc = subprocess.Popen(
                ["hyprpicker", "--render-inactive", "--no-zoom", "--disable-preview", "--quiet"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self._notify("Plaud", "Captura sem congelamento — instale hyprpicker")
            self._begin_region()
            return
        try:
            self._freeze_start_id = GLib.timeout_add(200, self._begin_region)
        except Exception:
            self._shot_fail()

    def _begin_region(self):
        if not self._shot_pending or self.rec.state == "stopped":
            self._freeze_start_id = None
            self._release_freeze()
            self._shot_pending = False
            return False
        if self._freeze_proc is not None and self._freeze_proc.poll() is not None:
            self._freeze_start_id = None
            self._shot_fail()
            return False
        if self._freeze_proc is not None:
            self._freeze_attempts += 1
            self._freeze_ready_count = self._freeze_ready_count + 1 if self._freeze_ready() else 0
            # Layer metadata can reach alpha=1 one frame before it is rendered.
            # Require readiness across two ticks before giving slurp the grab.
            if self._freeze_ready_count < 2 and self._freeze_attempts < 10:
                return True
            if self._freeze_ready_count < 2:
                self._freeze_start_id = None
                self._shot_fail()
                return False
        self._freeze_start_id = None
        out, elapsed = self._shot_out
        self._shot_next([["slurp"], []], out, elapsed)
        return False

    def _freeze_ready(self):
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            # Other wlroots compositors cannot report Hyprland layer readiness.
            return self._freeze_attempts >= 2
        try:
            result = subprocess.run(["hyprctl", "layers", "-j"], capture_output=True,
                                    text=True, check=True, timeout=1)
            outputs = json.loads(result.stdout)
            return bool(outputs) and all(any(
                layer.get("pid") == self._freeze_proc.pid
                and layer.get("namespace") == "hyprpicker"
                and layer.get("alpha", 0) >= 1
                and layer.get("w", 0) > 0 and layer.get("h", 0) > 0
                for layers in output.get("levels", {}).values() for layer in layers
            ) for output in outputs.values())
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            return False

    def _release_freeze(self):
        for attr in ("_freeze_start_id", "_freeze_deadline_id"):
            source = getattr(self, attr, None)
            setattr(self, attr, None)
            if source is not None:
                GLib.source_remove(source)
        proc, self._freeze_proc = getattr(self, "_freeze_proc", None), None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=0.2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass

    def _cancel_selector(self):
        if self._shot_watch is not None:
            GLib.source_remove(self._shot_watch)
            self._shot_watch = None
        proc, self._shot_proc = self._shot_proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=0.2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()

    def _capture_timeout(self):
        self._freeze_deadline_id = None
        self._cancel_selector()
        self._shot_fail()
        return False

    def _shot_fail(self):
        self._release_freeze()
        self._shot_pending = False
        self._notify("Plaud", "Falha ao capturar screenshot")

    def _shot_next(self, cmds, out, elapsed):
        """Try the next screenshot tool; on failure, chain to the one after it."""
        if not cmds:
            self._shot_fail()
            return
        # The empty command is the full-screen fallback: there is no region to
        # pick, so no child to spawn and watch -- run grim straight away.
        if not cmds[0]:
            self._grim(None, out, elapsed, cmds)
            return
        try:
            # stdout is a pipe, not DEVNULL: slurp reports the chosen region
            # there, and that geometry is what grim is handed next. stderr is a
            # pipe for the same reason -- it is the only thing that separates a
            # user who declined from a tool that could not run. See _shot_done.
            proc = subprocess.Popen(cmds[0], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception:
            self._shot_next(cmds[1:], out, elapsed)
            return
        self._shot_proc = proc
        # GLib reaps the child itself, so no waitpid here. The id is kept so the
        # source can be dropped on destroy: it holds a reference to self, and a
        # tool that never exits would otherwise leak it for the life of the
        # process -- the same asymmetry the tick had.
        try:
            self._shot_watch = GLib.child_watch_add(
                GLib.PRIORITY_DEFAULT, proc.pid,
                lambda pid, status: self._shot_done(proc, cmds, out, elapsed))
        except Exception:
            # Nothing will ever call _shot_done for this child, so the pending
            # flag would stay set and the button would be dead for the rest of
            # the session. The guard must not outlive what clears it -- and the
            # child nobody is listening to has to go too, for the same reason
            # _remove_tick kills one: flameshot would sit on a fullscreen grab.
            try:
                proc.kill()
            except Exception:
                pass
            self._shot_proc = None
            self._shot_fail()

    # slurp says "selection cancelled" on stderr, and only there: the message is
    # printed after its event loop ends with a 0x0 result, which is where ESC and
    # right-click both land (slurp 1.5.0 main.c:1076). Every other way slurp
    # exits 1 prints a different message before the loop is ever entered --
    # measured on this machine: "failed to create display" with no compositor,
    # "-p and -r cannot be used together" on bad arguments.
    _DECLINED = "selection cancelled"

    def _shot_done(self, proc, cmds, out, elapsed):
        self._shot_watch = None
        self._shot_proc = None
        # slurp's exit code is untrustworthy the same way flameshot's was, so the
        # geometry it printed is the evidence a region was actually chosen --
        # cancelling with ESC leaves stdout empty. Read it before trusting it.
        try:
            geom = proc.stdout.read().decode().strip()
        except Exception:
            geom = ""
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
        try:
            err = proc.stderr.read().decode()
        except Exception:
            err = ""
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass
        if not geom:
            # Empty geometry alone is ambiguous -- it means both "the user
            # declined" and "the tool never ran". Only the first must capture
            # nothing: falling through to full screen would hand the user the
            # very image they refused, and call it saved. The second still
            # falls back, so a missing or broken slurp does not cost them the
            # screenshot entirely.
            if self._DECLINED in err:
                self._release_freeze()
                self._shot_pending = False
                # No notification. The user pressed ESC a moment ago; telling
                # them what they just did is noise, and the failure this fix
                # exists to prevent was a message claiming the opposite.
                return
            self._shot_next(cmds[1:], out, elapsed)
            return
        self._grim(geom, out, elapsed, cmds)

    def _grim(self, geom, out, elapsed, cmds):
        """Write the actual image. grim is non-interactive and holds no grab, so
        it is waited on rather than watched: it returns in milliseconds."""
        if self._freeze_proc is not None and self._freeze_proc.poll() is not None:
            self._shot_fail()
            return
        cmd = ["grim"] + (["-g", geom] if geom else []) + [str(out)]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except Exception:
            pass
        # Exit code is not the test here either -- the written file is the only
        # reliable evidence a capture happened, as everywhere else in this path.
        if not (out.exists() and out.stat().st_size > 0):
            self._shot_next(cmds[1:], out, elapsed)
            return
        self._release_freeze()
        self._shot_pending = False
        # The capture can land after Stop, since the loop keeps running for the
        # upload. Attaching then is worse than dropping it: on_stop() has already
        # handed rec.screenshots to the upload thread, so the mark would never be
        # sent while the user was told it was saved.
        if self.rec.state == "stopped":
            self._notify("Plaud", "Screenshot descartado — a gravação já foi encerrada")
            return
        self.rec.add_screenshot(out)

    def _on_stop(self, *_):
        self.btn_stop.set_sensitive(False)
        # The timer label that used to read "enviando..." is gone with the
        # official layout, which renders no text. Freezing the waveform is the
        # same acknowledgement in the vocabulary the pill has left; the user is
        # told in words by the notification _do_stop() sends a moment later.
        self.wave.set_active(False)
        GLib.idle_add(self._do_stop)

    def _do_stop(self):
        # Runs its body once, however many callers reach it. There are two, and
        # they arrive by different routes: _on_stop() queues this at
        # PRIORITY_DEFAULT_IDLE (200), while the tray's signal handler calls it
        # synchronously at PRIORITY_DEFAULT (0). With both pending GLib
        # dispatches the signal first and then runs the queued call anyway --
        # priority-ordered, so it reproduces every run, not a narrow race.
        #
        # Without this the session stopped twice: two on_stop() calls, two
        # upload threads, two PlaudClients, two `done`s. Measured with a real
        # click on the real button plus a real SIGTERM -- clients=2, stops=2 on
        # both the tray and the no-tray fallback. Either the duplicate fails
        # fast and its `done` quits the loop out from under the first upload
        # (0/10 parts, no completion marker, rc=0 -- a more silent loss than the
        # signal bug this was meant to fix), or it succeeds and the user's
        # recording is uploaded to their account twice.
        #
        # The guard lives here rather than in either caller because the
        # asymmetry between them is the defect: making one caller match the
        # other still leaves two dispatches of the same work. One method, one
        # execution, and both callers stay honest.
        if self._stopping:
            return False
        self._stopping = True
        path = self.rec.stop()
        self.destroy()
        # Past this line rec.screenshots belongs to the upload thread; anything
        # attached later is never sent. destroy() above is what gives a pending
        # capture its one chance to be recovered, and it runs first.
        self._shots_taken = True
        if self.on_stop:
            self.on_stop(self.rec)
        else:
            Gtk.main_quit()
        return False

    def _on_kebab(self, button):
        """The "..." trigger (OFFICIAL-UI-SPEC.md §6.1-6.2): one destructive
        item, "Discard recording". A Gtk.Menu rather than a Popover -- GTK3 has
        no first-class Popover-from-code-only equivalent as light as this
        codebase's other widgets, and a Menu anchored to the button reproduces
        the spec's `align="end"` popover closely enough for one item.
        """
        menu = Gtk.Menu()
        item = Gtk.MenuItem()
        # text-status-destructive (§6.2): a Pango markup span rather than a
        # CSS provider, since this is the single label in a menu that exists
        # nowhere else -- a provider would be one more thing to keep alive for
        # a color used exactly once.
        lbl = Gtk.Label()
        lbl.set_markup('<span foreground="#ff503f">Descartar gravação</span>')
        item.add(lbl)
        item.connect("activate", self._on_discard_clicked)
        menu.append(item)
        menu.show_all()
        menu.popup_at_widget(button, Gdk.Gravity.SOUTH_EAST, Gdk.Gravity.NORTH_EAST, None)

    def _on_discard_clicked(self, *_):
        """Confirm dialog wording, PROVEN verbatim (OFFICIAL-UI-SPEC.md §6.3)."""
        dlg = Gtk.Dialog(title="Descartar gravação?")
        dlg.set_default_size(400, 220)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_margin_top(20)
        box.set_margin_bottom(14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        lbl = Gtk.Label(
            label="A gravação atual será excluída permanentemente. "
                  "Essa ação não pode ser desfeita.")
        lbl.set_line_wrap(True)
        lbl.set_xalign(0)
        box.add(lbl)
        dlg.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        discard_btn = dlg.add_button("Descartar", Gtk.ResponseType.YES)
        # #DE2716, the confirm button's own literal color (§6.3) -- not the
        # shared --status-destructive token, which is a different red.
        css = Gtk.CssProvider()
        css.load_from_data(
            b"button { background: #DE2716; color: #fff; }"
            b"button:hover { background: #c22012; }")
        discard_btn.get_style_context().add_provider(
            css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        dlg.show_all()
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.YES:
            GLib.idle_add(self._do_discard)

    def _do_discard(self):
        """Stop-and-discard rather than stop-and-upload (§6.3: same
        stopRecording RPC as a normal stop, distinguished by scene: "cancel").

        Shares _stopping with _do_stop -- see that guard's comment -- so a
        discard racing a stop click can only run one of them, never both.
        """
        if self._stopping:
            return False
        self._stopping = True
        self.rec.discard()
        self._notify("Plaud", "Gravação descartada")
        self.destroy()
        self._shots_taken = True
        if self.on_discard:
            self.on_discard(self.rec)
        else:
            Gtk.main_quit()
        return False

    def _notify(self, title, body):
        try:
            # stderr swallowed: libnotify prints three confined-mode warnings
            # per call and this inherits the launcher's redirect into app.log.
            # This is the noisiest caller -- every screenshot, pause and note.
            subprocess.Popen(["notify-send", "-a", "Plaud", title, body],
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # _raise() and _on_press() are gone with the calls that used them. Both were
    # X11-only and already silent no-ops here: a layer surface is stacked by its
    # layer, not by raise_(), and is positioned by the compositor from the
    # anchor above -- begin_move_drag() has nothing to move. Dragging the pill
    # is therefore dropped rather than reimplemented; the fixed top-right anchor
    # is where the official client parks it, and the margins above are the one
    # place to change it.


def run(mode="system", name=None, on_stop=None, on_state_change=None, on_discard=None):
    """Build the overlay and start recording. Does NOT run the GTK loop.

    The loop belongs to the caller: a session must not own the thing that
    outlives it, or "this recording finished" and "this process exits" stay the
    same event (CAN-314 item 1). The resident tray owns the loop (CAN-310), so
    one Gtk.main() outlives many overlays.
    """
    ov = Overlay(mode=mode, name=name, on_stop=on_stop, on_state_change=on_state_change,
                 on_discard=on_discard)
    ov.connect("destroy", lambda *_: None)
    return ov


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "system"
    # on_stop=None, so _do_stop falls through to Gtk.main_quit and this loop is
    # what it quits. UI only -- this path never uploads.
    run(mode=mode)
    Gtk.main()
