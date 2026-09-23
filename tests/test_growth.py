"""Check for CAN-314 item 10: unbounded on-disk growth.

Usage: python3 tests/test_growth.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

Two things grow without limit in ~/.local/share/plaud-linux:
  - logs/app.log, appended by the launcher with `>>` and never rotated
  - logs/<session>.log, one per recording, never pruned
  - recordings/screenshots/<session>/, one dir per session, never pruned

None of this is urgent in bytes -- 84 KB of logs today -- but none of it has
a ceiling either, and the app has no other cleanup path. The fix must bound
them WITHOUT ever touching a .opus: losing a recording is the one thing this
codebase promises not to do.
"""
import os, sys, tempfile, pathlib, types, time

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="growthchk-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

from plaud_linux import paths

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")

prune = getattr(paths, "prune_old_logs", None)
check("0 a pruning entry point exists",
      callable(prune), "(paths.prune_old_logs)" if prune else "(missing)")
if not callable(prune):
    print()
    print("SOME FAILED")
    sys.exit(1)

LOGS = paths.LOGS
RECS = paths.RECORDINGS
SHOTS = RECS / "screenshots"

def reset():
    for d in (LOGS, SHOTS):
        if d.exists():
            for p in sorted(d.rglob("*"), reverse=True):
                # is_file() follows symlinks, so a dangling or dir-pointing
                # link took the rmdir() branch and raised NotADirectoryError.
                # Check the link itself first.
                if p.is_symlink() or p.is_file():
                    p.unlink()
                else:
                    p.rmdir()
    LOGS.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

def age(p, days):
    t = time.time() - days * 86400
    os.utime(p, (t, t))

# --- CHECK 1: old per-session logs are pruned, recent ones kept -------------
reset()
old = LOGS / "gravacao_20250101_000000.log"; old.write_text("old"); age(old, 60)
new = LOGS / "gravacao_20260803_000000.log"; new.write_text("new")
prune()
check("1 old session logs pruned, recent kept",
      not old.exists() and new.exists(),
      f"(old gone={not old.exists()}, new kept={new.exists()})")

# --- CHECK 2: app.log is truncated when it grows past its cap ---------------
reset()
app = LOGS / "app.log"
app.write_bytes(b"x" * (4 * 1024 * 1024))       # 4 MB, over any sane cap
prune()
size = app.stat().st_size if app.exists() else -1
# Must still BE app.log: the launcher holds an append fd on this path opened
# before main() ran, so renaming it would send this session's output into the
# rotated file instead.
rotated = (LOGS / "app.log.1").exists()
check("2 app.log is bounded, in place",
      app.exists() and size < 4 * 1024 * 1024 and not rotated,
      f"(exists={app.exists()}, size now {size}, rotated-away={rotated})")

# --- CHECK 2b: a concurrent session keeps its recent lines ------------------
# There is no single-instance lock yet (CAN-314 item 4), so two sessions can
# overlap and B's prune hits A's live log. Truncating to zero destroyed the
# run being tailed; keeping a tail bounds that to old lines nobody reads.
reset()
app = LOGS / "app.log"
app.write_bytes(b"ANCIENT\n" * 10 + b"y" * (3 * 1024 * 1024) + b"\nRECENT-LINE-A\n")
writer = open(app, "a")            # session A's live O_APPEND fd
prune()
writer.write("LINE-AFTER-PRUNE\n")
writer.flush()
writer.close()
body = app.read_bytes()
check("2b an overlapping session keeps its recent log lines",
      b"RECENT-LINE-A" in body and b"LINE-AFTER-PRUNE" in body
      and b"\x00" not in body and len(body) < 1024 * 1024,
      "(recent kept=%s, post-prune write kept=%s, NUL hole=%s, size=%d)"
      % (b"RECENT-LINE-A" in body, b"LINE-AFTER-PRUNE" in body,
         b"\x00" in body, len(body)))

# --- CHECK 3: a .opus is NEVER deleted, however old ------------------------
# The load-bearing check. Pruning must not be able to reach the audio.
reset()
opus = RECS / "gravacao_20200101_000000.opus"; opus.write_bytes(b"OggS" + b"z" * 900)
age(opus, 3650)                                   # ten years old
meta = RECS / "gravacao_20200101_000000.json"; meta.write_text("{}"); age(meta, 3650)
prune()
check("3 a ten-year-old recording is never pruned",
      opus.exists(), f"(opus still there={opus.exists()})")
check("3b its metadata sidecar is kept too",
      meta.exists(), f"(json still there={meta.exists()})")

# --- CHECK 4: old screenshot dirs pruned, recent kept ----------------------
reset()
oldshot = SHOTS / "0001"; oldshot.mkdir(parents=True)
f1 = oldshot / "shot_000005_x.png"; f1.write_bytes(b"\x89PNG" + b"a" * 200)
age(f1, 60); age(oldshot, 60)
newshot = SHOTS / "0099"; newshot.mkdir(parents=True)
f2 = newshot / "shot_000005_y.png"; f2.write_bytes(b"\x89PNG" + b"b" * 200)
prune()
check("4 old screenshot dirs pruned, recent kept",
      not oldshot.exists() and newshot.exists(),
      f"(old gone={not oldshot.exists()}, new kept={newshot.exists()})")

# --- CHECK 5: pruning never raises, even on a hostile tree -----------------
# It runs at startup; an exception here would stop the app from recording,
# which trades a disk-space nuisance for the one failure that actually costs
# the user something.
reset()
weird = LOGS / "subdir"; weird.mkdir()
(weird / "nested.log").write_text("x")
try:
    prune()
    raised = None
except Exception as e:
    raised = repr(e)
check("5 pruning never raises", raised is None, f"(raised={raised})")

# --- CHECK 6: --status / plaud:// must NOT prune -------------------------
# The review's HIGH finding. main() ran prune before arg dispatch, so the
# plaud:// callback -- a SEPARATE process the browser launches mid-session --
# truncated the live session's app.log out from under it. Exercise main()'s
# real dispatch, not prune_old_logs() directly, which is what let this through.
reset()
app = LOGS / "app.log"
app.write_bytes(b"SESSION-A-DIAGNOSTIC\n" + b"x" * (3 * 1024 * 1024))
before = app.stat().st_size

import types as _t
sys.modules.setdefault("gi", __import__("gi"))
from plaud_linux import main as main_mod
main_mod.notify = lambda *a, **k: None

real_client = main_mod.plaud_api.PlaudClient
class _FakeClient:
    def is_logged_in(self): return True
main_mod.plaud_api.PlaudClient = lambda *a, **k: _FakeClient()
real_argv = sys.argv[:]
try:
    sys.argv = ["plaud-linux", "--status"]
    main_mod.main()
finally:
    sys.argv = real_argv
    main_mod.plaud_api.PlaudClient = real_client

after = app.stat().st_size
survived = b"SESSION-A-DIAGNOSTIC" in app.read_bytes()
check("6 --status does not truncate a live session's app.log",
      after == before and survived,
      f"(size {before} -> {after}, diagnostic line kept={survived})")

# --- CHECK 7: symlinks are skipped, not followed --------------------------
# Makes the docstring's absolute claim ("nothing here can reach a recording")
# literally true instead of merely likely.
reset()
victim = RECS / "gravacao_20200202_000000.opus"
victim.write_bytes(b"OggS" + b"v" * (3 * 1024 * 1024))
link = LOGS / "app.log"
if link.exists():
    link.unlink()
link.symlink_to(victim)
prune()
check("7 app.log as a symlink does not truncate its target",
      victim.stat().st_size > 3 * 1024 * 1024,
      f"(victim recording is now {victim.stat().st_size} bytes)")

reset()
# Age the symlink TARGET, not the link: d.stat() follows, so a fresh target
# makes the age gate skip the entry before symlink-following is ever reached.
# Without this the check passes on the vulnerable code too, proving nothing.
decoy = RECS / "olddir"
decoy.mkdir()
png_victim = decoy / "keepme.png"
png_victim.write_bytes(b"\x89PNG" + b"k" * 100)
age(png_victim, 60)
age(decoy, 60)
d = SHOTS / "0001"
d.symlink_to(decoy)
prune()
check("7b a screenshots symlink does not delete inside recordings/",
      png_victim.exists(), f"(png in recordings/ survived={png_victim.exists()})")

# --- CHECK 8: a HARDLINKED app.log does not damage its other name ---------
# is_symlink() returns False for a hardlink, so the symlink guard does not see
# this at all. Measured before the fix: a 3 MB recording left at 256 KB.
reset()
victim2 = RECS / "gravacao_20200303_000000.opus"
victim2.write_bytes(b"OggS" + b"h" * (3 * 1024 * 1024))
hard = LOGS / "app.log"
if hard.exists():
    hard.unlink()
os.link(victim2, hard)
prune()
check("8 a hardlinked app.log does not truncate the recording",
      victim2.stat().st_size > 3 * 1024 * 1024,
      f"(recording is now {victim2.stat().st_size} bytes)")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
