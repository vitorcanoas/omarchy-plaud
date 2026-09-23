"""One measured session in its own process. Driven by test_double_stop.py.

argv: <repo> <home> <outdir> <mode: tray|fallback> <arm>

Prints, once the loop returns:
  RESULT parts=<n>/10 marker=<bool> clients=<n> stops=<n> ends=<n>

Its own process because the thing under measurement is a REAL Gtk.main() taking
a REAL SIGTERM, and a signal handler installed by GLib.unix_signal_add is
process-wide -- there is no way to unwind it between arms in one interpreter.

Only two things are stubbed. PlaudClient, because CLAUDE.md forbids a real
upload from a check and because the upload has to be SLOW and observable: ten
files written a quarter-second apart, so "the recording was lost" is a count and
not an inference. And Recorder.start/stop, because ffmpeg is not what is being
measured -- the stop is still counted, which is how a double stop is seen.
Everything between them is real: the Overlay, its button, main.on_stop,
main._do_upload, its daemon worker, tray._on_signal, tray._on_session_end.
"""
import os, sys, pathlib, signal as sigmod, time, types

repo, HOME, OUT, MODE, ARM = sys.argv[1:6]
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

# Any real network call is a broken check, not a slow one. Same guard as
# test_signals.py.
stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

from plaud_linux import audio, main as m, overlay as ov_mod, tray as t  # noqa: E402

out = pathlib.Path(OUT)
out.mkdir(parents=True, exist_ok=True)
m.notify = lambda *a, **k: None
t.main_mod.notify = lambda *a, **k: None
t._set_icon = lambda on: None
m.paths.prune_old_logs = lambda: None
ov_mod.Overlay._notify = lambda self, *a: None
# Decoding is 35 s on a real file and irrelevant here.
m.peak_dbfs = lambda p: None
# The post-upload generation dialog waits for a human, and this probe drives a
# real Gtk.main() with no one to click it. Answered "auto" here for the same
# reason peak_dbfs is stubbed above: it is not what is being measured, and the
# thing that IS -- that the upload completes exactly once and the session ends
# -- runs identically whichever choice comes back. The dismissal path has its
# own checks in test_genchoice.py.
m._ask_generation_mode_sync = lambda *a, **k: "auto"

clients = []


class FakeClient:
    """Slow and controllable, so a signal can be timed against an upload that is
    genuinely in flight. Counted: a second instance means a second upload of the
    user's recording to their account."""

    def is_logged_in(self):
        return True

    def upload_and_generate(self, path, progress=None, auto_generate=True):
        # Status reads also construct clients; count actual uploads instead.
        clients.append(1)
        self.n = len(clients)
        for i in range(10):
            time.sleep(0.25)
            (out / f"c{self.n}_part{i}").write_text("x")
        (out / f"c{self.n}_COMPLETED").write_text("done")
        return "fake-id"

    def generate(self, file_id, progress=None):
        return {"status": 0}

    def attach_screenshots(self, *a, **k):
        return 0


m.plaud_api.PlaudClient = FakeClient

stops = []


def fake_start(self):
    self.state = "recording"
    self._last_src = (None, None, "src", None)


def fake_stop(self):
    stops.append(1)
    self.state = "stopped"
    self.final_path.parent.mkdir(parents=True, exist_ok=True)
    # Over _do_upload's 2000-byte floor, or it returns "gravação muito curta"
    # and never reaches the worker at all.
    self.final_path.write_bytes(b"\0" * 5000)
    return self.final_path


audio.Recorder.start = fake_start
audio.Recorder.stop = fake_stop

m.ensure_login_or_prompt = lambda: True
m.pick_sources = lambda: (True, False)

# The tray's `done` deliberately does NOT end the loop -- that is the whole
# point of a resident tray. For measurement only, wrap it so the process
# terminates: the real one runs first, so its own quit-when-pending logic is
# still what is being measured, and the delay lets a SECOND `done` (the failure
# being hunted) arrive and be counted before the loop returns.
ends = []
_real_end = t._on_session_end


def measured_end():
    ends.append(1)
    _real_end()
    GLib.timeout_add(1500, lambda: (Gtk.main_quit(), False)[1])


t._on_session_end = measured_end


def settle():
    while Gtk.events_pending():
        Gtk.main_iteration()


def arm():
    """Fire the arm's stimulus once a real overlay exists and is mapped.

    show_all() and the settle() matter: Gtk.Button.clicked() on an unmapped or
    insensitive button dispatches nothing, and a check that measures nothing
    passes for the wrong reason.
    """
    ov = t._overlay
    assert ov is not None, "no overlay -- the session never started"
    ov.show_all()
    ov.controls.show_all()
    settle()
    assert ov.btn_stop.get_sensitive() and ov.btn_stop.get_mapped(), \
        "the Parar button is not clickable; the click would measure nothing"

    if ARM == "control":
        ov.btn_stop.clicked()
    elif ARM == "attack":
        # The defect: the click queues _do_stop at PRIORITY_DEFAULT_IDLE, the
        # signal runs it at PRIORITY_DEFAULT. GLib dispatches the signal first
        # and then runs the queued call anyway.
        ov.btn_stop.clicked()
        os.kill(os.getpid(), sigmod.SIGTERM)
    elif ARM == "signal-then-click":
        # The reverse order: the signal stops the recorder synchronously, and
        # the user then presses Parar on a button the overlay has not finished
        # tearing down. The click must not start a second upload -- and it must
        # not leave the signal deferring on a `done` that never comes either.
        os.kill(os.getpid(), sigmod.SIGTERM)
        ov.btn_stop.clicked()
    elif ARM == "twice":
        ov._do_stop()
        ov._do_stop()
    elif ARM == "signal-only":
        # CAN-321 itself: no button, just a signal while recording. The handler
        # stops the recorder, which creates the upload, and defers to `done`.
        os.kill(os.getpid(), sigmod.SIGTERM)
    elif ARM == "second-session":
        # Session two's own signal. Session one set _signal_pending and never
        # cleared it, so a handler reading a stale flag takes the "already
        # pending" branch here: notify and return, recorder never stopped, no
        # upload created, no `done` -- and nothing below ever writes a part.
        os.kill(os.getpid(), sigmod.SIGTERM)
        return False
    else:
        raise AssertionError(f"unknown arm {ARM!r}")
    return False


if ARM == "second-session":
    # Two full sessions in one process, and only the SECOND is measured.
    #
    # Session one is ended by a signal, because that is what sets
    # _signal_pending -- but a signal that reaches _on_session_end also quits
    # the loop, which would end the process before session two exists. So the
    # signal is delivered to _on_signal directly, without _on_session_end being
    # allowed to act on it: the flag is set exactly as a real signal sets it,
    # and the loop survives to run session two. Session one's stop is the
    # button, so nothing about session one is left half-finished.
    first_done = []

    def second():
        # Session one has ended. Discard its evidence and start over, so every
        # counter below describes session two alone.
        for f in out.iterdir():
            f.unlink()
        clients.clear()
        stops.clear()
        ends.clear()
        t._start()
        GLib.idle_add(arm, priority=GLib.PRIORITY_HIGH)
        return False

    def measured_end_two():
        if not first_done:
            first_done.append(1)
            # _on_session_end's real body, minus the quit it would do for
            # session one's pending signal -- which is the whole point: this
            # process has to reach session two to measure it. Whether the flag
            # SURVIVES that reset is the thing under test, so the reset itself
            # is the real function's, not written out here.
            was_pending = t._signal_pending
            _real_end_no_quit()
            print(f"SESSION1 ended, pending_was={was_pending} "
                  f"pending_now={t._signal_pending}", flush=True)
            GLib.idle_add(second)
        else:
            ends.append(1)
            GLib.timeout_add(1500, lambda: (Gtk.main_quit(), False)[1])

    _quit = Gtk.main_quit

    def _real_end_no_quit():
        Gtk.main_quit = lambda: None
        try:
            _real_end()
        finally:
            Gtk.main_quit = _quit
            t.Gtk.main_quit = _quit

    def arm_one():
        ov = t._overlay
        ov.show_all()
        ov.controls.show_all()
        settle()
        # Set the flag the way a real signal does, then stop with the button.
        t._on_signal()
        return False

    t._on_session_end = measured_end_two
    t._install_signal_handlers()
    t._start()
    GLib.idle_add(arm_one, priority=GLib.PRIORITY_HIGH)
    Gtk.main()
elif MODE == "tray":
    t._install_signal_handlers()
    t._start()
    GLib.idle_add(arm, priority=GLib.PRIORITY_HIGH)
    Gtk.main()
else:
    # The no-tray fallback branch of run_tray: a different `done` policy and its
    # own pre-set flag. run_tray() enters the loop itself, so the arm is queued
    # beforehand and fires once that loop is running.
    t._install_signal_handlers()
    t._watcher_present = lambda: False
    GLib.idle_add(lambda: (t._start(), arm(), False)[-1], priority=GLib.PRIORITY_HIGH)
    t.run_tray()

parts = len(list(out.glob("c1_part*")))
marker = (out / "c1_COMPLETED").exists()
print(f"RESULT parts={parts}/10 marker={marker} clients={len(clients)} "
      f"stops={len(stops)} ends={len(ends)}", flush=True)
