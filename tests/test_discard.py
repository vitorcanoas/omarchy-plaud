"""Checks for CAN-315: the discard/cancel recording flow.

Usage: python3 tests/test_discard.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).

The official client's copy is proven in docs/plaud-desktop/OFFICIAL-UI-SPEC.md
§6: a kebab menu on the recording control bar, one destructive "Discard
recording" item, then a confirm dialog -- and only on confirming does the
session stop-and-discard instead of stop-and-upload.

The correctness requirement that matters most here: a discard must remove
ALL THREE artefacts a session can leave on disk -- the concatenated .opus,
the .seg_<session>/ segment directory, and the sidecar .json -- or
library.scan() still finds one of them and "Envios recentes" shows a ghost
entry for a recording the user was told was gone.

CHECK 1-2 drive a real Recorder.start() (a real ffmpeg/pulse capture, same
pattern as test_markring.py) through Recorder.discard() and inspect the
filesystem directly -- not library.scan() -- because scan() silently
tolerating a missing artefact would make this check pass for the wrong
reason. CHECK 3 covers the case that most resembles the real bug this
ticket exists to prevent: a session that already went through one
pause/resume cycle, so it has already concatenated into final_path once
before being discarded mid-second-segment, exercising exactly the "some
artefacts already exist, some don't" case that a naive "delete the
segdir only" fix would miss.
"""
import json, os, pathlib, sys, tempfile, time

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="discardchk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

# Before importing anything that could reach the network. A check that
# silently uploads to api.plaud.ai is not a check.
import types
stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

from plaud_linux import audio, paths

REC = paths.RECORDINGS

results = []
def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# --- CHECK 1: a fresh session (no pause yet) leaves nothing behind ----------
# Forcing _src rather than depending on real hardware, like test_markring.py --
# this only needs SOME ffmpeg process running, not real audio content.
rec1 = audio.Recorder(mode="system", name="discard_fresh")
rec1._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
rec1.start()
time.sleep(0.3)
seg_path = rec1.segments[0]
had_segdir = rec1.segdir.is_dir()
had_sidecar = rec1.meta_path.exists()
rec1.discard()
check("1 discard on a fresh session removes segdir, sidecar, and leaves no .opus",
      not rec1.segdir.exists() and not rec1.meta_path.exists()
      and not rec1.final_path.exists() and not seg_path.exists(),
      f"pre: segdir={had_segdir} sidecar={had_sidecar} | "
      f"post: segdir={rec1.segdir.exists()} sidecar={rec1.meta_path.exists()} "
      f"final={rec1.final_path.exists()} seg={seg_path.exists()}")

check("1b the recorder process is actually gone",
      rec1.proc is None, f"proc={rec1.proc}")

check("1c state reflects the discard, not a stopped/uploadable session",
      rec1.state == "discarded", f"state={rec1.state!r}")


# --- CHECK 2: a session already merged once (pause -> resume -> discard) ----
# Exercises the case a naive fix (only clean the segdir) would miss: after one
# pause/resume cycle, _concat() has already produced final_path and emptied
# segments/segdir for the FIRST segment, so at discard time the survivors are
# a mix -- final_path exists from the earlier merge, plus a fresh segdir with
# the second segment's in-progress file. Both must go.
rec2 = audio.Recorder(mode="system", name="discard_after_pause")
rec2._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
rec2.start()
time.sleep(0.3)
rec2.pause()
rec2.resume()
time.sleep(0.3)
final_existed = rec2.final_path.exists()   # first segment already concatenated
segdir_existed = rec2.segdir.is_dir()      # second segment's dir
rec2.discard()
check("2 discard after a pause/resume cycle removes the already-concatenated "
      ".opus AND the new segment's dir",
      not rec2.final_path.exists() and not rec2.segdir.exists()
      and not rec2.meta_path.exists(),
      f"pre: final={final_existed} segdir={segdir_existed} | "
      f"post: final={rec2.final_path.exists()} segdir={rec2.segdir.exists()} "
      f"sidecar={rec2.meta_path.exists()}")


# --- CHECK 3: nothing survives ANYWHERE under RECORDINGS for that session ---
# The ghost-entry failure mode is library.scan() walking the whole RECORDINGS
# tree, not just the three paths the Recorder happens to remember -- so this
# check does an independent directory walk rather than re-asking the Recorder
# about itself.
rec3 = audio.Recorder(mode="system", name="discard_scan")
rec3._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
rec3.start()
time.sleep(0.3)
rec3.discard()
leftover = [p.name for p in REC.iterdir() if "discard_scan" in p.name]
check("3 no file or directory mentioning the session survives under RECORDINGS",
      leftover == [], f"leftover={leftover}")


# --- CHECK 4: discard() is safe to call on a session with no audio yet ------
# start() then an immediate discard, before ffmpeg has necessarily produced
# any bytes -- must not raise, and must still clean up whatever partial state
# exists (segdir at minimum, since Recorder.__init__ creates it unconditionally
# and _save_meta() runs before start() is even called).
rec4 = audio.Recorder(mode="system", name="discard_immediate")
rec4._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
raised = None
try:
    rec4.discard()
except Exception as e:
    raised = e
check("4 discard before start() ever ran does not raise and cleans the sidecar",
      raised is None and not rec4.meta_path.exists() and not rec4.segdir.exists(),
      f"raised={raised!r} sidecar={rec4.meta_path.exists()} segdir={rec4.segdir.exists()}")


passed = sum(results)
total = len(results)
print(f"\n{passed}/{total} passed")
if passed != total:
    sys.exit(1)
