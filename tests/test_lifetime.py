"""Checks for CAN-314 item 1: session end is no longer process exit.

Usage: python3 tests/test_lifetime.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

The point of the refactor is that on_stop() can end a session WITHOUT quitting
the loop. Pre-fix that was impossible: on_stop hardcoded Gtk.main_quit.
"""
import os, sys, tempfile, pathlib, threading, time

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp(prefix="lifechk-")
sys.path.insert(0, repo)

import types
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

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


class Rec:
    """A recording that fails concat: takes on_stop's first early-return path."""
    def __init__(self, home):
        self.concat_failed = True
        self.segments_lost = 0
        self.segdir = pathlib.Path(home)
        self.final_path = pathlib.Path(home) / "x.opus"
        self.screenshots, self.notes = [], []


# --- CHECK 1: on_stop accepts a `done` callback and calls it ----------------
import inspect
sig = list(inspect.signature(main_mod.on_stop).parameters)
check("1 on_stop takes a session-end callback",
      sig == ["rec", "done"], f"(signature = {sig})")

# --- CHECK 2: a session can end WITHOUT quitting the loop -------------------
# This is the whole point. Run a real Gtk.main(), end a session with a `done`
# that is not main_quit, and prove the loop is still alive afterwards.
if sig == ["rec", "done"]:
    home = os.environ["PLAUD_LINUX_HOME"]
    main_mod.notify = lambda *a, **k: None      # keep the desktop quiet
    fired = []
    still_alive = []

    def scenario():
        main_mod.on_stop(Rec(home), lambda: fired.append(True))
        # If on_stop quit the loop, main_level() drops to 0 here.
        still_alive.append(Gtk.main_level())
        Gtk.main_quit()                          # end the test ourselves
        return False

    GLib.idle_add(scenario)
    GLib.timeout_add(4000, lambda: (Gtk.main_quit(), False)[1])   # deadlock guard
    Gtk.main()

    check("2 session ends without quitting the loop",
          fired == [True] and still_alive == [1],
          f"(done fired={fired}, main_level after={still_alive})")
else:
    check("2 session ends without quitting the loop", False, "(skipped: bad signature)")

# --- CHECK 3: overlay.run() does not own the loop ---------------------------
# Parse the AST so docstrings and comments cannot fool the check.
import ast, textwrap

def calls_in(tree):
    """Every dotted call name actually invoked in this AST."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                out.append(f"{f.value.id}.{f.attr}")
    return out

from plaud_linux import overlay as ov_mod
run_tree = ast.parse(textwrap.dedent(inspect.getsource(ov_mod.run)))
run_calls = calls_in(run_tree)
check("3 overlay.run() no longer runs the GTK loop",
      "Gtk.main" not in run_calls, f"(calls = {run_calls})")

# --- CHECK 4: exit policy stated exactly once in main.py --------------------
# Counts real references to Gtk.main_quit in code (as a call or passed as a
# value), never in a docstring or comment.
mtree = ast.parse(pathlib.Path(repo, "plaud_linux", "main.py").read_text())
refs = [n for n in ast.walk(mtree)
        if isinstance(n, ast.Attribute) and n.attr == "main_quit"
        and isinstance(n.value, ast.Name) and n.value.id == "Gtk"]
check("4 main.py references Gtk.main_quit exactly once",
      len(refs) == 1, f"({len(refs)} site(s), lines {[n.lineno for n in refs]})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
