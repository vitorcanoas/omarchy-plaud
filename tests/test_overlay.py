"""Checks for the three fixes applied to night/overlay.

Usage: python3 tests/test_overlay.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Each check prints PASS/FAIL. They must FAIL on the pre-fix tree.
"""
import os, sys, time, subprocess, tempfile, pathlib

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="plaudchk-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from plaud_linux import overlay as ov_mod


class FakeRec:
    """Stands in for Recorder: records what got attached, nothing else."""
    def __init__(self):
        self.state = "recording"
        self.screenshots = []
        self.notes = []
        self.session = "chk"
        self.final_path = pathlib.Path(home) / "chk.opus"
    def start(self): pass
    def stop(self):
        self.state = "stopped"
        return self.final_path
    def elapsed(self): return 5
    def add_screenshot(self, p): self.screenshots.append(p)


def make_overlay():
    """Build an Overlay without letting it construct a real Recorder/ffmpeg."""
    real = ov_mod.audio.Recorder
    ov_mod.audio.Recorder = lambda **kw: FakeRec()
    try:
        o = ov_mod.Overlay(mode="system", on_stop=lambda rec: seen.append(len(rec.screenshots)))
    finally:
        ov_mod.audio.Recorder = real
    return o


results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# --- CHECK 1: a pending tool is killed on destroy, not left orphaned ---------
seen = []
o = make_overlay()
o.shots_dir = pathlib.Path(home); o.shots_dir.mkdir(exist_ok=True)
o._shot_pending = True
o._shot_out = (pathlib.Path(home) / "never.png", 3)
proc = subprocess.Popen(["sleep", "30"])
o._shot_proc = proc
o._shot_watch = GLib.child_watch_add(GLib.PRIORITY_DEFAULT, proc.pid, lambda *a: None)
o._remove_tick()
time.sleep(0.4)
alive = proc.poll() is None
check("1 pending screenshot tool is killed on destroy",
      not alive, f"(child alive after destroy = {alive})")
if alive:
    proc.kill()
o.destroy()

# --- CHECK 2: recovery attach respects _shots_taken -------------------------
seen = []
o = make_overlay()
o.shots_dir = pathlib.Path(home)
shot = pathlib.Path(home) / "late.png"
shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)   # non-empty, looks written
o._shot_pending = True
o._shot_out = (shot, 7)
p2 = subprocess.Popen(["true"]); p2.wait()
o._shot_watch = GLib.child_watch_add(GLib.PRIORITY_DEFAULT, p2.pid, lambda *a: None)
o._shot_proc = None
o._shots_taken = True          # simulate: on_stop already took the list
o.rec.state = "stopped"
o._remove_tick()
check("2 no attach once on_stop has taken the list",
      len(o.rec.screenshots) == 0,
      f"(attached {len(o.rec.screenshots)}, expected 0)")
o.destroy()

# --- CHECK 3: recovery STILL works on the normal Stop path ------------------
# The guard must not break the case it was built for: destroy() runs before
# on_stop(), so a shot recovered there is still uploaded.
seen = []
o = make_overlay()
o.shots_dir = pathlib.Path(home)
shot3 = pathlib.Path(home) / "recovered.png"
shot3.write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 64)
o._shot_pending = True
o._shot_out = (shot3, 9)
p3 = subprocess.Popen(["true"]); p3.wait()
o._shot_watch = GLib.child_watch_add(GLib.PRIORITY_DEFAULT, p3.pid, lambda *a: None)
o._shot_proc = None
o._do_stop()                    # the real Stop path
check("3 recovery still reaches the upload on the real Stop path",
      seen and seen[0] == 1, f"(on_stop saw {seen[0] if seen else 'nothing'}, expected 1)")

# --- CHECK 4: _shot_pending cleared if child_watch_add raises ---------------
seen = []
o = make_overlay()
o.shots_dir = pathlib.Path(home)
orig = GLib.child_watch_add
GLib.child_watch_add = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
# Record the pid at spawn, because the code under test clears _shot_proc on this
# path -- reading it afterwards yields None and makes 4b assert nothing at all.
spawned = []
real_popen = subprocess.Popen
def _spy_popen(*a, **k):
    pr = real_popen(*a, **k)
    spawned.append(pr.pid)
    return pr
subprocess.Popen = _spy_popen
try:
    o._shot_next([["sleep", "30"]], pathlib.Path(home) / "x.png", 1)
    raised = False
except Exception as e:
    raised = repr(e)
finally:
    GLib.child_watch_add = orig
    subprocess.Popen = real_popen
check("4 screenshot button not dead if child_watch_add fails",
      (not raised) and o._shot_pending is False,
      f"(raised={raised}, pending={o._shot_pending})")

# 4b: and the child nobody is listening to must not be left running.
#
# Asks about the ONE pid this check spawned, rather than pgrep'ing for
# `^sleep 30$`. That pattern matches across the whole machine, so a sibling
# worktree running these same checks concurrently handed this check a "stray"
# it never spawned -- and the loop below then SIGKILLed another run's process.
# Measured: one such pgrep returned two pids, only one of them ours. Same trap
# as the instance checks fighting over a single bus name; the fix is the same,
# which is to identify the subject by something this run owns.
#
# The pid is the honest handle here, and it stays honest because the command is
# still the plain `sleep 30` the overlay itself Popen's: wrapping it in a shell
# to carry a marker would leave proc.kill() killing the wrapper while the real
# sleep survives, which is the failure this check exists to catch.
#
# Verified both ways rather than assumed, since three earlier spellings of this
# check passed against a tree whose proc.kill() had been deliberately removed.
time.sleep(0.3)
shot_pid = spawned[0] if spawned else None
strays = []
if shot_pid is not None and pathlib.Path(f"/proc/{shot_pid}").exists():
    # Alive, or a zombie already reaped by the caller? Only the former leaks.
    st = pathlib.Path(f"/proc/{shot_pid}/stat").read_text().rsplit(") ", 1)[-1]
    if st.split()[0] != "Z":
        strays = [str(shot_pid)]
# No pid means nothing was spawned, so there was never anything to leak and the
# assertion below would hold vacuously. Fail instead: a check that quietly stops
# checking is the failure mode this whole exercise was about.
check("4b unwatched child is killed, not orphaned",
      shot_pid is not None and not strays,
      f"(stray pids: {strays})" if shot_pid is not None
      else "(nothing was spawned -- check did not exercise the kill path)")
for pid in strays:
    subprocess.run(["kill", "-9", pid], capture_output=True)
o.destroy()

# --- CHECK 5: the tool chain actually writes an image on this machine -------
# The point of the check is that it is machine-honest: it asserts a real PNG
# appeared, not that a command was spawned. On the pre-grim tree this goes red
# on Arch/Wayland because flameshot and gnome-screenshot are both absent, so
# the chain exhausts to _shot_fail() and no file is ever written.
#
# The full-screen leg is used deliberately: the region leg needs an interactive
# drag, which no automated check can supply. What that leaves unproven is
# stated in the report -- slurp's real ESC behaviour still needs a human.
seen = []
o = make_overlay()
o.shots_dir = pathlib.Path(home)
shot5 = pathlib.Path(home) / "grim_full.png"
shot5.unlink(missing_ok=True)
o._shot_pending = True
o._shot_out = (shot5, 4)
o._shot_next([[]], shot5, 4)          # the full-screen fallback leg
size5 = shot5.stat().st_size if shot5.exists() else 0
png5 = shot5.exists() and shot5.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
check("5 the screenshot chain writes a real PNG on this machine",
      png5 and size5 > 10000 and o.rec.screenshots == [shot5],
      f"(png={png5}, size={size5}, attached={len(o.rec.screenshots)})")
o.destroy()

def slurp_like(stderr_msg):
    """A finished process that failed the way slurp does: exit 1, empty stdout,
    one line on stderr. Real pipes, so _shot_done reads them as it would slurp's."""
    p = subprocess.Popen([sys.executable, "-c",
                          f"import sys; sys.stderr.write({stderr_msg!r} + chr(10)); sys.exit(1)"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    return p


# --- CHECK 6: a DECLINED region captures nothing ----------------------------
# The user pressed ESC or right-clicked. slurp then exits 1 with empty stdout
# and prints exactly "selection cancelled" on stderr (slurp 1.5.0 main.c:1076;
# ESC and right-click both land there via result 0x0). Measured on this box:
# every other rc=1 case prints a different, pre-event-loop message instead
# ("failed to create display", "-p and -r cannot be used together"), which is
# what makes declined separable from broken.
#
# The stand-in is a real process that behaves exactly as slurp does on ESC:
# exit 1, nothing on stdout, "selection cancelled" on stderr. A real
# interactive cancel needs a human keypress -- slurp holds an exclusive
# keyboard grab that neither wtype nor hyprctl can inject through on this
# compositor -- so the process is driven, not the keyboard.
#
# Pre-fix this goes red: empty geometry chained to the full-screen leg, so a
# 3000x1920 grim capture was written and attached, and the user was told
# "Screenshot salvo" about an image they had explicitly declined.
seen = []
o = make_overlay()
o.shots_dir = pathlib.Path(home)
notes6 = []
o._notify = lambda t, b: notes6.append(b)
shot6 = pathlib.Path(home) / "declined.png"
shot6.unlink(missing_ok=True)
o._shot_pending = True
o._shot_out = (shot6, 7)
p6 = slurp_like("selection cancelled")        # what slurp writes on ESC
o._shot_done(p6, [["slurp"], []], shot6, 7)
check("6 a declined region captures and attaches nothing",
      (not shot6.exists()) and o.rec.screenshots == [] and o._shot_pending is False,
      f"(file={shot6.exists()}, attached={len(o.rec.screenshots)}, "
      f"pending={o._shot_pending}, notify={notes6})")

# 6b: and it must not claim anything was saved.
check("6b a declined region is not reported as saved",
      not any("salvo" in n for n in notes6), f"(notify={notes6})")
o.destroy()

# --- CHECK 7: a BROKEN region tool still falls back to full screen ----------
# Declined and broken are different conditions. If slurp is absent or cannot
# reach the compositor, denying the user a screenshot entirely would be a
# regression -- the fallback exists for exactly that. stderr is what tells
# them apart, so a broken slurp must still reach grim.
seen = []
o = make_overlay()
o.shots_dir = pathlib.Path(home)
shot7 = pathlib.Path(home) / "broken_fallback.png"
shot7.unlink(missing_ok=True)
o._shot_pending = True
o._shot_out = (shot7, 8)
p7 = slurp_like("failed to create display")   # slurp with no compositor
o._shot_done(p7, [["slurp"], []], shot7, 8)
png7 = shot7.exists() and shot7.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
check("7 a broken region tool still falls back to full screen",
      png7 and o.rec.screenshots == [shot7],
      f"(png={png7}, attached={len(o.rec.screenshots)})")
o.destroy()

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
