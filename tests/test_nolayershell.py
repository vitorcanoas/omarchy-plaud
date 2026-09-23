"""Checks that the non-GUI CLI paths survive a missing gtk-layer-shell typelib.

Usage: python3 tests/test_nolayershell.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

Why this file exists: gtk-layer-shell is a system typelib, and overlay.py calls
require_version() for it at module scope. A missing typelib raises ValueError,
NOT ImportError -- so the dual-import shim every module carries does not catch
it, and importing overlay at main.py's module scope made --status, --login and
the plaud:// handler all die with a raw traceback on a machine without the
package. None of those three draws an overlay. The launcher sends stdout to
app.log, so a GUI launch showed the user nothing at all.

Nothing else in tests/ would catch this: every other check runs in a process
where the typelib is present, which is exactly the machine the bug is invisible
on. So these checks spawn real subprocesses with that ONE namespace blocked,
which is the only way to reproduce the missing-package machine on this one.
"""
import os, sys, tempfile, pathlib, subprocess

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="nolschk-")

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# Blocks exactly one namespace and leaves every other require_version() alone,
# so what the child proves is "gtk-layer-shell is missing", not "gi is broken".
# sitecustomize runs before the app imports anything, which is what makes this
# equivalent to the typelib genuinely not being installed.
shimdir = tempfile.mkdtemp(prefix="nolsshim-")
pathlib.Path(shimdir, "sitecustomize.py").write_text(
    'import gi\n'
    '_real = gi.require_version\n'
    'def _blocked(namespace, version):\n'
    '    if namespace == "GtkLayerShell":\n'
    '        raise ValueError("Namespace GtkLayerShell not available")\n'
    '    return _real(namespace, version)\n'
    'gi.require_version = _blocked\n')


def run_cli(arg, timeout=90):
    """Run `python3 -m plaud_linux <arg>` with the typelib blocked."""
    env = dict(os.environ)
    env["PYTHONPATH"] = shimdir + os.pathsep + repo
    env["PLAUD_LINUX_HOME"] = home
    try:
        r = subprocess.run([sys.executable, "-m", "plaud_linux", arg],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=repo, env=env)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return "TIMEOUT", (e.stdout or "") + (e.stderr or "")


# The signature of the bug: the traceback from the uncaught ValueError. Matched
# on the namespace plus a traceback marker rather than on exit status alone,
# because --login legitimately exits non-zero in other environments.
def crashed_on_typelib(out):
    return "Traceback" in out and "GtkLayerShell" in out


# --- CHECK 1: --status ------------------------------------------------------
# The cheapest read-only probe in the app: it prints one boolean and draws
# nothing. If anything at all needs layer-shell, it is this.
rc, out = run_cli("--status")
check("1 --status answers with no gtk-layer-shell present",
      rc == 0 and "logged_in:" in out and not crashed_on_typelib(out),
      f"(rc={rc}, crashed={crashed_on_typelib(out)})")

# --- CHECK 2: the plaud:// OAuth handler ------------------------------------
# The auth_code is single-use: if this path dies before consuming it, the user
# must redo the whole browser login. A fake code is rejected by the backend,
# which is fine -- what is checked is that it got far enough to be rejected
# rather than dying at import time.
rc, out = run_cli("plaud://login?auth_code=NOLSCHK")
check("2 the plaud:// handler consumes the code with no gtk-layer-shell",
      rc == 0 and not crashed_on_typelib(out),
      f"(rc={rc}, crashed={crashed_on_typelib(out)})")

# --- CHECK 3: --login -------------------------------------------------------
# The next thing a user runs on a fresh machine -- i.e. exactly the machine
# most likely to be missing the package. Not asserted to open a browser here:
# only that it does not die at import.
rc, out = run_cli("--login")
check("3 --login runs with no gtk-layer-shell present",
      rc == 0 and not crashed_on_typelib(out),
      f"(rc={rc}, crashed={crashed_on_typelib(out)})")

# --- CHECK 4: the guard catches ValueError, not just ImportError -------------
# The heart of the defect. Asserts the import site handles a *ValueError*: a
# guard written as `except ImportError` looks correct, passes review, and still
# lets this escape. Driven in-process against a real start_session() so it
# tests the shipped code path, not a re-implementation of it.
probe = '''
import sys, types
sys.path.insert(0, %r)
import gi
_real = gi.require_version
def _blocked(ns, v):
    if ns == "GtkLayerShell":
        raise ValueError("Namespace GtkLayerShell not available")
    return _real(ns, v)
gi.require_version = _blocked
stub = types.ModuleType("requests")
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub
sys.argv = ["x"]
from plaud_linux import main as m
told = []
m.notify = lambda t, b: told.append(b)
m.ensure_login_or_prompt = lambda: True
m.pick_sources = lambda: (True, False)
ended = []
started = m.start_session(on_session_end=lambda: ended.append(1))
# Declined, the user was told in words, and `done` was NOT fired -- no session
# began, so there is none to end (main.py's stated contract for early returns).
print("RESULT", started is False, bool(told), ended == [])
''' % repo
env = dict(os.environ)
env["PLAUD_LINUX_HOME"] = home
try:
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                       text=True, timeout=90, cwd=repo, env=env)
    out4 = r.stdout + r.stderr
except subprocess.TimeoutExpired as e:
    out4 = (e.stdout or "") + (e.stderr or "")
check("4 a recording declines cleanly and tells the user, instead of crashing",
      "RESULT True True True" in out4,
      f"(crashed={crashed_on_typelib(out4)})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
