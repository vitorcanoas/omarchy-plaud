"""Checks for CAN-322: the session sidecar is written atomically, never rebuilt,
and records how the upload went.

Usage: python3 tests/test_sidecar.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

CHECK 1 is the one that matters, and it is the one the original suite could
not have expressed: the eleven sidecar checks that shipped this defect were
ALL single-threaded, and a single-threaded check of an atomicity fix proves
nothing -- write_text() truncate-then-write is only visible to a concurrent
reader. Measured on the pre-fix tree: 200/200 trials unparseable. After:
0/200.
"""
import json, os, pathlib, sys, tempfile, threading

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="sidecarchk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

import types
stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

from plaud_linux import audio, paths

# The pre-fix tree has neither helper. A missing API must go red as a FAIL
# line: a traceback reports no result at all, and run.py's per-file count is
# what would then catch it -- one indirection away from saying what broke.
def read_json(path):
    if not hasattr(audio, "read_json"):
        return "MISSING"
    return audio.read_json(path)

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


def make_rec(session):
    """A Recorder with the sidecar state populated, and no ffmpeg anywhere.

    __new__ rather than __init__: the constructor probes for a capture source
    and spawns nothing useful in a check, and every attribute the sidecar
    reads is set explicitly below.
    """
    r = audio.Recorder.__new__(audio.Recorder)
    r.session, r.ts, r.mode, r.mic = session, "20260902_120000", "system", False
    r.state, r.started_at, r.paused_accum, r.paused_at = "recording", None, 0.0, None
    r.final_path = paths.RECORDINGS / f"{session}.opus"
    r.meta_path = paths.RECORDINGS / f"{session}.json"
    r.segments_lost, r.upload = 0, None
    r.screenshots = [{"path": f"/s{i}.png", "t": i} for i in range(30)]
    r.notes = [{"text": "as anotações que o usuário digitou " * 10, "t": 3}]
    return r


# --- CHECK 1: a concurrent reader never sees a partial sidecar --------------
# Two differently-sized writers (the big 11-key sidecar and a small one) race
# one reader, 200 trials. This is the defect, and it is invisible to any
# single-threaded check.
N = 200
rec = make_rec("concurrent")
rec._save_meta()
corrupt, torn = 0, 0
for _ in range(N):
    stop, bad = threading.Event(), []
    def w_big():
        for _ in range(300):
            if stop.is_set():
                return
            rec._save_meta()
    def w_small():
        for _ in range(300):
            if stop.is_set():
                return
            s, n = rec.screenshots, rec.notes
            rec.screenshots, rec.notes = [], []
            rec._save_meta()
            rec.screenshots, rec.notes = s, n
    def rd():
        for _ in range(400):
            try:
                d = json.loads(rec.meta_path.read_text(encoding="utf-8"))
            except Exception:
                bad.append("unparseable")
                stop.set()
                return
            # Not just parseable: a truncated file can still parse as valid
            # JSON with keys missing, which is how an 11-key sidecar came back
            # with 4. Every write here carries all twelve -- the count is the
            # torn-file detector, so it tracks the sidecar's real key count
            # (12 since "flags" joined it for the mark ring, CAN-309 phase 1).
            if len(d) != 12:
                bad.append(f"torn:{len(d)}keys")
                stop.set()
                return
        stop.set()
    ts = [threading.Thread(target=w_big), threading.Thread(target=w_small),
          threading.Thread(target=rd)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if bad:
        corrupt += 1
        if bad[0].startswith("torn"):
            torn += 1
check("1 a concurrent reader never sees a partial sidecar",
      corrupt == 0, f"({corrupt}/{N} corrupt, {torn} torn-but-parseable)")

# --- CHECK 2: no temp files are left behind in the user's directory --------
strays = [p.name for p in paths.RECORDINGS.glob("*.tmp")] + \
         [p.name for p in paths.RECORDINGS.glob(".*tmp*")]
check("2 the atomic write leaves no temp file behind",
      not strays, f"(strays = {strays})")

# --- CHECK 3: an unreadable sidecar is preserved, NEVER rebuilt ------------
# This is the rule that protects the typed notes: the original repair logic
# reconstructed what it could infer, which is how they were destroyed.
bad_path = paths.RECORDINGS / "damaged.json"
bad_path.write_text('{"notes": [{"text": "irreplaceable"}], "session"', encoding="utf-8")
before = bad_path.read_text(encoding="utf-8")
got = read_json(bad_path)
kept = list(paths.RECORDINGS.glob("damaged.json.corrupt.*"))
check("3 an unreadable sidecar is preserved, not rebuilt",
      got is None and len(kept) == 1 and kept[0].read_text(encoding="utf-8") == before
      and not bad_path.exists(),
      f"(returned={got!r}, preserved={[p.name for p in kept]})")

# --- CHECK 4: a sidecar write never touches the .opus ----------------------
# "Never lose a recording" -- the sidecar path must be incapable of harming
# the audio, including when the sidecar itself is unreadable.
rec4 = make_rec("withaudio")
rec4.final_path.write_bytes(b"OpusHead" + b"\x00" * 4096)
sig = (rec4.final_path.read_bytes(), rec4.final_path.stat().st_size)
rec4._save_meta()
if hasattr(rec4, "set_upload"):
    rec4.set_upload(ok=False, error="boom")
rec4.meta_path.write_text("}{ not json", encoding="utf-8")
read_json(rec4.meta_path)
after = (rec4.final_path.read_bytes(), rec4.final_path.stat().st_size)
check("4 no sidecar operation ever touches the .opus",
      rec4.final_path.exists() and after == sig,
      f"({sig[1]} bytes before, {after[1]} after)")

# --- CHECK 5: the upload outcome is persisted and readable ----------------
rec5 = make_rec("outcome")
rec5._save_meta()
check("5a a sidecar with no upload yet carries no outcome",
      "upload" not in json.loads(rec5.meta_path.read_text(encoding="utf-8")),
      "")
d = d2 = {"upload": None, "notes": None}
if hasattr(rec5, "set_upload"):
    rec5.set_upload(ok=False, error="connection reset")
    d = json.loads(rec5.meta_path.read_text(encoding="utf-8"))
    rec5.set_upload(ok=True, file_id="abc123")
    d2 = json.loads(rec5.meta_path.read_text(encoding="utf-8"))
up, up2 = d.get("upload") or {}, d2.get("upload") or {}
ok_fail = (up.get("ok") is False and up.get("error") == "connection reset"
           and "at" in up)
# The notes must survive the outcome write -- that is the whole point.
notes_kept = d.get("notes") == rec5.notes
check("5b a failed upload is recorded on disk, with the notes intact",
      ok_fail and notes_kept, f"(upload={up!r})")
check("5c a later success updates the outcome",
      up2.get("ok") is True and up2.get("file_id") == "abc123",
      f"(upload={up2!r})")

# --- CHECK 6: main.py records the outcome on every terminal path ----------
import inspect
from plaud_linux import main as main_mod
src = inspect.getsource(main_mod._do_upload)
n_sites = src.count("_record_outcome(")
_rec_outcome = getattr(main_mod, "_record_outcome", None)
seen = []
class FakeRec:
    concat_failed = False
    def set_upload(self, **kw):
        seen.append(kw)
if _rec_outcome:
    _rec_outcome(FakeRec(), ok=True, file_id="z")
# A rec that cannot record its outcome must not break the upload path.
class Hostile:
    def set_upload(self, **kw):
        raise RuntimeError("disk full")
raised = False
try:
    if _rec_outcome:
        _rec_outcome(Hostile(), ok=False, error="x")
except Exception:
    raised = True
check("6 every terminal upload path records its outcome, and a failing "
      "write cannot break the upload",
      _rec_outcome is not None and n_sites >= 5
      and seen == [{"ok": True, "file_id": "z"}] and not raised,
      f"({n_sites} outcome sites in _do_upload, swallowed={not raised})")

print("\n" + ("ALL PASS" if all(results) else "SOME FAILED"))
sys.exit(0 if all(results) else 1)
