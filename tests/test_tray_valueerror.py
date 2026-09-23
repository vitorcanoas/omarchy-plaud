"""Checks that the app survives a tray it cannot build.

Usage: python3 tests/test_tray_valueerror.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

Two mechanisms, one invariant. Checks 1-2 keep the original: AyatanaAppIndicator3
is a system typelib, and tray.py USED TO call require_version() for it at module
scope. Checks 3-5 cover what replaced it -- tray.py now publishes its own
StatusNotifierItem and needs Pillow to decode the bundled PNGs into IconPixmap
bytes. The library changed; the rule did not: an unusable tray is reported, never
a crash, and never a silently blank icon.

Why the original mechanism still matters below: A missing typelib raises ValueError,
NOT ImportError -- so the dual-import shim every module carries does not catch
it. The import is deferred to the exact else branch (bare `plaud-linux` with no
args), so it only crashes the bare launch, not --status/--login/plaud://. None of
those paths use a tray. The launcher sends stdout to app.log, so a GUI launch
showed the user nothing at all.

Nothing else in tests/ would catch this: every other check runs in a process
where the typelib is present, which is exactly the machine the bug is invisible
on. So these checks spawn real subprocesses with that ONE namespace blocked,
which is the only way to reproduce the missing-package machine on this one.
"""
import os, sys, tempfile, pathlib, subprocess

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="traychk-")

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# Blocks exactly one namespace and leaves every other require_version() alone,
# so what the child proves is "AyatanaAppIndicator3 is missing", not "gi is broken".
# sitecustomize runs before the app imports anything, which is what makes this
# equivalent to the typelib genuinely not being installed.
shimdir = tempfile.mkdtemp(prefix="trayshim-")
pathlib.Path(shimdir, "sitecustomize.py").write_text(
    'import gi\n'
    '_real = gi.require_version\n'
    'def _blocked(namespace, version):\n'
    '    if namespace == "AyatanaAppIndicator3":\n'
    '        raise ValueError("Namespace AyatanaAppIndicator3 not available")\n'
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
# because the bare launch may exit with different codes in various scenarios.
def crashed_on_typelib(out):
    return "Traceback" in out and "AyatanaAppIndicator3" in out


# --- CHECK 1: --status remains unaffected -----------------------------------
# The cheapest read-only probe in the app: it prints one boolean and draws
# nothing. This should work even when AyatanaAppIndicator3 is blocked, since
# --status returns before reaching the tray import branch.
rc, out = run_cli("--status")
check("1 --status works even with AyatanaAppIndicator3 blocked",
      rc == 0 and "logged_in:" in out and not crashed_on_typelib(out),
      f"(rc={rc}, crashed={crashed_on_typelib(out)})")

# --- CHECK 2: the plaud:// OAuth handler also unaffected --------------------
# Like --status, this should return before the tray branch. The auth_code is
# single-use, so if this path dies, the user must redo browser login.
rc, out = run_cli("plaud://login?auth_code=TRAYCHECK")
check("2 the plaud:// handler works with AyatanaAppIndicator3 blocked",
      rc == 0 and not crashed_on_typelib(out),
      f"(rc={rc}, crashed={crashed_on_typelib(out)})")

# --- CHECK 3: a tray that cannot build does not take the app down ----------
#
# The original checks 3-5 blocked the AyatanaAppIndicator3 namespace, because
# tray.py used to call require_version() for it at module scope. tray.py no
# longer imports that library at all -- it publishes its own
# org.kde.StatusNotifierItem and serves IconPixmap directly, since
# AyatanaAppIndicator3's binding has no way to set a pixmap and the bar on this
# machine ignores IconThemePath entirely.
#
# So those checks could no longer fail for their stated reason. Worse, they had
# stopped testing anything: with nothing to block, main() ran on to a real
# Gtk.main() and sat there until the 90 s timeout, which the harness reported as
# a failure with a completely unrelated cause.
#
# The INVARIANT they were defending is not obsolete, only its mechanism is: a
# tray that cannot be constructed must be reported to the user and must not
# crash the process. Today the way that happens is a missing Pillow -- the one
# new dependency the pixmap path introduced -- so that is what these block.
def run_probe(body, blocked_module=None, timeout=60):
    """Run a probe with one module import forced to fail."""
    shim = ""
    if blocked_module:
        shim = (
            "import builtins\n"
            "_real_import = builtins.__import__\n"
            "def _blocked(name, *a, **kw):\n"
            f"    if name == {blocked_module!r} or name.startswith({blocked_module!r} + '.'):\n"
            f"        raise ImportError('No module named ' + repr(name))\n"
            "    return _real_import(name, *a, **kw)\n"
            "builtins.__import__ = _blocked\n")
    src = f"import sys\nsys.path.insert(0, {repo!r})\n" + shim + body
    env = dict(os.environ)
    env["PLAUD_LINUX_HOME"] = home
    try:
        r = subprocess.run([sys.executable, "-c", src], capture_output=True,
                           text=True, timeout=timeout, cwd=repo, env=env)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        # Decode defensively: TimeoutExpired carries BYTES even when the call
        # asked for text=True, so the obvious concatenation raises TypeError and
        # hides the real result. That bug was live in this file.
        def _dec(x):
            if x is None:
                return ""
            return x.decode("utf-8", "replace") if isinstance(x, bytes) else x
        return "TIMEOUT " + _dec(e.stdout) + _dec(e.stderr)


out3 = run_probe(
    "from plaud_linux import tray\n"
    "print('IMPORTED_OK')\n",
    blocked_module="PIL")
check("3 tray.py imports even with Pillow missing (the import is lazy)",
      "IMPORTED_OK" in out3 and "Traceback" not in out3,
      f"(output={out3.strip()[-200:]})")

# --- CHECK 4: the failure surfaces where main.py can catch it ---------------
# Lazy is only half the contract. The decode must still FAIL, and fail at tray
# construction, so main.py's guard around the tray import branch has something
# to catch and can fall back to a no-tray run. Silently serving a blank icon
# would be the worse outcome -- an invisible tray is this codebase's oldest bug.
out4 = run_probe(
    "from plaud_linux import tray\n"
    "try:\n"
    "    tray._load_pixmap(tray.ICON_IDLE)\n"
    "    print('RESULT no-error')\n"
    "except ImportError:\n"
    "    print('RESULT ImportError')\n"
    "except Exception as e:\n"
    "    print('RESULT ' + type(e).__name__)\n",
    blocked_module="PIL")
check("4 the missing dependency raises at pixmap decode, not silently blank",
      "RESULT ImportError" in out4,
      f"(output={out4.strip()[-200:]})")

# --- CHECK 5: the read-only CLI paths never touch the tray at all -----------
# The reason the guard is deferred rather than at module scope. --status and the
# plaud:// callback run as SEPARATE PROCESSES while a recording is in flight,
# draw no tray, and must not care what the tray needs. Checks 1 and 2 prove this
# for a blocked typelib; this proves it for the new dependency.
out5 = run_probe(
    "import sys\n"
    "sys.argv = ['plaud-linux', '--status']\n"
    "from plaud_linux import main as m\n"
    "m.main()\n"
    "print('SURVIVED')\n",
    blocked_module="PIL")
check("5 --status survives with Pillow missing",
      "SURVIVED" in out5 and "logged_in:" in out5 and "Traceback" not in out5,
      f"(output={out5.strip()[-200:]})")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
