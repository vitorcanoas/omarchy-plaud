#!/usr/bin/env python3
"""
plaud-linux :: "Envios recentes" window (GTK3)

The list that closes the lifecycle loop: what is in recordings/, whether it
reached the server, and a button to send the ones that did not.

The name is the official client's own. Its tray menu reads "Envios recentes"
(docs/plaud-desktop/README.md §1, media/image15.png), and the standby panel
carries the same label over a collapsible list of past uploads
(media/image18.png). Gap-analysis item 16 in that README lists the
recent-uploads list as the thing this app was missing; this is it.

Where we deliberately diverge from the official layout, and why: there, the
list is a section *inside* the standby panel, because that client has a
standby panel to put it in. This one does not -- plaud-linux is idle as a
tray icon with no window at all (tray.py, "we have no window when idle"), so
the list has to be something the menu can open. A separate small window is
the minimum that works, and it keeps the tray's existing menu idiom intact.

Deliberately NOT a note browser. CLAUDE.md's boundary is that this app does
not reimplement Plaud Web, and the official client honours the same line --
its own "generate custom" hands off to the browser rather than rendering the
note. So a row here shows only what the local filesystem knows: name, when,
how long, how big, and what became of the upload. There is no transcript, no
summary, no editing.

This window owns no Recorder and touches no network. Resending calls
main.resend(), which is where the network lives, for the same reason
overlay.py calls back into main rather than importing plaud_api: the layer
rule in CLAUDE.md. Reading the disk goes through library.py, which is pure.

Sizing follows the official windows, which are small: recording-main is
280x215 and recording-mini 226x126, and the observed "Envios recentes" list
sits inside a panel of about that width. 300 is that width plus room for a
status pill the official list does not carry, and the height is ours -- a
scrolling list needs rows visible, and the official one is a section in a
taller panel rather than a window with a measurable height of its own.
"""
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

import sys  # noqa: E402

try:
    from . import library
except ImportError:
    import library


# THESE NUMBERS ARE A CHOICE, NOT A MEASUREMENT -- said plainly because every
# other geometry constant in this project cites a bundle line, and a reader is
# entitled to know this one cannot. The official client has no recents window
# to measure: its 17 windows are enumerated in docs/plaud-desktop/OFFICIAL-UI-SPEC.md
# §1 and none of them is a list, because there the recents list is a section
# inside the standby panel (media/image18.png).
#
# So the size is bracketed by the house style rather than copied from it:
# recording-main is 280x215 and confirm-dialog 400x220, both PROVEN in §1, and
# the observed list sits in a panel of about that width. 300 is that width plus
# room for the status pill the official row does not carry. The height is the
# part with no precedent at all -- a scrolling list has to show more than two
# rows, and the official one borrows its height from a taller parent panel.
WINDOW_WIDTH = 300
WINDOW_HEIGHT = 420
MIN_WIDTH = 260
MIN_HEIGHT = 200

WMCLASS = "plaud-library"

# Tokens are PROVEN, unlike the geometry above: docs/plaud-desktop/OFFICIAL-UI-SPEC.md
# §7, same values panel.py already cites -- --background-secondary #f9f9f9,
# --separator-default #ebebeb, --labels-secondary #3d3d3d,
# --labels-tertiary #7a7a7a, --separator-emphasized #d6d6d6.
#
# The state colours derive from the bundle's own semantic tokens rather than
# picked-by-eye pastels: --status-destructive #ff503f, --status-constructive
# #36d96c, --status-cautionary #fabe3e. They are used as the pill's TEXT over a
# tinted ground, not as its fill -- at full saturation on a 10px pill they are
# the loudest thing in a list whose whole job is to be scanned, and #36d96c on
# white fails contrast outright. Darkened for text, tinted for ground; the hue
# is the token's, the lightness is this pill's.
CSS = b"""
.plaud-lib, window.plaud-lib { background-color: #f9f9f9; }
.plaud-lib-header {
  border-bottom: 1px solid #ebebeb;
  padding: 10px 12px;
}
.plaud-lib-title { color: #3d3d3d; font-weight: 600; font-size: 13px; }
.plaud-lib-sub { color: #7a7a7a; font-size: 11px; }
.plaud-lib-row {
  border-bottom: 1px solid #ebebeb;
  padding: 9px 12px;
}
.plaud-lib-row:last-child { border-bottom: none; }
.plaud-lib-name { color: #3d3d3d; font-size: 12px; }
.plaud-lib-meta { color: #7a7a7a; font-size: 11px; }
.plaud-lib-empty { color: #7a7a7a; font-size: 12px; }
/* Status pills. Muted on purpose -- a list where every row shouts is a list
   nobody reads. Only the states that need the user to act carry colour. */
.plaud-lib-badge {
  font-size: 10px;
  padding: 1px 7px;
  /* 8px = the bundle's "larger" radius token; 4px is its standard. A pill
     wants the larger one. */
  border-radius: 8px;
  background: #ebebeb;
  color: #7a7a7a;
}
.plaud-lib-badge-uploaded { background: #eefaf2; color: #1c8a44; }
.plaud-lib-badge-failed   { background: #ffefed; color: #d02b1b; }
.plaud-lib-badge-unsent   { background: #fff6e6; color: #9a6a10; }
.plaud-lib-badge-segments { background: #fff6e6; color: #9a6a10; }
.plaud-lib-badge-tiny     { background: #ebebeb; color: #7a7a7a; }
.plaud-lib-btn {
  font-size: 11px;
  padding: 3px 10px;
  /* 4px = the bundle's standard radius. */
  border-radius: 4px;
  border: 1px solid #d6d6d6;
  background: #ffffff;
  color: #3d3d3d;
}
.plaud-lib-btn:hover  { background: #f2f2f2; }
.plaud-lib-btn:active { background: #ebebeb; }
.plaud-lib-btn:disabled { background: #f7f7f7; color: #a3a3a3; border-color: #ebebeb; }
"""

# Per-screen, exactly as panel.py and overlay.py do it: the provider outlives
# the window, so opening this twice would otherwise stack two identical
# providers that nothing ever removes.
_css_screens = set()

# The one live window. Opening the list twice gave two views over the same
# directory, and a resend in one left the other showing a stale row offering
# to send it again -- which is the duplicate-note failure this whole feature
# exists to avoid. Present the existing one instead.
_window = None


class LibraryWindow(Gtk.Window):
    """A read-only list of recordings, with a resend button per orphan."""

    def __init__(self, on_resend=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        # Injected rather than imported so this module never reaches the
        # network itself, and so the checks can drive it without a live
        # PlaudClient. main.py passes main.resend.
        self._on_resend = on_resend
        self.set_title("Envios recentes")
        self.set_wmclass(WMCLASS, WMCLASS)
        self.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.set_size_request(MIN_WIDTH, MIN_HEIGHT)
        self.set_position(Gtk.WindowPosition.CENTER)
        self._apply_css()
        self.get_style_context().add_class("plaud-lib")
        self._rows_box = None
        self._subtitle = None
        self._build_ui()
        self.connect("destroy", self._on_destroy)
        self.refresh()

    # --- lifetime ----------------------------------------------------------

    def _on_destroy(self, *_):
        # Release the singleton, or the next open would present a destroyed
        # window: a GTK window that has been destroyed still exists as a Python
        # object, and present() on it is a silent no-op -- the menu item would
        # simply stop working for the rest of the process's life.
        global _window
        if _window is self:
            _window = None

    def _apply_css(self):
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
        root.get_style_context().add_class("plaud-lib")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.get_style_context().add_class("plaud-lib-header")
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        t = Gtk.Label(label="Envios recentes", xalign=0.0)
        t.get_style_context().add_class("plaud-lib-title")
        titles.pack_start(t, False, False, 0)
        self._subtitle = Gtk.Label(label="", xalign=0.0)
        self._subtitle.get_style_context().add_class("plaud-lib-sub")
        titles.pack_start(self._subtitle, False, False, 0)
        header.pack_start(titles, True, True, 0)

        btn = Gtk.Button(label="Atualizar")
        btn.get_style_context().add_class("plaud-lib-btn")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self.refresh())
        header.pack_end(btn, False, False, 0)
        root.pack_start(header, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(self._rows_box)
        root.pack_start(scroller, True, True, 0)

        self.add(root)

    # --- content -----------------------------------------------------------

    def refresh(self):
        """Rebuild every row from the current state of the disk.

        A full rebuild rather than patching the changed row: the list is at
        most a few dozen rows on a personal machine, and a partial update is
        how a row ends up showing an outcome that the sidecar no longer
        agrees with. Reading the disk is the cheap, correct answer here.
        """
        for child in self._rows_box.get_children():
            self._rows_box.remove(child)
            child.destroy()

        entries = library.scan()
        n_resendable = sum(1 for e in entries if e.resendable)
        if not entries:
            self._subtitle.set_text("Nenhuma gravação local.")
        elif n_resendable:
            self._subtitle.set_text(
                f"{len(entries)} gravação(ões) · {n_resendable} por enviar")
        else:
            self._subtitle.set_text(f"{len(entries)} gravação(ões) · tudo enviado")

        if not entries:
            empty = Gtk.Label(
                label="Nada em Gravações ainda.\nAs gravações aparecem aqui "
                      "assim que a primeira sessão terminar.")
            empty.get_style_context().add_class("plaud-lib-empty")
            empty.set_justify(Gtk.Justification.CENTER)
            empty.set_margin_top(28)
            self._rows_box.pack_start(empty, False, False, 0)
        else:
            for e in entries:
                self._rows_box.pack_start(self._row(e), False, False, 0)
        self._rows_box.show_all()

    def _row(self, entry):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("plaud-lib-row")

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label(label=entry.session, xalign=0.0)
        name.get_style_context().add_class("plaud-lib-name")
        name.set_ellipsize(3)  # Pango.EllipsizeMode.END
        # The session name is the only place a long string can force the
        # window wider than the user sized it; every other field is bounded.
        name.set_max_width_chars(28)
        left.pack_start(name, False, False, 0)

        bits = [library.human_when(entry.when)]
        if entry.elapsed_s:
            bits.append(library.human_duration(entry.elapsed_s))
        if entry.size_bytes:
            bits.append(library.human_size(entry.size_bytes))
        if entry.segments_lost:
            bits.append(f"{entry.segments_lost} trecho(s) perdido(s)")
        meta = Gtk.Label(label="  ·  ".join(bits), xalign=0.0)
        meta.get_style_context().add_class("plaud-lib-meta")
        left.pack_start(meta, False, False, 0)
        row.pack_start(left, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_valign(Gtk.Align.CENTER)
        badge = Gtk.Label(label=library.STATE_LABEL[entry.state])
        badge.get_style_context().add_class("plaud-lib-badge")
        badge.get_style_context().add_class(f"plaud-lib-badge-{entry.state}")
        badge.set_halign(Gtk.Align.END)
        right.pack_start(badge, False, False, 0)

        if entry.resendable:
            b = Gtk.Button(label="Reenviar")
            b.get_style_context().add_class("plaud-lib-btn")
            b.set_halign(Gtk.Align.END)
            b.connect("clicked", self._resend_clicked, entry)
            right.pack_start(b, False, False, 0)
        row.pack_end(right, False, False, 0)

        tip = library.STATE_HINT[entry.state]
        if entry.error:
            tip = f"{tip}\n{entry.error}"
        row.set_tooltip_text(tip)
        return row

    def _resend_clicked(self, btn, entry):
        if self._on_resend is None:
            return
        # Insensitive immediately, and never re-enabled by this handler: a
        # second click while the first upload is in flight would send the same
        # file twice and produce two notes on the server for one recording.
        # The refresh below rebuilds the row from the sidecar, so the button
        # comes back only if the resend genuinely failed.
        btn.set_sensitive(False)
        btn.set_label("Reenviando…")
        self._on_resend(entry, lambda: self.refresh())


def open_library(on_resend=None):
    """Show the list, reusing the one that is already open.

    Returns the window either way, so a caller can tell it succeeded.
    """
    global _window
    if _window is not None:
        _window.refresh()
        _window.present()
        return _window
    _window = LibraryWindow(on_resend=on_resend)
    _window.show_all()
    _window.present()
    return _window


if __name__ == "__main__":
    # smoke test: show the list against whatever PLAUD_LINUX_HOME points at.
    # No resend callback -- this entry point must not reach the network, so the
    # buttons are inert here by construction rather than by discipline.
    w = open_library(on_resend=None)
    w.connect("destroy", Gtk.main_quit)
    print(f"library window: {len(library.scan())} gravação(ões)", flush=True)
    Gtk.main()
    sys.exit(0)
