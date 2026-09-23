"""Checks for CAN-315 items 1-2: global shortcuts + per-state tray icon.

Usage: python3 tests/test_shortcuts.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).

Two mechanisms under test:

  - Per-state icon: tray._set_icon(state) now takes "idle"/"recording"/"paused"
    instead of a bool, and Overlay reports pause/resume back to it through the
    new on_state_change callback (overlay.py -> main.py -> tray.py, the same
    seam on_stop already crosses).

  - Global shortcuts: `plaud-linux --shortcut <name>` in a SEPARATE process
    reaches the resident instance via Gio.Application.activate_action() over
    the SAME bus name (ai.plaud.LinuxRecorder) and routing style plaud://
    already uses (main._claim_instance / remote.open). No new D-Bus interface,
    no new dependency -- GDBus transports action activation as
    org.gtk.Actions automatically.

This file re-execs itself under a PRIVATE session bus for the same reason
test_instance.py does: ai.plaud.LinuxRecorder is one well-known name per bus,
so two copies of any check spawning a real owner would fight over it. See
test_instance.py's _isolate_bus() docstring for the measured variance that
motivated this (four agents, one untouched tree, four different totals).

What "own process, real Gio.Application" proves that a same-process unit test
cannot: activate_action() dispatches over a real D-Bus round trip, not a
Python function call, so this is the only way to catch a mismatch between
main.py's `--shortcut` sender and tray.py's registered action names -- exactly
the class of bug a mocked call would hide.
"""
import os, shutil, sys, pathlib, subprocess, tempfile, textwrap, time, types


def _isolate_bus():
    if os.environ.get("PLAUD_CHECK_PRIVATE_BUS"):
        return
    dbus = shutil.which("dbus-run-session")
    if not dbus:
        print("WARNING: dbus-run-session not found -- running on the SHARED "
              "session bus. Results are unreliable if another copy of this "
              "file is running.", flush=True)
        return
    os.environ["PLAUD_CHECK_PRIVATE_BUS"] = "1"
    os.execvp(dbus, [dbus, "--", sys.executable, os.path.abspath(__file__)]
                    + sys.argv[1:])


_isolate_bus()

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="shortcutchk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, GLib  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}", flush=True)


try:
    from plaud_linux import tray as tray_mod
    from plaud_linux import main as main_mod
except Exception as e:
    check("0 plaud_linux.tray is importable", False, f"({type(e).__name__}: {e})")
    print()
    print("SOME FAILED")
    sys.exit(1)

tray_mod.main_mod.notify = lambda *a, **k: None
main_mod.paths.prune_old_logs = lambda: None

ENV = dict(os.environ)
for v in ("LD_LIBRARY_PATH", "GTK_PATH", "XDG_DATA_HOME", "GIO_MODULE_DIR",
          "GI_TYPELIB_PATH", "LD_PRELOAD", "GSETTINGS_SCHEMA_DIR"):
    ENV.pop(v, None)
ENV["PLAUD_LINUX_HOME"] = HOME
ENV["PYTHONUNBUFFERED"] = "1"


# --- CHECK 1: _set_icon accepts the three real Recorder states --------------
# audio.Recorder.state is idle/recording/paused/stopped; _set_icon must resolve
# every one of the first three to a distinct (icon, label) pair, and never
# raise on an unknown string (defensive default to idle).
class _FakeIndicator:
    # set_icon, not set_icon_full: tray.py no longer drives
    # AyatanaAppIndicator3 (whose binding cannot set IconPixmap at all) and
    # publishes its own StatusNotifierItem instead. A double that keeps the old
    # method name does not fail loudly -- _set_icon raises AttributeError deep
    # inside the tray, which is the same drift that once made a successful
    # upload report as failed. The double tracks the interface it doubles.
    def __init__(self):
        self.calls = []

    def set_icon(self, icon, label):
        self.calls.append((icon, label))


fi = _FakeIndicator()
tray_mod._indicator = fi
for state in ("idle", "recording", "paused"):
    tray_mod._set_icon(state)
icons = [c[0] for c in fi.calls]
labels = [c[1] for c in fi.calls]
# Three DISTINGUISHABLE states, ONE icon. The owner asked (2026-09-08) that the
# bar never switch to the red "recording" artwork: the vertical pill is the
# recording indicator and a second one in the bar was redundant. The Windows
# client agrees -- its tray icon becomes the pill and returns on stop, with no
# red tray state. So the states are distinct where they still can be, the
# tooltip label, and the pixmap is the idle one for every state. This pins
# both halves: three labels, and no state ever selecting a different icon.
check("1 idle/recording/paused differ in label only; the bar icon never changes",
      len(set(labels)) == 3 and set(icons) == {tray_mod.ICON_IDLE},
      f"(icons={icons} labels={labels})")

fi.calls.clear()
tray_mod._set_icon("something-unexpected")
check("2 an unknown state falls back to idle rather than raising",
      fi.calls and fi.calls[0][0] == tray_mod.ICON_IDLE,
      f"(calls={fi.calls})")
tray_mod._indicator = None


# --- CHECK 2: Overlay reports pause/resume through on_state_change ----------
# The tray has no window of its own to poll rec.state from, so pause/resume
# must be PUSHED to it. Drives the real Overlay._on_pause with a fake Recorder
# and fake widgets, since that method also touches button/tooltip/notify.
class _FakeRec:
    def __init__(self):
        self.state = "recording"
        self.paused_calls = self.resumed_calls = 0

    def pause(self):
        self.paused_calls += 1
        self.state = "paused"

    def resume(self):
        self.resumed_calls += 1
        self.state = "recording"


class _FakeWidget:
    def set_tooltip_text(self, *_):
        pass


try:
    from plaud_linux import overlay as overlay_mod
except ImportError:
    overlay_mod = None
except ValueError:
    overlay_mod = None  # gtk-layer-shell typelib missing on this machine

if overlay_mod is None:
    check("3 Overlay reports pause/resume via on_state_change", False,
          "(gtk-layer-shell typelib missing -- cannot construct Overlay here)")
else:
    ov = overlay_mod.Overlay.__new__(overlay_mod.Overlay)
    ov.rec = _FakeRec()
    ov.btn_pause = _FakeWidget()
    ov._set_pause_icon = lambda *_: None
    ov._notify = lambda *_: None
    reported = []
    ov.on_state_change = lambda s: reported.append(s)
    ov._on_pause()  # recording -> paused
    ov._on_pause()  # paused -> recording
    check("3 Overlay reports pause/resume via on_state_change",
          reported == ["paused", "recording"], f"(reported={reported})")


# --- CHECK 3: the four shortcut actions no-op safely with no live overlay ---
# A hotkey can fire when nothing is recording (or during the login/source
# dialogs, where _overlay is still None). None of the four may raise, and none
# may call into an overlay that does not exist.
tray_mod._overlay = None
raised = []
for fn in (tray_mod._shortcut_stop, tray_mod._shortcut_pause, tray_mod._shortcut_mark):
    try:
        fn()
    except Exception as e:
        raised.append(f"{fn.__name__}: {type(e).__name__}")
check("4 stop/pause/mark no-op without raising when nothing is recording",
      not raised, f"(raised={raised})")


# --- CHECK 4: the four actions ARE registered as Gio.SimpleActions ----------
# _install_actions must wire exactly these four names -- a typo here would
# make main.py's sender and tray.py's receiver silently disagree on a name,
# which only a real cross-process call (CHECK 5) would otherwise catch.
app = Gio.Application(application_id="ai.plaud.test.ShortcutNames",
                      flags=Gio.ApplicationFlags.HANDLES_OPEN)
app.register(None)
tray_mod._install_actions(app)
check("5 record/stop/pause/mark are all registered as app actions",
      sorted(app.list_actions()) == ["mark", "pause", "record", "shot", "show", "stop"],
      f"(registered={sorted(app.list_actions())})")


# --- CHECK 5: a REAL second process reaches a REAL first process ------------
# The actual mechanism end to end: an owner process registers on the real bus
# name and installs the four actions exactly as run_tray() does; a separate
# `plaud-linux --shortcut stop` process finds it as remote and calls
# activate_action(). If this passes, a Hyprland `bind` running the same
# command line reaches a running plaud-linux exactly the same way.
OWNER_SRC = textwrap.dedent('''
    import os, sys, types
    sys.path.insert(0, %(repo)r)
    stub = types.ModuleType("requests")
    def _boom(*a, **k): raise AssertionError("NETWORK")
    stub.get = stub.post = stub.put = _boom
    stub.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = stub
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import Gtk, GLib, Gio
    from plaud_linux import main as m
    from plaud_linux import tray as t
    m.notify = lambda *a, **k: None
    t.main_mod.notify = lambda *a, **k: None

    app = Gio.Application(application_id=m.APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
    app.register(None)
    t._install_actions(app)

    fired = []
    real_stop = t._shortcut_stop
    def spy_stop(*a):
        fired.append("stop")
        print("FIRED:stop", flush=True)
        Gtk.main_quit()
    t._shortcut_stop = spy_stop
    # Re-wire the action to the spy: the action object already captured the
    # original handler at add_action time in _install_actions, so patching the
    # module-level name alone would not be observed -- replace the action.
    new_act = Gio.SimpleAction.new("stop", None)
    new_act.connect("activate", spy_stop)
    app.remove_action("stop")
    app.add_action(new_act)

    print("OWNER-READY", flush=True)
    GLib.timeout_add(10000, lambda: (print("TIMEOUT", flush=True), Gtk.main_quit(), False)[-1])
    Gtk.main()
''') % {"repo": repo}


def spawn_owner():
    p = subprocess.Popen([sys.executable, "-c", OWNER_SRC],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, env=ENV)
    t0 = time.time()
    while time.time() - t0 < 15:
        line = p.stdout.readline()
        if not line:
            break
        if "OWNER-READY" in line:
            return p
    p.kill(); p.wait()
    return None


owner = spawn_owner()
try:
    if owner is None:
        check("6 a real --shortcut stop reaches a real running instance",
              False, "(could not establish an owner process)")
    else:
        r = subprocess.run(
            [sys.executable, "-m", "plaud_linux", "--shortcut", "stop"],
            capture_output=True, text=True, timeout=15, env=ENV, cwd=repo)
        out, err = owner.communicate(timeout=10)
        check("6 a real --shortcut stop reaches a real running instance",
              "FIRED:stop" in out and r.returncode == 0,
              f"(owner_out={out.strip()!r}, sender_rc={r.returncode}, "
              f"sender_stderr={r.stderr.strip()!r})")
finally:
    if owner and owner.poll() is None:
        owner.kill(); owner.wait()


# --- CHECK 6: --shortcut with nothing running declines, does not launch -----
# notify() posts to org.freedesktop.Notifications (or falls back to
# notify-send), never to stdout/stderr -- so this drives main() in a child
# with notify() itself replaced by a print, the same technique
# test_instance.py's run_main() uses, rather than asserting on notify's own
# (invisible, here) side effect.
DECLINE_SRC = textwrap.dedent('''
    import sys, types
    sys.path.insert(0, %(repo)r)
    stub = types.ModuleType("requests")
    def _boom(*a, **k): raise AssertionError("NETWORK")
    stub.get = stub.post = stub.put = _boom
    stub.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = stub
    import gi
    gi.require_version("Gtk", "3.0")
    from plaud_linux import main as m
    m.notify = lambda t, b: print("NOTIFY:" + b, flush=True)
    m.start_session = lambda *a, **k: print("STARTED-SESSION", flush=True)
    try:
        from plaud_linux import tray as _tray
    except Exception:
        _tray = None
    if _tray is not None:
        _tray.run_tray = lambda app=None: print("STARTED-SESSION", flush=True)
    sys.argv = ["plaud-linux", "--shortcut", "record"]
    m.main()
    print("MAIN-RETURNED", flush=True)
''') % {"repo": repo}
r = subprocess.run([sys.executable, "-c", DECLINE_SRC],
                   capture_output=True, text=True, timeout=15, env=ENV)
out = r.stdout + r.stderr
check("7 --shortcut with no instance running declines rather than launching",
      "NOTIFY:Plaud não está em execução." in out
      and "STARTED-SESSION" not in out and r.returncode == 0,
      f"(rc={r.returncode}, out={out.strip()!r})")


# --- CHECK 7: an unknown action name is rejected, not silently activated ----
r = subprocess.run([sys.executable, "-m", "plaud_linux", "--shortcut", "bogus"],
                   capture_output=True, text=True, timeout=15, env=ENV, cwd=repo)
check("8 an unrecognised --shortcut name is rejected up front",
      "ação desconhecida" in (r.stdout + r.stderr),
      f"(out={(r.stdout+r.stderr).strip()!r})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
