#!/usr/bin/env python3
"""
plaud-linux :: resident tray icon (CAN-310)

Owns the one long-lived Gtk.main(). A session is now something that starts and
ends *inside* the loop instead of being the loop -- start_session() no longer
calls Gtk.main(), and the `done` it passes resets this icon rather than quitting.

Layer rule: this is an entry-point concern, so it calls into main.py and never
into plaud_api or audio directly.

We publish org.kde.StatusNotifierItem on the session bus OURSELVES (see
_Sni below) rather than going through AyatanaAppIndicator3. That library has
no way to set IconPixmap at all -- gi.repository.AyatanaAppIndicator3.Indicator
exposes only set_icon()/set_icon_full()/set_icon_theme_path(), every one a
NAME the rendering host must resolve itself. quickshell (this machine's SNI
host, confirmed by reading its own source at
git.outfoxxed.me/quickshell/quickshell/src/services/status_notifier/) resolves
IconName via Qt's theme lookup FIRST and falls back to IconPixmap only if that
lookup fails; it never reads IconThemePath at all, custom themes included --
measured here too: our own item's IconThemePath sat at "" on the live bus
despite set_icon_theme_path() being called before the first paint. So a name
was always going to dead-end, whether or not a theme is configured, and
IconPixmap was never reachable through this library to serve as the fallback.

State note (owner, 2026-09-08): the bar icon is the SAME idle artwork in every
state. Recording and paused change only the tooltip/menu labels, never the
pixels -- the vertical pill already says "recording", and a second red icon in
the bar was redundant and confusing. This matches the Windows 1.3.7 client,
whose tray icon is replaced by the pill while recording and simply returns
when the recording stops (docs/plaud-desktop/README.md sections 4 and 6).
"""
import signal
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, GLib  # noqa: E402

try:
    from . import login as login_mod  # noqa: E402
    from . import main as main_mod  # noqa: E402
except ImportError:
    import login as login_mod  # noqa: E402
    import main as main_mod  # noqa: E402

# Bundled icons: real ARGB32 pixel data shipped straight over IconPixmap, not a
# themed name for any host to resolve. 22x22 status-size PNGs, decoded once at
# indicator construction (see _load_pixmap below).
#
# Why not themed names any more: a themed name is resolved by the RENDERING
# HOST, and quickshell -- the bar on this machine -- looks the name up itself,
# never reads IconThemePath, and only falls back to IconPixmap when the lookup
# fails. Its own log recorded the failure verbatim ("Unable to create pixmap
# for tray icon"). AyatanaAppIndicator3's Python binding has no way to set a
# pixmap at all, which is why that library is gone from this module entirely.
_TRAY_ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "tray"
ICON_IDLE = "plaud-linux"
# Recording and paused reuse the idle artwork DELIBERATELY, and keep their own
# tooltips. The owner asked (2026-09-08) that the bar never switch to the red
# "recording" icon: the vertical pill is the recording indicator, and the bar
# showing it a second time was redundant and confusing. The Windows client
# behaves the same way -- its tray icon becomes the pill and comes back when the
# recording ends; it never shows a red tray state. assets/tray/plaud-
# recording.ico stays on disk (it is Plaud's artwork, documented in
# docs/assets-official/README.md) but nothing loads it any more. The states are
# still distinct everywhere they can be without new pixels: _ICON_BY_STATE
# below maps them to "Gravando"/"Pausado", so the user reads the state from
# the tooltip.
ICON_RECORDING = ICON_IDLE
ICON_PAUSED = ICON_IDLE

# Three states still exist -- idle/recording/paused, matching
# audio.Recorder.state -- but they now share one pixmap and differ only in the
# label. The official client's other two variants (`ppc-off` personal-
# workspace, `update-available` auto-update) still don't apply here.
_indicator = None

# True between "the overlay was built" and "the upload finished". One flag, not
# a lock: every mutation happens on the GTK main thread -- the session starts in
# a menu callback and ends via GLib.idle_add -- which is the same argument
# on_stop()'s `ended` flag already makes.
_busy = False

# True once a session really began -- i.e. start_session() returned True and an
# overlay exists (or existed, and its upload is now in flight). This is NOT the
# same question as _busy, and conflating them is what a previous attempt at this
# fix got wrong: _busy is already True during the login prompt and the source
# dialog, both of which run a NESTED Gtk loop (dlg.run()) that dispatches this
# module's signal handler. In that window there is no recorder, no upload and
# nothing to lose, so a signal must exit immediately -- deferring there waits
# for a `done` that can never come, and the app becomes unkillable by anything
# short of SIGKILL, which is exactly the discard this issue exists to prevent.
_started = False

# A signal asked us to quit and we deferred. Deliberately separate from the
# no-tray fallback's own "quit when the session ends" policy: one earlier
# version used a single flag for both, so in the fallback -- where the policy
# flag is set BEFORE the session -- the first signal read its own pending flag
# as already set and became a no-op. The recorder was never stopped, the upload
# never created, and the comment on that branch asserted it was covered.
_signal_pending = False

# This run exits when the session ends, because it has no tray to go back to.
# A POLICY, decided up front by the no-tray fallback -- deliberately not the
# same variable as _signal_pending, which is a signal's REQUEST arriving later.
# One earlier version used a single flag for both and the fallback's own policy
# silenced the first signal there.
_quit_when_done = False

# The live session's overlay while one exists, else None. Only the signal
# handler reads it, and only to stop a recording it is about to wait for.
_overlay = None
_standby = None


def _load_pixmap(name):
    """Decode the official 24x24 ICO frame into the SNI IconPixmap wire format.

    org.kde.StatusNotifierItem.IconPixmap is a(iiay): width, height, then
    ARGB32 bytes in NETWORK (big-endian) byte order with straight (not
    premultiplied) alpha -- confirmed against quickshell's own parser
    (dbus_item_types.cpp, which byte-swaps on a little-endian host). PIL
    gives RGBA; each pixel is reordered to A,R,G,B to match.
    """
    # Imported here rather than at module scope, and allowed to raise: a
    # machine without Pillow must fail at tray CONSTRUCTION, where main.py
    # already catches it and falls back to a no-tray run, rather than at
    # import time, where it would take --status and the plaud:// callback
    # down with it -- both of which run as separate processes while a
    # recording is in flight and draw no tray at all.
    from PIL import Image
    # `name` is kept for the callers' vocabulary; every name now decodes the
    # same idle frame (see the ICON_* comment above).
    del name
    source = _TRAY_ICON_DIR / "plaud-idle.ico"
    im = Image.open(source).ico.getimage((24, 24)).convert("RGBA")
    w, h = im.size
    argb = bytearray(w * h * 4)
    for i, (r, g, b, a) in enumerate(im.getdata()):
        argb[i * 4:i * 4 + 4] = bytes((a, r, g, b))
    return (w, h, bytes(argb))


class _Sni:
    """Our own org.kde.StatusNotifierItem, published directly on the bus.

    Replaces AyatanaAppIndicator3, which has no way to set IconPixmap --
    only name-based calls a rendering host must resolve itself, and
    quickshell's resolution of our names came up empty (see module
    docstring). This class exists to serve the one property that library
    cannot: real ARGB32 pixels, the same way a tray icon that DOES paint on
    this host (Insync, measured via `busctl --user introspect`) does it.
    """

    _INTROSPECTION_XML = """
    <node>
      <interface name="org.kde.StatusNotifierItem">
        <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
        <property name="Category" type="s" access="read"/>
        <property name="Id" type="s" access="read"/>
        <property name="Title" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <property name="IconName" type="s" access="read"/>
        <property name="IconPixmap" type="a(iiay)" access="read"/>
        <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
        <property name="ItemIsMenu" type="b" access="read"/>
        <signal name="NewIcon"/>
        <signal name="NewToolTip"/>
        <signal name="NewStatus"><arg type="s"/></signal>
      </interface>
    </node>
    """

    def __init__(self, menu, on_activate):
        self._menu = menu
        self._on_activate = on_activate
        self._pixmaps = {ICON_IDLE: _load_pixmap(ICON_IDLE)}
        self._icon = ICON_IDLE
        self._title = "Plaud Linux"
        self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._object_path = "/StatusNotifierItem"
        node_info = Gio.DBusNodeInfo.new_for_xml(self._INTROSPECTION_XML)
        iface_info = node_info.interfaces[0]
        # Own a unique name so RegisterStatusNotifierItem can address us by
        # it -- the watcher stores whatever we register (our unique bus
        # name here, matching how Insync and NordVPN register on this host).
        self._reg_id = self._conn.register_object(
            self._object_path, iface_info,
            self._handle_method_call, self._handle_get_property, None)

    def _handle_method_call(self, connection, sender, path, interface,
                             method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._on_activate)
        elif method == "ContextMenu":
            GLib.idle_add(self._show_menu)
        # Scroll: no-op, nothing to scroll.
        invocation.return_value(None)

    def _show_menu(self):
        try:
            from . import desktop_ui
        except ImportError:
            import desktop_ui
        if getattr(self, "_context_window", None) is not None:
            self._context_window.destroy()
        self._context_window = desktop_ui.context_menu(self._menu)
        self._context_window.connect("destroy", lambda *_: setattr(self, "_context_window", None))
        return False

    def _handle_get_property(self, connection, sender, path, interface, name):
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", "plaud-linux")
        if name == "Title":
            return GLib.Variant("s", self._title)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "IconName":
            return GLib.Variant("s", "")
        if name == "IconPixmap":
            w, h, data = self._pixmaps[self._icon]
            return GLib.Variant("a(iiay)", [(w, h, data)])
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], "Plaud Linux", self._title))
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        return None

    def register_with_watcher(self):
        # The shell can restart independently of this resident application.
        self._watch_id = Gio.bus_watch_name_on_connection(
            self._conn, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
            self._watcher_appeared, None)

    def _watcher_appeared(self, *_):
        try:
            self._register()
        except GLib.Error as exc:
            print(f"tray registration failed: {exc}", flush=True)

    def _register(self):
        self._conn.call_sync(
            "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._conn.get_unique_name(),)),
            None, Gio.DBusCallFlags.NONE, 3000, None)

    def set_icon(self, name, title):
        # NewIcon only when the pixmap really changed -- with one artwork for
        # every state that is never, so a state change costs the host one
        # tooltip refresh and no repaint.
        changed = name != self._icon
        self._icon = name
        self._title = title
        if changed:
            self._conn.emit_signal(
                None, self._object_path, "org.kde.StatusNotifierItem",
                "NewIcon", None)
        self._conn.emit_signal(None, self._object_path, "org.kde.StatusNotifierItem",
                               "NewToolTip", None)


_ICON_BY_STATE = {
    "idle": (ICON_IDLE, "Plaud Linux"),
    "recording": (ICON_RECORDING, "Gravando"),
    "paused": (ICON_PAUSED, "Pausado"),
}


def _set_icon(state):
    """state is one of "idle"/"recording"/"paused" -- audio.Recorder.state's
    vocabulary minus "stopped", which _on_session_end reverts to "idle" for.

    Only the label differs between states now (see ICON_*); the artwork is
    always the idle one. A state string rather than a bool so the third state
    fits without a second parameter. The bool version this replaced took every caller's "idle" as
    truthy and showed the RECORDING icon on an idle tray -- which is why the
    callers, not this signature, are the contract to match.
    """
    if _indicator is not None:
        icon, label = _ICON_BY_STATE.get(state, _ICON_BY_STATE["idle"])
        _indicator.set_icon(icon, label)


def _watcher_present():
    """Is there a StatusNotifierItem host to render us?

    Without one an indicator renders NOWHERE: the process is alive, holds the
    bus name, and is invisible -- the worst failure mode this codebase has, and
    one it has already hit. Cheap to ask, so ask before committing to a tray.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 3000, None)
        return r.unpack()[0]
    except Exception:
        return False


def _on_session_end():
    """The tray's `done`: this session ended, the process keeps running.

    Replaces Gtk.main_quit in start_session(). Nothing here ends the loop, which
    is what makes an in-flight upload un-killable by a session ending.

    Unless a signal asked us to leave while the upload was still running. This
    is the ONLY place a deferred quit is honoured, because `done` is the only
    evidence the upload actually finished -- never a timer, since the worker is
    daemon=True and a timer that fires first discards exactly what it was meant
    to protect.
    """
    global _busy, _started, _overlay, _signal_pending
    _busy = False
    _started = False
    _overlay = None
    _set_icon("idle")
    if _standby is not None:
        _standby.set_busy(False)
        _standby.refresh()
    # Either reason ends the loop, and both mean the same thing here: the upload
    # is over, so quitting can no longer discard it.
    quit_now = _signal_pending or _quit_when_done
    # Cleared with the rest of the per-session state, not left behind. A stale
    # flag makes the NEXT session's first signal take the "already pending"
    # branch: it notifies and returns without stopping the recorder, so no
    # upload is ever created and no `done` ever arrives -- the "neither" state.
    # Latent only because Gtk.main() is the last statement in both run_tray
    # branches today; that is a property of the caller, not of this flag.
    # _quit_when_done is deliberately NOT cleared: it is this run's policy, not
    # a per-session request.
    _signal_pending = False
    if quit_now:
        Gtk.main_quit()


def _open_settings(*_):
    """Menu "Configurações". Opens the settings window (CAN-315 item 4).

    Imported here, not at module scope: settings.py pulls in Gtk like every
    other GUI module, but the import staying local keeps this file's own
    import-time cost the same as before for the (more common) path where the
    user never opens it -- same reasoning as main.py's lazy `import tray`.
    """
    try:
        from . import settings as settings_mod
    except ImportError:
        import settings as settings_mod
    settings_mod.open_settings()


def _close_card():
    if _indicator is not None and _watcher_present():
        _standby.hide()
    else:
        _quit()


def _show(*_):
    """Opening the application never starts or stops capture."""
    global _standby
    context = getattr(_indicator, "_context_window", None)
    if context is not None:
        context.destroy()
    if _overlay is not None and getattr(_overlay.rec, "state", "stopped") in ("recording", "paused"):
        try:
            from . import recording_main
        except ImportError:
            import recording_main
        recording_main.open_for(_overlay)
        return False
    if _standby is None:
        try:
            from . import desktop_ui
        except ImportError:
            import desktop_ui
        _standby = desktop_ui.Standby(
            _start, _open_settings, main_mod.open_web_home,
            lambda fid: main_mod._open_url(f"{login_mod.WEB_HOST}/file/{fid}"),
            on_close=_close_card)
    _standby.start.set_label("Iniciar gravação" if main_mod.login_available() else "Entrar no Plaud")
    _standby.refresh()
    _standby.show_all()
    _standby.present()
    return False


def _upload_progress(text):
    if _standby is not None:
        _standby.set_busy(True, text)
    return False


def _upload_ready():
    if _standby is not None:
        _standby.set_busy(False)
        _standby.hide()


def _session_stopped(rec, done):
    _set_icon("idle")
    try:
        _show()
        _standby.set_busy(True)
    except Exception as exc:
        print(f"upload card failed: {exc}", flush=True)
    main_mod.on_stop(rec, done)


def _start(*_):
    """The explicit recording action. Begins a session inside the running loop."""
    global _busy, _started, _overlay
    if _busy:
        main_mod.notify("Plaud Linux", "Já existe uma gravação em andamento.")
        return
    # Set before start_session(), cleared by _on_session_end(). start_session()
    # can still return early (login declined, mode cancelled) without ever
    # producing a `done` -- those paths never reach overlay_mod.run() -- so the
    # flag is cleared here on the early return rather than being leaked.
    #
    # The finally is what makes that true when start_session *raises* rather
    # than returns. Without it the flag stuck True forever, and because GTK
    # catches exceptions out of a menu callback and keeps the loop spinning,
    # the result was silent: no recording, no upload, Gravar refusing to start
    # and Sair refusing to quit, with kill the only way out. Measured on a
    # recordings dir made unwritable -- a full disk or a read-only remount --
    # which raises PermissionError from Recorder's own mkdir.
    #
    # `started` is initialised because the finally reads it on the raising path.
    started = False
    _busy = True
    _set_icon("recording")
    if _standby is not None:
        _standby.hide()
    try:
        started = main_mod.start_session(on_session_end=_on_session_end,
                                          on_state_change=_set_icon)
    finally:
        # start_session() returns the overlay when a session began, False
        # otherwise -- truthy either way for the flag below.
        _overlay = started or None
        _started = bool(started)
        if not started:
            _busy = False
            _set_icon("idle")
            if _standby is not None:
                _standby.show_all()
        # A signal that arrived while the source dialog was up was answered
        # there and then (nothing existed to lose), so it never reaches here.
        # But a signal delivered during start_session() AFTER the overlay was
        # built defers, and if the session then failed to start there is no
        # `done` coming to honour it -- so honour it here instead. Without this
        # the tray sits with a pending quit nobody will ever action.
        if _signal_pending and not started:
            Gtk.main_quit()


def _open_library(*_):
    """Open the "Envios recentes" list. CAN-315 item 5.

    Through main.py, never straight into library/librarywin: tray.py is an
    entry point and the layer rule keeps it calling into main.py only (see the
    module docstring). main.open_library() is what knows how to hand the
    window a resend callback that can reach the network.

    Not guarded by _busy. The list only reads recordings/, and the resend it
    offers uploads a finished file from an earlier session, which shares no
    state with a session in progress -- there is nothing here for the quit
    guard to protect. Note that _busy is NOT set by opening this window
    either: it is not a session, and making it one would mean the user could
    not quit the app while a list was merely open.
    """
    try:
        main_mod.open_library()
    except Exception as e:
        # A failure building the window must not take down the tray -- the
        # icon is the only way to record, and losing it to a list is a far
        # worse outcome than the list not opening.
        main_mod.notify("Plaud Linux", f"Não consegui abrir os envios recentes: {e}")
        print(f"tray: open_library failed: {e}", flush=True)


def _quit(*_):
    """Refuse to quit while a recording or an upload is live.

    This is the one way the tray could break "never lose a recording". The
    upload worker is daemon=True: it dies the instant Gtk.main() returns, with no
    finally and no notification -- measured, a worker with 2 s of work left died
    silently. So a quit that races the upload does not merely risk the recording,
    it discards it.

    The official client guards the same way (lifecycle.ts, createBeforeQuitHandler):
    it checks `recordingStatus !== Idle || hasActiveUpload` -- an active upload
    blocks quit, not just an active recording -- calls event.preventDefault(),
    and offers *staying* as the confirm action. Where we deliberately diverge:
    they exit on a 150 ms timer, which they get away with because their upload
    lives in another process. Ours does not, so we refuse and let the user quit
    once `done` has actually fired.
    """
    if login_mod.is_authenticating():
        main_mod.notify("Plaud Linux", "Aguarde a conclusão do login antes de sair.")
        return
    if _busy:
        main_mod.notify(
            "Plaud Linux",
            "Gravação ou envio em andamento — o Plaud continua aberto. "
            "Tente sair novamente quando terminar.")
        return
    Gtk.main_quit()


def _shortcut_record(*_):
    """Global-shortcut action "record": same entry point as the menu/left-click.

    Not a new path -- _start() already guards _busy on its own, so a shortcut
    firing mid-session just gets the same "already recording" notification the
    menu item would.
    """
    _start()


def _shortcut_stop(*_):
    """Global-shortcut action "stop": same as pressing Parar on the pill.

    Not a quit path -- it starts the upload (via Overlay._do_stop -> on_stop),
    it does not end the loop. Only meaningful once a session has actually
    started (_started), matching the same guard _on_signal uses before it
    touches _overlay: during the login prompt or the source dialog _overlay is
    still None and there is nothing to stop.
    """
    if _overlay is None:
        main_mod.notify("Plaud Linux", "Nenhuma gravação em andamento.")
        return
    _overlay._do_stop()


def _shortcut_pause(*_):
    """Global-shortcut action "pause": same as the pill's pause/resume button.

    Overlay._on_pause() already no-ops outside recording/paused, so the only
    guard needed here is "is there an overlay at all".
    """
    if _overlay is None:
        main_mod.notify("Plaud Linux", "Nenhuma gravação em andamento.")
        return
    _overlay._on_pause()


def _shortcut_shot(*_):
    if _overlay is not None and _overlay.rec.state in ("recording", "paused"):
        _overlay._on_shot()
    else:
        main_mod.notify("Plaud Linux", "Nenhuma gravação em andamento.")


def _shortcut_mark(*_):
    """Global-shortcut action "mark": same as the pencil, opens the highlights
    panel. Matches the official client's Alt+Shift+H (highlight) in spirit --
    our annotation flow is panel-first rather than a silent timestamp, see
    Overlay._on_note's own docstring.
    """
    if _overlay is None:
        main_mod.notify("Plaud Linux", "Nenhuma gravação em andamento.")
        return
    _overlay._on_note()


def _install_actions(app):
    """Wire the four shortcut actions onto the app's own GApplication actions.

    A second `plaud-linux --shortcut <name>` process registers as a REMOTE on
    the same bus name (APP_ID, see main._claim_instance) and calls
    app.activate_action(name, None); GDBus transports that over
    org.gtk.Actions with no extra interface of our own, and the callback below
    runs on THIS process's existing Gtk.main() -- the same main thread every
    other tray callback already runs on, so no GLib.idle_add is needed here
    any more than it is in _start()/_quit().

    Registered unconditionally (both the indicator branch and the no-tray
    fallback) because a global shortcut must keep working even in the
    degraded, tray-less environment -- the bus name is still held either way.
    """
    if app is None:
        return
    for name, handler in (("record", _shortcut_record),
                          ("stop", _shortcut_stop),
                          ("pause", _shortcut_pause),
                          ("mark", _shortcut_mark),
                          ("show", _show),
                          ("shot", _shortcut_shot)):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", handler)
        app.add_action(action)


def _on_signal(*_):
    """SIGTERM/SIGINT/SIGHUP: quit like `Sair` does, never on top of an upload.

    The tray's `Sair` already refuses to quit while a recording or an upload is
    live, but a signal walks straight past it: nothing in this package installed
    a handler, so the default disposition ended the process and the daemon=True
    upload worker died with it. Measured with a probe importing none of this
    code -- a worker writing ten parts stopped at two for all three signals,
    with no traceback and nothing in any log.

    Resident is what makes this the normal exit rather than an edge case: logout,
    reboot and shutdown all send SIGTERM, and a TUI is closed with Ctrl-C.

    The rule every branch below keeps: **either exit now or defer -- never
    neither, and never both.** "Neither" is a process the user cannot quit;
    "both" is a second quit path racing the upload, i.e. the discard this exists
    to prevent.

      idle .......................... exit now (nothing to lose)
      busy but no session started ... exit now -- the login prompt and the source
                                      dialog run a NESTED loop that dispatches
                                      this handler, and there is no recorder and
                                      no upload in that window. Deferring here
                                      waits on a `done` that can never arrive.
      recording ..................... stop the recorder, which starts the upload,
                                      then defer to `done`
      uploading ..................... defer to `done`

    Honest about its limit, in the one place that survives: this REDUCES the
    window, it does not close it. SIGKILL cannot be caught, and a session
    manager's logout grace period is finite -- seconds, typically -- so a large
    upload will still be cut short by the SIGKILL that follows. Normal quit and
    normal logout stop discarding uploads; a forced kill still does.

    Returns True so GLib keeps the source alive for a second signal. Never
    raises, and that is load-bearing rather than tidy: when a handler installed
    this way raises, GLib REMOVES the source, so the next signal hits the
    default disposition and kills the process outright -- measured, rc=-15 with
    the upload lost. So the body is total, and the stop attempt is guarded.
    """
    global _signal_pending
    if not _busy:
        Gtk.main_quit()
        return True
    if not _started:
        # Busy, but nothing has been recorded yet: the user is still in the
        # login prompt or the source dialog. Quit now rather than wait forever.
        Gtk.main_quit()
        return True
    if _signal_pending:
        # A second signal while already waiting. Deliberately NOT an escalation
        # to a forced exit: that would discard the upload the first signal chose
        # to protect, which is the whole defect. Say so and keep waiting.
        main_mod.notify(
            "Plaud Linux",
            "Envio em andamento — o Plaud sai assim que terminar.")
        return True
    _signal_pending = True
    # Stop the recorder if one is still running; that is what creates the
    # upload we are about to wait for. If the overlay is already gone the
    # upload is in flight and there is simply nothing to stop.
    #
    # Guarded because a failure to stop must NOT be read as "nothing to wait
    # for, exit now" -- an earlier attempt at this fix did exactly that, and it
    # killed the upload on the precise path this handler exists to protect.
    # Once _signal_pending is set we are committed to waiting for `done`, and
    # `done` is guaranteed by on_stop()'s own contract, which ends the session
    # even when its body fails.
    ov = _overlay
    if ov is not None:
        try:
            ov._do_stop()
        except Exception:
            # The recorder may already be stopping, or stopping may have failed.
            # Swallowed on purpose: a failure to stop must never be read as
            # "nothing to wait for, exit now".
            #
            # Known residual, stated rather than glossed: if _do_stop() raises,
            # no upload is ever created, so no `done` will arrive and this stays
            # deferred -- `Sair` refuses (still busy) and further signals wait.
            # That is the "neither" state, and it is reachable ONLY through the
            # separate, pre-existing defect that overlay._do_stop() has no
            # try/except of its own (the same raise from the Parar button hangs
            # the loop today, with or without this handler). Fixing that belongs
            # to that defect, not here. The trade is deliberate: waiting costs a
            # process the user must kill, while exiting costs the recording --
            # and the recording is the thing this file exists to protect.
            pass
    main_mod.notify(
        "Plaud Linux",
        "Saindo… aguardando o envio terminar para não perder a gravação.")
    return True


def _install_signal_handlers():
    """Catch the three signals that end a resident process.

    GLib.unix_signal_add, not signal.signal. The reason usually given for this
    -- that a Python handler is starved while the loop sits in a C call -- was
    measured here and is FALSE: with the loop blocked 3 s inside a C call (what
    peak_dbfs() really does for ~35 s on a 2 h file), both mechanisms ran the
    handler, both delayed until the call returned, neither lost it.

    The real reason is different and still decisive: a GLib source runs as a
    normal callback on the main loop, so it may touch GTK -- Gtk.main_quit(),
    a notification -- directly, while a signal.signal handler runs at an
    arbitrary point between bytecodes and may not. This handler does both.

    It also takes over SIGINT from PyGObject's own fallback, which otherwise
    calls Gtk.main_quit() and re-raises KeyboardInterrupt -- quitting straight
    through the guard, which for Ctrl-C is the primary path.
    """
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _on_signal)


def run_tray(app=None, show_on_launch=True):
    """Build the indicator and run the ONE long-lived loop.

    `app` is the Gio.Application holding the bus name, threaded through to
    start_session() so a routed plaud:// still lands in this process.
    """
    global _indicator, _busy, _started, _overlay, _quit_when_done
    # Before anything can start, so a signal arriving during the very first
    # dialog is caught rather than killing the process by default disposition.
    _install_signal_handlers()
    # Unconditional, before the tray/no-tray branch: a global shortcut must
    # keep reaching this process even when there is no visible indicator.
    _install_actions(app)
    main_mod._desktop_progress = _upload_progress
    main_mod._desktop_stopped = _session_stopped
    main_mod._desktop_ready = _upload_ready

    menu = Gtk.Menu()
    item_rec = Gtk.MenuItem(label="Gravar")
    item_rec.connect("activate", _start)
    menu.append(item_rec)
    # The official client's tray menu carries this same entry, under this same
    # name (docs/plaud-desktop/README.md §1, media/image15.png). Unlike
    # "Gravar" it is NOT guarded by _busy: the list only reads recordings/ and
    # calling it during a session is a reasonable thing to want -- the user
    # checking whether last week's failed upload is still waiting while this
    # week's records. Resending from it during a session is allowed for the
    # same reason: it uploads a different, already-finished file, and the two
    # uploads share no state.
    item_lib = Gtk.MenuItem(label="Envios recentes")
    item_lib.connect("activate", lambda *_: main_mod.open_web_home())
    menu.append(item_lib)
    local = Gtk.MenuItem(label="Gravações locais / reenviar")
    local.connect("activate", _open_library)
    menu.append(local)
    # Same official menu (docs/plaud-desktop/media/image15.png: "Preferências"
    # sits below "Envios recentes"). Opens the settings window (CAN-315 item 4).
    item_settings = Gtk.MenuItem(label="Preferências")
    item_settings.connect("activate", _open_settings)
    menu.append(item_settings)
    item_quit = Gtk.MenuItem(label="Sair")
    item_quit.connect("activate", _quit)
    menu.append(item_quit)
    menu.show_all()

    # Activation opens the current card; only the recording button captures.
    try:
        _indicator = _Sni(menu, on_activate=_show)
        _indicator.register_with_watcher()
    except Exception as exc:
        print(f"tray unavailable: {exc}", flush=True)
        main_mod.notify("Plaud Linux", "Não consegui exibir o ícone. O painel continua disponível.")
        show_on_launch = True

    # start_session() wires the `open` handler for routed plaud:// URLs, but it
    # only runs when a session starts. The tray is resident, so wire it here too
    # -- a login callback arriving while idle must still be consumed.
    main_mod.wire_login_handler(app, on_complete=lambda ok: _show())

    if show_on_launch or not _watcher_present():
        _show()
    Gtk.main()


if __name__ == "__main__":
    run_tray()
