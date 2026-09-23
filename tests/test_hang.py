"""Check for the pre-thread hang in main.on_stop.

Usage: python3 tests/test_hang.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

The bug: on_stop() ends the session by calling done() -- either directly on an
early-return path, or from the worker's finally. If anything raises BEFORE
threading.Thread(...).start() gets to run, neither happens: no worker exists to
reach that finally, and the exception unwinds out of the GTK callback into the
main loop, which prints a traceback and keeps running. The overlay is already
destroyed by then, so what is left is a process with no window that only dies
by kill -- the same invisible-eternal-process class as the login path.

Each check drives a REAL Gtk.main() and asserts it returns. A hang is the
failure being tested for, so every check is wrapped in a watchdog that quits
the loop and marks the check failed rather than blocking the run forever.
"""
import os, sys, tempfile, pathlib, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp(prefix="hangchk-")
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
from plaud_linux import main as main_mod

main_mod.notify = lambda *a, **k: None          # keep the desktop quiet

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


class Rec:
    """A recording good enough to reach the worker (past both early returns)."""
    def __init__(self, home, size=8000):
        self.concat_failed = False
        self.segments_lost = 0
        self.segdir = pathlib.Path(home)
        self.final_path = pathlib.Path(home) / "ok.opus"
        self.final_path.write_bytes(b"x" * size)
        self.screenshots, self.notes = [], []


def run_session(rec, timeout_ms=5000):
    """Run on_stop inside a real loop. Returns (loop_ended, done_fired)."""
    fired = []
    timed_out = []

    def scenario():
        try:
            main_mod.on_stop(rec, lambda: (fired.append(True), Gtk.main_quit()) and False)
        except BaseException as e:
            # An exception escaping on_stop is itself the bug: in the real app
            # GTK swallows it and the loop keeps spinning. Reproduce that here
            # rather than letting it abort the check.
            print(f"      (on_stop raised: {e!r})")
        return False

    def watchdog():
        timed_out.append(True)
        Gtk.main_quit()
        return False

    GLib.idle_add(scenario)
    wd = GLib.timeout_add(timeout_ms, watchdog)
    Gtk.main()
    if not timed_out:
        GLib.source_remove(wd)
    return (not timed_out), bool(fired)


home = os.environ["PLAUD_LINUX_HOME"]

# --- CHECK 1: PlaudClient() blowing up must still end the session -----------
# The first thing worker() does is construct a PlaudClient. Pre-fix that call
# sits inside worker()'s try, so this one is actually survivable -- it is the
# control, proving the harness reports PASS when the code is sound.
real_client = main_mod.plaud_api.PlaudClient
main_mod.plaud_api.PlaudClient = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no client"))
try:
    ended, fired = run_session(Rec(home))
finally:
    main_mod.plaud_api.PlaudClient = real_client
check("1 failure inside worker() still ends the session",
      ended and fired, f"(loop ended={ended}, done fired={fired})")

# --- CHECK 2: a failure BEFORE the thread starts must still end the session --
# This is the real bug. Thread.start() raises RuntimeError("can't start new
# thread") for real when the process is at its thread limit -- so this is not
# a hypothetical: it is the exact exception CPython raises on that path.
import threading as _t
real_start = _t.Thread.start
def _no_threads(self):
    raise RuntimeError("can't start new thread")
_t.Thread.start = _no_threads
try:
    ended, fired = run_session(Rec(home))
finally:
    _t.Thread.start = real_start
check("2 failure before the thread starts still ends the session",
      ended and fired, f"(loop ended={ended}, done fired={fired})")

# --- CHECK 3: rec attribute access blowing up must still end the session -----
# on_stop touches rec.final_path / rec.concat_failed before any try exists.
class HostileRec:
    concat_failed = False
    @property
    def final_path(self):
        raise AttributeError("recorder was torn down")
ended, fired = run_session(HostileRec())
check("3 failure reading the recording still ends the session",
      ended and fired, f"(loop ended={ended}, done fired={fired})")

# --- CHECK 4: notify() failing in the handler must not re-open the hang ----
# The review's MEDIUM finding. The except handler called notify() BEFORE
# end_session(), so the guarantee was sequenced behind the noisiest part of
# the path: _bus_notify catches Exception, not BaseException.
real_notify = main_mod.notify
def _hostile_notify(*a, **k):
    raise KeyboardInterrupt()
main_mod.notify = _hostile_notify
try:
    ended, fired = run_session(HostileRec())
finally:
    main_mod.notify = real_notify
check("4 a notify() that raises still ends the session",
      ended and fired, f"(loop ended={ended}, done fired={fired})")

# --- CHECK 5: an exception whose __str__ raises must not re-open it either --
# The handler interpolates {e}, so str(e) runs before notify() is even entered.
class Nasty(Exception):
    def __str__(self):
        raise RuntimeError("__str__ exploded")

class StrBombRec:
    concat_failed = False
    @property
    def final_path(self):
        raise Nasty()
ended, fired = run_session(StrBombRec())
check("5 an exception with a hostile __str__ still ends the session",
      ended and fired, f"(loop ended={ended}, done fired={fired})")

# --- CHECKS 6-9: the same ordering bug in _do_upload's early returns -------
# Verification found the fix applied to on_stop's handler was never applied to
# the two early-return paths, which notify() BEFORE done(). Pre-existing, and
# the concat-failed path is a real mode this file actively handles.

class ConcatFailedRec:
    """Takes the concat-failed early return."""
    def __init__(self, home):
        self.concat_failed = True
        self.segments_lost = 2
        self.segdir = pathlib.Path(home)
        self.final_path = pathlib.Path(home) / "x.opus"
        self.screenshots, self.notes = [], []

class TooShortRec:
    """Takes the too-short early return."""
    def __init__(self, home):
        self.concat_failed = False
        self.segments_lost = 0
        self.segdir = pathlib.Path(home)
        self.final_path = pathlib.Path(home) / "tiny.opus"
        self.final_path.write_bytes(b"x" * 10)
        self.screenshots, self.notes = [], []

for label, rec in (("6 concat-failed", ConcatFailedRec(home)),
                   ("7 too-short", TooShortRec(home))):
    real_notify = main_mod.notify
    main_mod.notify = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
    try:
        ended, fired = run_session(rec)
    finally:
        main_mod.notify = real_notify
    check(f"{label}: notify() raising still ends the session",
          ended and fired, f"(loop ended={ended}, done fired={fired})")

# --- CHECK 8: a BaseException from _do_upload still ends the session -------
class BaseBombRec:
    concat_failed = False
    @property
    def final_path(self):
        raise KeyboardInterrupt()
ended, fired = run_session(BaseBombRec())
check("8 a BaseException still ends the session",
      ended and fired, f"(loop ended={ended}, done fired={fired})")

# --- CHECK 9: a done() that raises once is retried, not marked done --------
# The flag must record the outcome, not the intent.
fired9 = []
attempts = [0]
def flaky_done():
    attempts[0] += 1
    if attempts[0] == 1:
        raise RuntimeError("transient")
    fired9.append(True)
    Gtk.main_quit()

def scenario9():
    rec = ConcatFailedRec(home)
    try:
        main_mod.on_stop(rec, flaky_done)
    except BaseException:
        pass
    # A second arrival must retry, because the first never succeeded.
    try:
        main_mod.on_stop(rec, flaky_done)
    except BaseException:
        pass
    if not fired9:
        Gtk.main_quit()
    return False

real_notify = main_mod.notify
main_mod.notify = lambda *a, **k: None
try:
    GLib.idle_add(scenario9)
    wd9 = GLib.timeout_add(5000, lambda: (Gtk.main_quit(), False)[1])
    Gtk.main()
finally:
    main_mod.notify = real_notify
check("9 a done() that raises once is retried",
      attempts[0] >= 2 and bool(fired9),
      f"(attempts={attempts[0]}, eventually fired={bool(fired9)})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
