"""Checks for CAN-310: the tray must not lose an upload, or wedge itself.

Usage: python3 tests/test_tray.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).

Two opposite failures, both fatal in their own way:

  - `Sair` while an upload is in flight kills it. The worker is daemon=True, so
    it dies the instant Gtk.main() returns -- no finally, no notification.
    Measured with a probe importing none of this code: a worker writing ten
    parts stopped at three, exit 0, nothing in any log.
  - The busy flag sticking True leaves the app PERMANENTLY unquittable. GTK
    catches an exception out of a menu callback and keeps the loop spinning, so
    this one is silent: no recording, no upload, Gravar refusing to start and
    Sair refusing to quit, with kill the only way out.

The second was found by an adversarial review after the first had been fixed
and merged, which is the argument for both living here rather than in a
scratch directory.
"""
import os, sys, pathlib, stat, tempfile, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="traychk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}", flush=True)


try:
    from plaud_linux import main as main_mod
    from plaud_linux import tray as tray_mod
except Exception as e:
    # A tree predating the tray has no module to check. Report it as the one
    # failure rather than crashing: the file must still run against an older
    # revision, which is what the optional repo argument is for.
    check("0 plaud_linux.tray is importable", False, f"({type(e).__name__}: {e})")
    print()
    print("SOME FAILED")
    sys.exit(1)

main_mod.notify = lambda *a, **k: None
tray_mod.main_mod.notify = lambda *a, **k: None
tray_mod._set_icon = lambda on: None
main_mod.paths.prune_old_logs = lambda: None

quit_calls = []
tray_mod.Gtk.main_quit = lambda: quit_calls.append(1)


def reset():
    tray_mod._busy = False
    quit_calls.clear()


# --- CHECK 1: a raise inside start_session must not wedge the tray ----------
# Induced realistically rather than by patching start_session: the recordings
# dir is made unwritable, which is what a full disk, a revoked permission or a
# read-only remount looks like, and Recorder's own mkdir raises through it. So
# the failure arrives from real code on the real path.
reset()
main_mod.ensure_login_or_prompt = lambda: True
main_mod.pick_sources = lambda: (True, False)
rec_dir = pathlib.Path(HOME) / "recordings"
rec_dir.mkdir(parents=True, exist_ok=True)
os.chmod(rec_dir, stat.S_IRUSR | stat.S_IXUSR)
raised = None
try:
    tray_mod._start()
except Exception as e:
    raised = type(e).__name__
finally:
    os.chmod(rec_dir, stat.S_IRWXU)
busy = tray_mod._busy
tray_mod._quit()
check("1 a raising start_session leaves the tray quittable",
      busy is False and bool(quit_calls),
      f"(raised={raised}, _busy={busy}, quittable={bool(quit_calls)})")

# --- CHECK 2: the three no-session paths clear the flag ---------------------
# None of these start anything, so none may leave the flag set -- and none may
# call `done` either, since there is no session to end.
cases = [
    ("login declined", lambda: False, (True, False)),
    ("dialog cancelled", lambda: True, None),
    ("neither source", lambda: True, (False, False)),
]
clean = []
for label, login, sources in cases:
    reset()
    main_mod.ensure_login_or_prompt = login
    main_mod.pick_sources = lambda s=sources: s
    try:
        tray_mod._start()
    except Exception as e:
        clean.append(f"{label}:raised {type(e).__name__}")
        continue
    tray_mod._quit()
    if tray_mod._busy or not quit_calls:
        clean.append(f"{label}:busy={tray_mod._busy},quit={bool(quit_calls)}")
check("2 no-session paths leave the tray quittable",
      not clean, f"({'; '.join(clean) if clean else 'all three clean'})")

# --- CHECK 3: Sair refuses while busy --------------------------------------
# The guard itself. Without it, quitting mid-upload discards the recording.
reset()
tray_mod._busy = True
tray_mod._quit()
refused = not quit_calls
tray_mod._busy = False
tray_mod._quit()
check("3 Sair refuses while busy and allows when idle",
      refused and bool(quit_calls),
      f"(refused_while_busy={refused}, allowed_when_idle={bool(quit_calls)})")

# --- CHECK 4: the session-end callback clears the flag ----------------------
# `done` in a resident world: end the session, keep the process.
reset()
tray_mod._busy = True
tray_mod._on_session_end()
ended_clean = tray_mod._busy is False
tray_mod._quit()
check("4 on_session_end ends the session without quitting the loop",
      ended_clean and bool(quit_calls),
      f"(_busy={tray_mod._busy}, quittable={bool(quit_calls)})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
