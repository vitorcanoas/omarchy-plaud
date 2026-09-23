"""Checks for CAN-321: a signal must not kill an in-flight upload.

Usage: python3 tests/test_signals.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).

The tray's `Sair` refuses to quit while a recording or an upload is live, but
SIGTERM/SIGINT/SIGHUP walked straight past it: nothing installed a handler, so
the default disposition ended the process and the daemon=True upload worker died
with it. Measured with a probe importing none of this code -- a worker writing
ten parts stopped at two for all three signals, exit without traceback, nothing
in any log.

The invariant every check below attacks: **every state either exits NOW or
defers -- never neither, never both.**

  - "neither" is a process the user cannot quit. That is the failure mode a
    previous attempt at this fix actually shipped: a signal during the source
    dialog set a pending quit that nothing could ever action, because no session
    existed to produce a `done`.
  - "both" is a second quit path racing the upload, which discards it -- the
    very defect being fixed.

These drive the REAL `_start()` rather than pre-setting the flags by hand. That
distinction is the whole point: an earlier round of checks configured the flag
state directly, so none of them ever ran `_start()`, and that is precisely where
two defects were living.
"""
import os, sys, pathlib, subprocess, tempfile, textwrap, time, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="sigchk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}", flush=True)


try:
    from plaud_linux import main as main_mod
    from plaud_linux import tray as tray_mod
except Exception as e:
    check("0 plaud_linux.tray is importable", False, f"({type(e).__name__}: {e})")
    print()
    print("SOME FAILED")
    sys.exit(1)

# A tree predating the fix has no handler at all. Report that as every check
# failing, rather than dying on the first AttributeError: the file must still
# RUN against an older revision -- that is what the optional repo argument is
# for, and a crash there proves nothing about which behaviours are missing.
_MISSING = [n for n in ("_on_signal", "_install_signal_handlers",
                        "_started", "_signal_pending", "_quit_when_done",
                        "_overlay")
            if not hasattr(tray_mod, n)]
if _MISSING:
    for i, name in enumerate(
            ["a signal during an upload defers, and `done` is what quits",
             "a signal while recording stops the recorder and defers",
             "a signal while idle exits immediately",
             "a signal during the source dialog exits instead of hanging",
             "repeated signals keep waiting instead of forcing the exit",
             "the deferred path quits exactly once",
             "a raising _do_stop still defers rather than exiting",
             "the handler returns True so the source survives for the next signal",
             "the no-tray fallback's first signal stops the recorder and defers",
             "the no-tray fallback still exits once `done` fires",
             "a real SIGTERM lets a real in-flight upload finish"], 1):
        check(f"{i} {name}", False, f"(tray.py is missing: {', '.join(_MISSING)})")
    print()
    print("SOME FAILED")
    sys.exit(1)

main_mod.notify = lambda *a, **k: None
tray_mod.main_mod.notify = lambda *a, **k: None
tray_mod._set_icon = lambda on: None
main_mod.paths.prune_old_logs = lambda: None

quit_calls = []
tray_mod.Gtk.main_quit = lambda: quit_calls.append(1)


class FakeOverlay:
    """Stands in for the real overlay: the only thing the handler asks of it is
    _do_stop(). Recording it is how a check tells "the recorder was stopped"
    from "the handler skipped it"."""
    def __init__(self, raises=False):
        self.stopped = 0
        self.raises = raises

    def _do_stop(self):
        self.stopped += 1
        if self.raises:
            raise OSError(28, "No space left on device")


def reset():
    tray_mod._busy = False
    tray_mod._started = False
    tray_mod._signal_pending = False
    tray_mod._quit_when_done = False
    tray_mod._overlay = None
    quit_calls.clear()


def _stub_overlay_module(overlay):
    """Put the fake overlay where start_session() will actually look for it.

    Not `main_mod.overlay_mod = ...`: start_session() does the import INSIDE
    itself (`from . import overlay as overlay_mod`) so a missing
    gtk-layer-shell typelib cannot kill --status/--login, which means the name
    it uses is a LOCAL, and a module attribute of that name is never read.
    Stubbing sys.modules is what the local import resolves against, so this
    reaches the real call site instead of a name nothing reads.
    """
    stub = types.ModuleType("plaud_linux.overlay")
    # **_ swallows on_state_change: start_session() (CAN-315) now passes it
    # through to overlay_mod.run() as a third keyword the real function
    # accepts, and this fake must accept it too or the real call site raises
    # TypeError before this test's own assertions run.
    stub.run = lambda mode, on_stop, **_: overlay
    sys.modules["plaud_linux.overlay"] = stub


def start_real(overlay):
    """Drive the REAL _start() to a live-session state.

    Only the leaves are stubbed -- login, the source dialog, and overlay
    construction -- so tray._start()'s own flag bookkeeping is the code under
    test rather than something the check asserts by hand.
    """
    main_mod.ensure_login_or_prompt = lambda: True
    main_mod.pick_sources = lambda: (True, False)
    _stub_overlay_module(overlay)
    tray_mod._start()


# --- CHECK 1: a signal mid-upload defers instead of quitting -----------------
# The defect itself, driven through the real _start(). The overlay is gone (Stop
# already ran) and the upload is in flight: the handler must NOT quit.
reset()
ov = FakeOverlay()
start_real(ov)
tray_mod._overlay = None          # Stop ran; upload now in flight
tray_mod._on_signal()
deferred = (not quit_calls) and tray_mod._signal_pending
# `done` arriving is the ONLY thing that may quit -- never a timer.
tray_mod._on_session_end()
check("1 a signal during an upload defers, and `done` is what quits",
      deferred and len(quit_calls) == 1,
      f"(quit_during_upload={not deferred and 'yes' or 'no'}, "
      f"pending={tray_mod._signal_pending}, quits_after_done={len(quit_calls)})")

# --- CHECK 2: a signal while recording stops the recorder, then defers -------
# Stopping is what CREATES the upload, so a handler that defers without stopping
# waits forever on a `done` nobody will send.
reset()
ov = FakeOverlay()
start_real(ov)
tray_mod._on_signal()
check("2 a signal while recording stops the recorder and defers",
      ov.stopped == 1 and not quit_calls and tray_mod._signal_pending,
      f"(stopped={ov.stopped}, quit={bool(quit_calls)}, pending={tray_mod._signal_pending})")

# --- CHECK 3: idle exits immediately ----------------------------------------
# The other half of the invariant. A guard that never lets go is not a fix.
reset()
tray_mod._on_signal()
check("3 a signal while idle exits immediately",
      len(quit_calls) == 1, f"(quit_calls={len(quit_calls)})")

# --- CHECK 4: THE HANG -- a signal during the source dialog ------------------
# _start() sets _busy BEFORE start_session(), and the login prompt and source
# dialog run a NESTED Gtk loop (dlg.run()) that dispatches this very handler.
# So _busy is True while nothing exists to lose. Deferring here waits on a
# `done` that can never come: the app becomes unquittable and only SIGKILL ends
# it -- which discards the next recording. This is the "neither" state, and a
# previous attempt at this fix shipped exactly it.
#
# Driven through the REAL dialog call, not by setting flags: the handler fires
# from inside pick_sources(), which is where it really would arrive.
reset()
fired = {}
def dialog_that_gets_a_signal():
    fired["busy"] = tray_mod._busy
    fired["started"] = tray_mod._started
    tray_mod._on_signal()          # signal arrives while the dialog is up
    fired["quit"] = len(quit_calls)
    fired["pending"] = tray_mod._signal_pending
    return None                    # user never chose; treat as cancelled
main_mod.ensure_login_or_prompt = lambda: True
main_mod.pick_sources = dialog_that_gets_a_signal
tray_mod._start()
check("4 a signal during the source dialog exits instead of hanging",
      fired.get("busy") is True and fired.get("quit") == 1
      and not fired.get("pending"),
      f"(_busy_during_dialog={fired.get('busy')}, _started={fired.get('started')}, "
      f"quit={fired.get('quit')}, pending={fired.get('pending')})")

# --- CHECK 5: two signals must not escalate into a forced exit ---------------
# The second Ctrl-C is the reflex when the first appears to do nothing. If it
# force-quits, it discards the upload the first one chose to protect -- turning
# the fix into the bug.
reset()
ov = FakeOverlay()
start_real(ov)
tray_mod._overlay = None
tray_mod._on_signal()
tray_mod._on_signal()
tray_mod._on_signal()
survived = not quit_calls
tray_mod._on_session_end()
check("5 repeated signals keep waiting instead of forcing the exit",
      survived and len(quit_calls) == 1,
      f"(quit_before_done={not survived}, quits_after_done={len(quit_calls)})")

# --- CHECK 6: "both" -- exactly one quit, never two -------------------------
# A double quit path is the other half of the invariant. Counted, not asserted
# as a boolean: two main_quit calls is a race with the upload, not a tidiness
# issue.
reset()
ov = FakeOverlay()
start_real(ov)
tray_mod._on_signal()             # stops recorder, defers
tray_mod._on_session_end()        # upload done
check("6 the deferred path quits exactly once",
      len(quit_calls) == 1, f"(quit_calls={len(quit_calls)})")

# --- CHECK 7: a raising _do_stop must not be read as "nothing to wait for" ---
# The self-inflicted regression from the previous attempt: the stop attempt
# raised, the handler read that as "failed to stop, so exit now", and it killed
# the upload on the exact path it existed to protect. A full disk (OSError 28)
# is the realistic source.
reset()
ov = FakeOverlay(raises=True)
start_real(ov)
raised = None
try:
    tray_mod._on_signal()
except Exception as e:
    raised = type(e).__name__
check("7 a raising _do_stop still defers rather than exiting",
      raised is None and not quit_calls and tray_mod._signal_pending,
      f"(handler_raised={raised}, quit={bool(quit_calls)}, pending={tray_mod._signal_pending})")

# --- CHECK 8: the handler never raises, so GLib keeps the source -------------
# Measured: when a handler installed by unix_signal_add raises, GLib REMOVES the
# source, and the next signal hits the default disposition and kills the process
# outright (rc=-15, upload lost). So "the handler returns True and does not
# raise" is load-bearing, not style.
reset()
ov = FakeOverlay(raises=True)
start_real(ov)
rv = tray_mod._on_signal()
check("8 the handler returns True so the source survives for the next signal",
      rv is True, f"(returned={rv!r})")

# --- CHECK 9: the no-tray fallback -- the path that fooled two gates ---------
# Its `done` is Gtk.main_quit and its policy flag is set BEFORE the session, so
# an implementation that reads one flag for both "this run exits when the
# session ends" and "a signal asked to quit" makes the FIRST signal here a
# no-op: recorder never stopped, upload never created. Driven through the real
# run_tray() fallback branch, with the watcher forced absent.
reset()
ov = FakeOverlay()
main_mod.ensure_login_or_prompt = lambda: True
main_mod.pick_sources = lambda: (True, False)
_stub_overlay_module(ov)
tray_mod._watcher_present = lambda: False
tray_mod._install_signal_handlers = lambda: None
entered = []
tray_mod.Gtk.main = lambda: entered.append(1)
tray_mod.run_tray()
tray_mod._start()  # The user explicitly starts; opening a card must not record.
# The fallback is now mid-session with its policy flag set. A signal here must
# still stop the recorder and defer -- not be swallowed by the policy flag.
before = len(quit_calls)
tray_mod._on_signal()
check("9 the no-tray fallback's first signal stops the recorder and defers",
      ov.stopped == 1 and len(quit_calls) == before,
      f"(entered_loop={bool(entered)}, stopped={ov.stopped}, "
      f"quit_on_first_signal={len(quit_calls) - before})")

# --- CHECK 10: the fallback still exits when its upload finishes -------------
# The policy must survive the signal handling: this run has no tray to return
# to, so `done` must end the process either way.
tray_mod._on_session_end()
check("10 the no-tray fallback still exits once `done` fires",
      len(quit_calls) == before + 1,
      f"(quits_after_done={len(quit_calls) - before})")

# --- CHECK 11: end to end, a real signal against a real loop -----------------
# Everything above drives the handler by calling it. This one sends a REAL
# SIGTERM to a REAL process running a REAL Gtk.main() with a daemon worker, and
# judges by parts written and a completion marker -- never by exit code, which
# was 0 for the losing case that motivated this whole issue.
outdir = pathlib.Path(HOME) / "e2e"
prog = textwrap.dedent(f'''
    import os, sys, threading, time, pathlib, types
    os.environ["PLAUD_LINUX_HOME"] = {HOME!r}
    sys.path.insert(0, {repo!r})
    stub = types.ModuleType("requests")
    def _b(*a, **k): raise AssertionError("NETWORK")
    stub.get = stub.post = stub.put = _b
    stub.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = stub
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    from plaud_linux import tray as t, main as m
    m.notify = lambda *a, **k: None
    t.main_mod.notify = lambda *a, **k: None
    t._set_icon = lambda on: None
    out = pathlib.Path({str(outdir)!r}); out.mkdir(parents=True, exist_ok=True)

    # Stand in for the upload worker: same shape as main._do_upload's -- a
    # daemon thread that ends by handing `done` back through GLib.idle_add.
    def upload():
        for i in range(10):
            time.sleep(0.3)
            (out / ("part%d" % i)).write_text("x")
        (out / "COMPLETED").write_text("done")
        GLib.idle_add(t._on_session_end)

    class Ov:
        def _do_stop(self):
            threading.Thread(target=upload, daemon=True).start()

    t._install_signal_handlers()
    t._busy = True; t._started = True; t._overlay = Ov()
    print("READY", flush=True)
    Gtk.main()
''')
script = pathlib.Path(HOME) / "e2e.py"
script.write_text(prog)
env = dict(os.environ)
for k in ("LD_LIBRARY_PATH", "GTK_PATH", "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR"):
    env.pop(k, None)
p = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, env=env)
# Wait for READY rather than sleeping a guessed interval: on a loaded machine a
# fixed sleep signals before the handler is installed and the check measures
# nothing.
ready = p.stdout.readline().strip() if p.stdout else ""
time.sleep(0.5)
p.send_signal(__import__("signal").SIGTERM)
try:
    rc = p.wait(timeout=30)
except subprocess.TimeoutExpired:
    p.kill(); rc = "TIMEOUT"
parts = len(list(outdir.glob("part*"))) if outdir.exists() else 0
done_marker = (outdir / "COMPLETED").exists()
check("11 a real SIGTERM lets a real in-flight upload finish",
      ready == "READY" and parts == 10 and done_marker and rc == 0,
      f"(ready={ready!r}, parts={parts}/10, completed={done_marker}, rc={rc})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
