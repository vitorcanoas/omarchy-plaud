"""Checks for CAN-315 item 5: the recording lifecycle loop closes.

Usage: python3 tests/test_library.py [repo]
Defaults to this checkout; pass a path to run against another tree.

Principle II keeps a failed upload's .opus in recordings/ and tells the user.
Nothing led back from there -- no UI listed it, nothing resent it -- so every
failed upload became silent manual work. library.py answers "what is on disk
and was it sent?", and main.resend() sends it.

The checks that matter most here, and why they are the ones written:

CHECK 3 is the bug this suite already found in the feature it tests. The first
implementation filtered loose segments on the same >=2000 bytes the uploader
rejects on, and that hid one of the three REAL orphaned recordings on this
machine: .seg_gravacao_20260902_232546/seg_000.opus is 1.6 KB, so the session
scored "empty" and was dropped from the listing entirely. A list that silently
omits the user's audio is worse than no list -- it asserts there is nothing
there. Small is not absent.

CHECK 8 is the one a single-threaded suite is structurally incapable of
expressing, and it exists because of what happened the last time that was
overlooked in this file's neighbour: CAN-322 shipped a sidecar defect past
eleven checks that were ALL single-threaded, and the defect destroyed the
user's typed notes in 131 of 200 concurrent trials. Entry.mark_uploaded()
writes to the same file the running app writes, so the same failure shape is
available here: read a sidecar at listing time, hold it while the user types a
note, then write the stale copy back and roll the note away. The fix is that
_patch_upload() re-reads from disk immediately before writing. Only a
concurrent check can see the difference -- single-threaded, both versions pass.

CHECK 9 is the other half of that lesson: an unreadable sidecar must be left
alone, never rebuilt from what the code can infer, and never at the cost of
the .opus.
"""
import json, os, pathlib, sys, tempfile, threading, time

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="libchk-")
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

try:
    from plaud_linux import library
except ImportError:
    library = None

REC = paths.RECORDINGS

results = []
def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


def write_sidecar(session, **over):
    meta = {
        "session": session,
        "created": over.pop("created", "20260902_232504"),
        "mode": "system", "mic": False, "state": "stopped",
        "elapsed_s": over.pop("elapsed_s", 31),
        "final_path": str(REC / f"{session}.opus"),
        "segments_lost": 0, "screenshots": [], "notes": [], "flags": [],
    }
    meta.update(over)
    (REC / f"{session}.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def write_audio(session, nbytes):
    p = REC / f"{session}.opus"
    p.write_bytes(b"OggS" + b"\0" * max(nbytes - 4, 0))
    return p


def write_segment(session, nbytes, idx=0):
    d = REC / f".seg_{session}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"seg_{idx:03d}.opus"
    p.write_bytes(b"OggS" + b"\0" * max(nbytes - 4, 0))
    return p


def clear():
    for p in REC.iterdir():
        if p.is_dir():
            for q in p.iterdir():
                q.unlink()
            p.rmdir()
        else:
            p.unlink()


if library is None:
    check("library module exists", False, "plaud_linux.library not importable")
    print(f"\n0/1 passed")
    sys.exit(1)


# --- CHECK 1: the three upload states are told apart -------------------------
clear()
write_sidecar("sent", upload={"ok": True, "file_id": "abc123", "at": "x"})
write_audio("sent", 50000)
write_sidecar("broke", upload={"ok": False, "error": "boom", "at": "x"})
write_audio("broke", 50000)
write_sidecar("never")
write_audio("never", 50000)
by = {e.session: e for e in library.scan()}
check("uploaded / failed / unsent are distinguished",
      by["sent"].state == "uploaded" and by["broke"].state == "failed"
      and by["never"].state == "unsent",
      f"{by['sent'].state} / {by['broke'].state} / {by['never'].state}")

# --- CHECK 2: an already-sent recording is never offered for resend ----------
# Sending it again makes a second cloud note for one recording, and the user
# has to work out which to delete.
check("an uploaded recording is not resendable",
      by["sent"].uploaded and not by["sent"].resendable
      and by["broke"].resendable and by["never"].resendable,
      f"sent={by['sent'].resendable} broke={by['broke'].resendable} "
      f"never={by['never'].resendable}")

# --- CHECK 3: small is not absent -------------------------------------------
# The real regression this suite caught. A 1.6 KB segment is under the 2000
# bytes the uploader refuses, and filtering the listing on that number made a
# real orphaned recording on this machine invisible.
clear()
write_segment("tinyseg", 1600)
write_sidecar("tinyseg")
write_audio("tinyfinal", 1500)
write_sidecar("tinyfinal")
seen = {e.session: e for e in library.scan()}
check("a sub-2000-byte recording still appears in the listing",
      "tinyseg" in seen and "tinyfinal" in seen,
      f"listed={sorted(seen)}")

# --- CHECK 4: loose segments are shown, and are NOT one-click resendable -----
# Sending them would mean concatenating first, which writes into recordings/.
# That is a repair, not a resend, and it must not happen behind a button that
# says "Reenviar". The row still has to exist so the audio is never invisible.
clear()
write_segment("orphanseg", 4000)
write_sidecar("orphanseg")
e = library.scan()[0]
check("loose segments listed but not silently uploaded",
      e.state == "segments" and not e.resendable and len(e.loose_segments) == 1,
      f"state={e.state} resendable={e.resendable} segs={len(e.loose_segments)}")

# --- CHECK 5: audio with no sidecar at all is still found --------------------
# A crash between the ffmpeg spawn and the first _save_meta() leaves exactly
# this. A listing built only from sidecars would not show the audio.
clear()
write_audio("nosidecar", 50000)
found = [e.session for e in library.scan()]
check("a .opus with no sidecar is still listed", found == ["nosidecar"], f"{found}")

# --- CHECK 6: absent upload record reads as NOT sent -------------------------
# Every sidecar written before this feature existed lacks an `upload` key. The
# cost of guessing "sent" is a lost recording; of guessing "not sent", a
# duplicate. Principle II picks the duplicate.
clear()
write_sidecar("legacy")
write_audio("legacy", 50000)
e = library.scan()[0]
check("no upload record means not uploaded", not e.uploaded and e.resendable,
      f"uploaded={e.uploaded} resendable={e.resendable}")

# --- CHECK 7: ok:True with a failed attach still counts as uploaded ----------
# _record_outcome writes ok:True the moment the file id comes back, BEFORE
# attaching marks. A later attach failure must not make the app offer to send
# the audio again -- it is already on the server.
clear()
write_sidecar("attachfail",
              upload={"ok": True, "file_id": "f1", "at": "x"},
              screenshots=[{"path": "/tmp/a.png", "t": 3}])
write_audio("attachfail", 50000)
e = library.scan()[0]
check("uploaded-then-attach-failed is not resent", e.uploaded and not e.resendable,
      f"uploaded={e.uploaded}")

# --- CHECK 8: CONCURRENCY -- a resend must never roll back the user's notes --
# The failure shape a single-threaded check cannot express, and the one that
# destroyed real notes in CAN-322. Entry holds a copy of the sidecar read at
# listing time. If mark_uploaded() merges into THAT copy, every note the
# running app appended in between is written away.
#
# Modelled exactly as it happens: build the Entry (the user opens the list),
# then have the app append notes while a resend completes against it.
clear()
write_sidecar("race")
write_audio("race", 50000)
entry = library.scan()[0]          # the list is open, sidecar read into memory

# The interleaving is FORCED, not raced. Sleeping two threads and hoping they
# collide is how a concurrency check passes on a broken tree: measured, the
# loose version below stayed green with the lock deliberately neutered, because
# the two writers simply never overlapped in that run. So the note writer is
# driven to the exact point where it has read the file and not yet written it,
# and held there while the resend completes. That is the one interleaving that
# loses data, and it now happens every run rather than sometimes.
#
# Taking library.sidecar_lock in the writer is also the point rather than a
# convenience: flock is advisory and excludes only writers that take the same
# lock, so a writer that skipped it would prove nothing about the lock.
has_read = threading.Event()
may_write = threading.Event()
writer_err = []

def note_writer():
    try:
        with library.sidecar_lock(REC / "race.json"):
            cur = audio.read_json(REC / "race.json")
            cur.setdefault("notes", []).append({"text": "typed by the user", "t": 7})
            has_read.set()          # read done, write not yet
            may_write.wait(timeout=10)
            audio.write_json(REC / "race.json", cur)
    except Exception as e:          # noqa: BLE001 - reported, not swallowed
        writer_err.append(repr(e))
        has_read.set()

t = threading.Thread(target=note_writer)
t.start()
has_read.wait(timeout=10)

# The resend lands now, in the window where the app holds a stale copy. With
# the lock working this blocks until the writer finishes; without it, it slips
# in between and is then erased by the writer's stale write.
patch_done = threading.Event()
def do_patch():
    entry.mark_uploaded("fid-race")
    patch_done.set()

t2 = threading.Thread(target=do_patch)
t2.start()
# Give the patch a moment to either block on the lock (correct) or complete
# inside the window (the bug), then release the writer.
time.sleep(0.2)
patched_inside_window = patch_done.is_set()
may_write.set()
t.join(timeout=10)
t2.join(timeout=10)

final = audio.read_json(REC / "race.json") or {}
kept = [n for n in final.get("notes", []) if n.get("text") == "typed by the user"]
up = final.get("upload") or {}
# Both must survive. On an unlocked tree the patch completes inside the window
# (patched_inside_window True) and the writer's stale write then erases the
# upload record -- measured: upload=None with the lock neutered.
check("a resend and a concurrent note never erase each other",
      not writer_err and len(kept) == 1 and up.get("ok") is True
      and not patched_inside_window,
      f"note_kept={len(kept)} upload={up or None} "
      f"patch_slipped_into_window={patched_inside_window} err={writer_err}")

# --- CHECK 9: an unreadable sidecar is preserved, never rebuilt --------------
# audio.read_json renames a corrupt file aside and returns None. A resend
# against it must refuse rather than reconstruct it from the in-memory guess,
# and must not touch the .opus.
clear()
write_audio("damaged", 50000)
before = (REC / "damaged.opus").read_bytes()
(REC / "damaged.json").write_text('{"notes": [{"text": "impor', encoding="utf-8")
entries = library.scan()
dmg = [e for e in entries if e.session == "damaged"]
kept_aside = list(REC.glob("damaged.json.corrupt.*"))
ok_patch = dmg and dmg[0].mark_uploaded("nope") is False
after = (REC / "damaged.opus").read_bytes()
check("a corrupt sidecar is kept aside, not rebuilt, and the .opus is untouched",
      bool(dmg) and len(kept_aside) == 1 and ok_patch and after == before
      and not (REC / "damaged.json").exists(),
      f"listed={bool(dmg)} aside={len(kept_aside)} refused={ok_patch} "
      f"audio_same={after == before}")

# --- CHECK 10: resend() refuses an already-uploaded recording ----------------
# Enforced in main.resend, not only in the UI: a caller that reached it any
# other way must get the same refusal, or the guard is decoration.
clear()
write_sidecar("already", upload={"ok": True, "file_id": "f9", "at": "x"})
write_audio("already", 50000)
import plaud_linux.main as main_mod
notes_sent = []
main_mod.notify = lambda t, b: notes_sent.append(b)
e = library.scan()[0]
started = main_mod.resend(e)
check("resend refuses a recording already on the server",
      started is False and any("já foi enviada" in b for b in notes_sent),
      f"started={started} msgs={notes_sent}")

# --- CHECK 11: resend of loose segments refuses and names where they are -----
# The user must be told the audio survived and where, not just "no".
clear()
write_segment("segonly", 4000)
write_sidecar("segonly")
notes_sent.clear()
e = library.scan()[0]
started = main_mod.resend(e)
check("resend refuses loose segments and says where the audio is",
      started is False and any(".seg_segonly" in b for b in notes_sent),
      f"started={started} msgs={notes_sent}")

# --- CHECK 12: the listing never writes to recordings/ -----------------------
# scan() is a read. If merely opening the list mutates the user's directory,
# every guarantee above is built on sand.
clear()
write_sidecar("readonly")
write_audio("readonly", 50000)
write_segment("readonly", 3000)
snap = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in REC.rglob("*") if p.is_file()}
for _ in range(3):
    library.scan()
    library.orphans()
    library.uploaded()
snap2 = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
         for p in REC.rglob("*") if p.is_file()}
check("scanning the library writes nothing", snap == snap2,
      f"{len(snap)} files, changed={ {k for k in snap if snap.get(k) != snap2.get(k)} }")

# --- CHECK 13: newest first, and the order is stable -------------------------
# Sorted on the sidecar's own `created`, not mtime: writing an upload outcome
# touches the sidecar, so mtime would reshuffle the list every failed resend.
clear()
for i, ts in enumerate(["20260901_100000", "20260903_100000", "20260902_100000"]):
    write_sidecar(f"s{i}", created=ts)
    write_audio(f"s{i}", 50000)
# Touch the OLDEST one last, so an mtime sort would put it first.
os.utime(REC / "s0.json", None)
order = [e.session for e in library.scan()]
order2 = [e.session for e in library.scan()]
check("newest first by recording time, not by mtime",
      order == ["s1", "s2", "s0"] and order == order2, f"{order}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
