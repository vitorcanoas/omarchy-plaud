"""Checks for CAN-317: a session that recorded nothing must never upload an
EARLIER session's audio as its own.

Usage: python3 tests/test_staleupload.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

The mechanism, because a check that does not construct it proves nothing.
Recorder.__init__ de-duplicates `gravacao_<ts>` names against what is already
in recordings/ (audio.py) -- but ONLY when `name is None`. An explicit
`name=` skips that loop entirely, so `final_path` can already hold a previous
session's .opus before this session records a byte. _concat() then has two
exits that merge nothing and return None:

  - no segment produced a usable file  -> `if not segs: return None`
  - the concat itself failed           -> sets concat_failed, returns None

and stop() ignores that return value, handing back self.final_path regardless.
_do_upload() reads the ATTRIBUTE rec.final_path, never the return value, so on
the first of those two exits it found the older file, saw a plausible size,
saw concat_failed False -- and uploaded the wrong meeting under this session's
name. Nothing in any log, notification or sidecar said so; the user gets a
transcript of a different conversation and no signal at all. That is the whole
defect, and CHECK 1 and CHECK 2 construct exactly it.

Uploading the wrong audio is worse than failing loudly, so the fix REFUSES.
Principle II still binds the refusal: CHECK 3 and CHECK 4 assert it deletes
nothing -- the stale .opus and any segments stay on disk -- and CHECK 5 that
the user is actually told, rather than left assuming the file was sent.
"""
import os, pathlib, sys, tempfile, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="stalechk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

# A check that reaches the network is not measuring this defect, it is measuring
# the internet. Any call is an outright failure, not a skip.
stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

from plaud_linux import audio, paths

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


STALE = b"PREVIOUS SESSION AUDIO" * 500   # 11000 bytes, comfortably over the
                                          # 2000-byte floor _do_upload enforces


def make_rec(name, segments):
    """A stopped Recorder for session `name`, with no ffmpeg anywhere.

    __new__ rather than __init__: the constructor probes for a capture source
    and would spawn nothing useful here. Every attribute _concat() and
    _do_upload() read is set explicitly. This is also what lets the check pin
    the exact precondition -- an explicit `name=`, so no de-duplication ran.
    """
    r = audio.Recorder.__new__(audio.Recorder)
    r.session, r.ts, r.mode, r.mic = name, "20260902_120000", "system", False
    r.state, r.started_at, r.paused_accum, r.paused_at = "stopped", None, 0.0, None
    r.segdir = paths.RECORDINGS / f".seg_{name}"
    r.segdir.mkdir(parents=True, exist_ok=True)
    r.segments = segments
    r.final_path = paths.RECORDINGS / f"{name}.opus"
    r.meta_path = paths.RECORDINGS / f"{name}.json"
    r.screenshots, r.notes, r.upload = [], [], None
    r.concat_failed, r.segments_lost, r._merged = False, 0, False
    return r


# ---------------------------------------------------------------- CHECK 1
# The plain case: an earlier "reuniao" left a .opus behind, a new "reuniao"
# session produced NO segment at all (ffmpeg died on spawn -- a bad output
# path or a missing encoder leaves nothing, and start() sets state
# "recording" without ever looking). _concat() must not leave that older file
# looking like this session's result.
old = paths.RECORDINGS / "reuniao.opus"
old.write_bytes(STALE)
r1 = make_rec("reuniao", [])
r1._concat()
check("1 no segments at all: refuses rather than passing off the older file",
      r1.concat_failed,
      f"concat_failed={r1.concat_failed} (False here means _do_upload would "
      f"have uploaded {old.name}, which is a DIFFERENT session's audio)")

# ---------------------------------------------------------------- CHECK 2
# The variant that is worse, because it half-reports. Every segment was
# started but left 0 bytes: ffmpeg writes the Ogg only at a clean exit, so a
# SIGKILLed segment stays empty. segments_lost becomes non-zero, so the user
# IS warned -- but only that the audio is "shorter", while what actually gets
# uploaded is a whole different recording.
old2 = paths.RECORDINGS / "call.opus"
old2.write_bytes(STALE)
r2 = make_rec("call", [])
dead = r2.segdir / "seg_000.opus"
dead.write_bytes(b"")
r2.segments = [dead]
r2._concat()
check("2 all segments unusable: refuses, not just a 'shorter audio' warning",
      r2.concat_failed,
      f"concat_failed={r2.concat_failed} segments_lost={r2.segments_lost}")

# ---------------------------------------------------------------- CHECK 3
# Principle II. The refusal must not be implemented by deleting anything: the
# stale file is still the user's only copy of THAT session, and the empty
# segments are the only trace that this one was attempted.
check("3 refusal deletes nothing: the earlier .opus is still on disk",
      old.exists() and old.read_bytes() == STALE and old2.exists(),
      f"reuniao={old.exists()} call={old2.exists()} bytes_intact="
      f"{old.exists() and old.read_bytes() == STALE}")

check("4 refusal deletes nothing: this session's segment dir survives",
      dead.exists() and r2.segdir.exists(),
      f"segdir={r2.segdir.exists()} seg_000={dead.exists()}")

# ---------------------------------------------------------------- CHECK 5
# A silent refusal re-opens the defect from the other side: the user assumes
# it went up. _do_upload must both end the session and say something that does
# not claim the older file was sent.
import plaud_linux.main as main_mod

said = []
main_mod.notify = lambda t, b, **k: said.append(b)
main_mod._record_outcome = lambda rec, **k: None

ended = []
main_mod._do_upload(r1, lambda: ended.append(True))
msg = " ".join(said)
check("5 the user is told nothing was sent, and the session still ends",
      bool(ended) and "nada enviado" in msg.lower()
      and "não foi enviado" in msg.lower(),
      f"ended={bool(ended)} msg={msg!r}")

# ---------------------------------------------------------------- CHECK 6
# The fix must not turn every session into a refusal. A session whose single
# segment IS usable still merges and still uploads -- including over a stale
# file of the same name, which is the correct outcome there: this session
# really did produce that audio.
old3 = paths.RECORDINGS / "good.opus"
old3.write_bytes(STALE)
r3 = make_rec("good", [])
seg = r3.segdir / "seg_000.opus"
seg.write_bytes(b"FRESH SESSION AUDIO" * 500)
r3.segments = [seg]
out = r3._concat()
check("6 a session that did record still merges and is not refused",
      out == r3.final_path and not r3.concat_failed and r3._merged
      and r3.final_path.read_bytes().startswith(b"FRESH"),
      f"returned={out} concat_failed={r3.concat_failed} merged={r3._merged}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
