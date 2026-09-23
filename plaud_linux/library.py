#!/usr/bin/env python3
"""
plaud-linux :: the recording library -- what is on disk and what became of it.

Closes the lifecycle loop. Principle II guarantees a failed upload leaves the
.opus in recordings/ and tells the user; until now nothing led back from there.
A recording that failed to send was a file the user had to notice, remember,
and re-send by hand -- through a UI that did not exist.

This module answers one question, "what is in recordings/ and was it sent?",
and it answers it by READING ONLY. It never records, never uploads, never
touches GTK, and -- the constraint everything else here is shaped around --
never writes to the recordings directory at all. The one write in the whole
lifecycle-loop feature is `mark_uploaded()` in the resend path, and it goes
through audio.write_json() like every other sidecar write.

Why not just build a Recorder for an orphan and re-run the normal upload:
`Recorder.__init__` calls `self.segdir.mkdir()` and then `self._save_meta()`
unconditionally (audio.py:390, 407). Constructing one for a session that
already exists on disk would overwrite that session's sidecar with a blank
one -- destroying the notes the user typed, which live nowhere else. That is
the exact failure the read_json() docstring in audio.py forbids. So the
resend path passes a plain `Entry` (a dataclass over the sidecar dict), and
`main.py` reads the fields it needs off it rather than off a Recorder.

The hard boundary from CLAUDE.md: this lists and resends. It does not show
note content, transcripts or summaries -- those belong to web.plaud.ai, and
the official client itself redirects there rather than reimplementing them.
"""
import contextlib
import fcntl
import os
import time
from datetime import datetime
from pathlib import Path

try:
    from . import audio
    from . import paths
except ImportError:  # standalone script -- the dual-import shim, see CLAUDE.md
    import audio
    import paths


REC_DIR = paths.RECORDINGS

# An .opus this small is a header and nothing else. Same threshold _do_upload()
# rejects on (main.py), named once here so "too small to be audio" means the
# same thing in the listing as it does at the moment of upload -- a recording
# shown as resendable that the uploader would then refuse is a dead button.
MIN_AUDIO_BYTES = 2000


class Entry:
    """One recording on disk: its sidecar, its audio, and what became of it.

    Deliberately not a Recorder. A Recorder owns ffmpeg processes and a
    segment directory and writes a sidecar on construction; this owns a dict
    that was read off the disk. The upload path in main.py needs exactly six
    things -- final_path, session, screenshots, notes, flags, and somewhere
    to record the outcome -- so those are what this exposes, under the same
    names, and a resend can reuse that path unchanged.
    """

    def __init__(self, meta_path, meta):
        self.meta_path = Path(meta_path)
        self.meta = meta or {}
        self.session = self.meta.get("session") or self.meta_path.stem
        fp = self.meta.get("final_path")
        # Fall back to the conventional name rather than trusting the sidecar
        # blindly: `final_path` is an absolute path recorded on the machine
        # that wrote it, and PLAUD_LINUX_HOME can move recordings/ between
        # runs. The sibling .opus next to the sidecar is what is actually here.
        self.final_path = Path(fp) if fp else self.meta_path.with_suffix(".opus")
        if not self.final_path.exists():
            sibling = self.meta_path.with_suffix(".opus")
            if sibling.exists():
                self.final_path = sibling
        self.segdir = REC_DIR / f".seg_{self.session}"
        self.screenshots = self.meta.get("screenshots") or []
        self.notes = self.meta.get("notes") or []
        self.flags = self.meta.get("flags") or []
        self.upload = self.meta.get("upload") or None
        self.created = self.meta.get("created") or ""
        self.elapsed_s = self.meta.get("elapsed_s") or 0
        self.mode = self.meta.get("mode") or "system"
        self.segments_lost = self.meta.get("segments_lost") or 0

    # --- what became of it -------------------------------------------------

    @property
    def uploaded(self):
        """True only on positive evidence that the audio reached the server.

        `ok: True` is written by _record_outcome() the moment upload_and_generate
        returns a file id, before the attach step -- so a recording whose marks
        failed to attach still counts as uploaded, which is correct: the audio
        is on the server and re-sending it would produce a duplicate note.

        Absence of an `upload` block is NOT uploaded. Every sidecar written
        before this feature existed lacks one, and the safe reading of "no
        record either way" is "cannot prove it was sent" -- the cost of that
        being wrong is a duplicate, and the cost of the opposite is a silently
        lost recording. Principle II picks the duplicate.
        """
        return bool(self.upload) and self.upload.get("ok") is True

    @property
    def file_id(self):
        return (self.upload or {}).get("file_id")

    @property
    def error(self):
        return (self.upload or {}).get("error")

    @property
    def has_audio(self):
        """A concatenated .opus big enough to be more than an Ogg header."""
        try:
            return self.final_path.is_file() and \
                self.final_path.stat().st_size >= MIN_AUDIO_BYTES
        except OSError:
            return False

    @property
    def loose_segments(self):
        """Un-concatenated segments, in order, when the merge never happened.

        The case CAN-315 calls out explicitly: _concat() can fail and leave the
        audio as .seg_<session>/seg_NNN.opus with no final .opus beside it. The
        audio exists and is the user's, so the listing must see it -- a resend
        that only looked for the .opus would call this session empty and offer
        nothing, which is precisely the silent loss this feature exists to end.

        MIN_AUDIO_BYTES is deliberately NOT applied here, and this is the one
        threshold decision in the file worth stating twice. It was, at first,
        and it hid one of the three real orphan recordings on this machine:
        .seg_gravacao_20260902_232546/seg_000.opus is 1.6 KB, under the 2000 the
        uploader rejects on, so the session scored `empty` and vanished from the
        listing entirely. A short recording is still the user's recording, and a
        list that silently omits it is worse than no list -- it actively tells
        the user there is nothing there. Anything non-empty counts. Whether it
        is big enough to *send* is a separate question, answered by has_audio on
        the merged file, which is the only thing an upload ever reads.
        """
        try:
            if not self.segdir.is_dir():
                return []
            segs = sorted(p for p in self.segdir.iterdir()
                          if p.suffix == ".opus" and p.is_file())
        except OSError:
            return []
        return [p for p in segs if _size(p) > 0]

    @property
    def tiny_audio(self):
        """A final .opus that exists but is under what the uploader accepts.

        Its own state rather than "empty", for the same reason loose_segments
        does not filter on size: the file is there, it is the user's, and a
        listing that drops it says "nothing here" about something that is.
        """
        try:
            return self.final_path.is_file() and 0 < _size(self.final_path) < MIN_AUDIO_BYTES
        except OSError:
            return False

    @property
    def state(self):
        """One of: uploaded | failed | unsent | segments | tiny | empty.

        `failed` and `unsent` differ only in whether an attempt is on record,
        and they are kept apart because they read differently to a human: one
        is "this went wrong", the other is "this never got as far as trying"
        (the app was killed, the machine rebooted mid-session). Both are
        resendable and both are the user's audio.

        `empty` is the only state that drops a row from the listing, so it has
        to mean *genuinely no audio anywhere* -- not "no audio I would upload".
        Conflating those hid a real 1.6 KB recording; see loose_segments.
        """
        if self.uploaded:
            return "uploaded"
        if self.has_audio:
            return "failed" if self.upload else "unsent"
        if self.loose_segments:
            return "segments"
        if self.tiny_audio:
            return "tiny"
        return "empty"

    @property
    def resendable(self):
        """Can this be sent right now, without repairing anything first?

        Loose segments are deliberately excluded: sending them would mean
        concatenating first, and concatenation writes into recordings/ -- a
        repair, not a resend. The listing still shows those sessions, under
        their own state, so the audio is never invisible; it just is not one
        click from the server.
        """
        return not self.uploaded and self.has_audio

    @property
    def when(self):
        """A datetime for sorting, from the sidecar's own timestamp if possible.

        mtime is the fallback and not the primary: writing the upload outcome
        touches the sidecar, so mtime says when the upload was last attempted,
        not when the recording was made. Sorting a list of recordings by that
        reorders it every time a resend fails.
        """
        try:
            return datetime.strptime(self.created, "%Y%m%d_%H%M%S")
        except (ValueError, TypeError):
            pass
        for p in (self.final_path, self.meta_path):
            try:
                return datetime.fromtimestamp(p.stat().st_mtime)
            except OSError:
                continue
        return datetime.fromtimestamp(0)

    @property
    def size_bytes(self):
        if self.has_audio:
            return _size(self.final_path)
        return sum(_size(p) for p in self.loose_segments)

    def mark_uploaded(self, file_id):
        """Record a successful resend, exactly as _record_outcome() would.

        Merged into whatever the sidecar already holds rather than replacing
        it, because the previous `error` is history the user may want and this
        function has no reason to erase it. Re-read from disk immediately
        before writing, not merged into the copy loaded at listing time: that
        copy may be minutes old, and anything the running app appended in the
        meantime -- a note, a screenshot -- would be silently rolled back by
        writing the stale version. Principle II again: never write over the
        user's data with an older version of it.
        """
        return self._patch_upload(ok=True, file_id=file_id)

    def mark_failed(self, error):
        """Record a resend that did not get the audio to the server."""
        return self._patch_upload(ok=False, error=str(error))

    def _patch_upload(self, **fields):
        # Read and write inside one lock. Re-reading alone is not enough: the
        # note the app is writing can land between this read and this write,
        # and then this write erases it. See sidecar_lock.
        with sidecar_lock(self.meta_path):
            fresh = audio.read_json(self.meta_path)
            if fresh is None:
                # Unreadable or gone. read_json() has already renamed a corrupt
                # file aside; what must NOT happen is rebuilding it from the
                # copy in memory, which is a guess at the user's notes rather
                # than the notes. Report the failure and leave the disk alone
                # -- the audio is untouched either way, and this only ever
                # writes the .json.
                return False
            fields.setdefault("at", datetime.now().isoformat())
            fresh["upload"] = dict(fresh.get("upload") or {}, **fields)
            self.meta = fresh
            self.upload = fresh["upload"]
            audio.write_json(self.meta_path, fresh)
        return True


def _size(p):
    try:
        return p.stat().st_size
    except OSError:
        return 0


@contextlib.contextmanager
def sidecar_lock(meta_path):
    """Serialise a read-modify-write of one sidecar against other writers.

    audio.write_json() is atomic -- a reader never sees a torn file -- but
    atomicity is not exclusion, and the sidecar is edited by read-modify-write
    from more than one place. Measured: a resend's `upload: {ok: true}` and a
    note typed in the running app, interleaved as read/read/write/write, ends
    with whichever wrote last and the other silently gone.

    Losing the note is the CAN-322 failure again. Losing the upload record is
    worse in a way peculiar to this feature: the list would then show a
    recording that IS on the server as still needing to be sent, and the user
    clicking "Reenviar" gets a duplicate note. Re-reading immediately before
    writing narrows that window but cannot close it -- something must exclude.

    A separate .lock file, never the sidecar itself: locking the sidecar would
    mean opening it for write, and the whole point of write_json's temp-and-
    rename is that the real file is never opened for write in place. flock is
    advisory and per-open-file-description, so it only excludes writers that
    take this same lock -- which is why the note-writing paths in audio.py take
    it too.

    Best-effort by design. A filesystem with no working flock (some network
    mounts) raises OSError here, and the correct response is to proceed
    unlocked exactly as the code did before this existed: the risk is a lost
    field, and refusing to record a successful upload at all is the worse
    outcome of the two.
    """
    lock_path = meta_path.with_name(f".{meta_path.name}.lock")
    fh = None
    try:
        fh = open(lock_path, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        if fh is not None:
            fh.close()
            fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def scan(rec_dir=None):
    """Every recording in recordings/, newest first.

    Built from the sidecars, then widened to cover .opus and segment
    directories that have no sidecar at all -- a crash between the ffmpeg
    spawn and the first _save_meta() leaves exactly that, and a listing built
    from sidecars alone would not show the audio it left behind.

    An unparseable sidecar does NOT drop the recording from the listing: it is
    shown from what the filesystem alone can say. read_json() renames the bad
    file aside and returns None; this treats that as "no metadata", never as
    "no recording", because the .opus is the part that matters and it is still
    right there.
    """
    d = Path(rec_dir) if rec_dir else REC_DIR
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []

    seen = {}
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        p = d / name
        meta = audio.read_json(p)
        if meta is None:
            # Sidecar unreadable and now renamed aside by read_json(). The
            # session still exists on disk; carry the stem so the .opus below
            # is still found and offered.
            meta = {"session": p.stem}
        seen[p.stem] = Entry(p, meta)

    # Audio with no sidecar, and segment dirs with neither. `.seg_` is a
    # hidden dir by name, so it is walked explicitly rather than caught by the
    # listing above.
    for name in names:
        if name.endswith(".opus") and not name.startswith("."):
            stem = name[: -len(".opus")]
            if stem not in seen:
                seen[stem] = Entry(d / f"{stem}.json", {"session": stem})
        elif name.startswith(".seg_"):
            stem = name[len(".seg_"):]
            if stem not in seen:
                seen[stem] = Entry(d / f"{stem}.json", {"session": stem})

    out = [e for e in seen.values() if e.state != "empty"]
    # Newest first, and by session name within the same second so the order is
    # total -- two recordings can share a timestamp string, and an unstable
    # order would shuffle rows between openings of the window.
    out.sort(key=lambda e: (e.when, e.session), reverse=True)
    return out


def orphans(rec_dir=None):
    """Recordings that are on disk and are not on the server."""
    return [e for e in scan(rec_dir) if not e.uploaded]


def uploaded(rec_dir=None):
    """Recordings this machine has positive evidence of having sent."""
    return [e for e in scan(rec_dir) if e.uploaded]



def _within(rec_dir, path):
    """Resolve `path` and confirm it is rec_dir itself or strictly inside it.

    The one check every deletion in discard() is gated behind. `Entry` is
    built from a sidecar the running app writes with ordinary file I/O, not
    from a hardened format -- `final_path` is a plain string field, and
    Entry.__init__ already distrusts it once (falling back to the sibling
    .opus when it does not exist, because PLAUD_LINUX_HOME can move
    recordings/ between the run that wrote the sidecar and this one). A
    session name of "../.." or a stray absolute final_path pointing at, say,
    the user's home directory is exactly the kind of value that field can
    hold without the sidecar being corrupt in any way read_json() would
    catch. Resolving both sides (following symlinks) and comparing is what
    makes this a real containment check rather than a string prefix test --
    "/rec/../etc" and "/etc" are the same path, and only resolve() sees that.
    """
    try:
        base = Path(rec_dir).resolve()
        target = Path(path).resolve()
    except OSError:
        return False
    return target == base or base in target.parents


def discard(entry):
    """Permanently delete every on-disk artifact of one recording session.

    Removes, best-effort and independently: the concatenated .opus
    (entry.final_path), the segment directory (entry.segdir), the sidecar
    (entry.meta_path), that sidecar's lock file if sidecar_lock() left one
    behind, and the session's screenshot directory -- computed the same way
    overlay.py computes it (entry.final_path.parent / "screenshots" /
    entry.session), since Entry carries no attribute for it and inventing a
    second convention would only let the two drift apart.

    Not a Recorder operation and not offered as one: library.py's own module
    docstring explains why a Recorder cannot be constructed for a session
    that already exists on disk -- __init__ calls segdir.mkdir() and
    _save_meta() unconditionally (audio.py:390, 407), which would blank the
    notes this function is about to delete anyway, and worse, would blank
    them for a session the caller may have picked *by mistake* half a second
    before realising it and aborting. discard() takes the plain Entry and
    touches nothing that isn't already named on it.

    SAFETY. This deletes user data on a caller's say-so, so it is written to
    fail closed rather than fail loud:

    - Every path is bounded to entry.segdir's parent -- REC_DIR in ordinary
      use, but derived from the entry rather than imported, so a caller
      testing against a different recordings/ gets the same guarantee. See
      _within(). A session name or final_path that resolves outside that
      directory aborts the delete for that artifact and is reported as a
      failure; it is not "fixed" by clamping it to somewhere else. Proven in
      tests/test_discard.py by pointing final_path outside the tree and
      checking the file survives.
    - The screenshots dir and segdir are removed with os.walk(topdown=False,
      followlinks=False) plus an explicit is_symlink() guard on every entry
      before unlink/rmdir. followlinks=False alone stops os.walk from
      descending into a symlinked subdirectory, but it still hands back the
      symlink's own name for removal -- which must go through os.unlink(),
      not a directory-removal call, or a symlinked dir would have its
      *target* emptied instead of the link itself. paths.prune_old_logs()
      hit the equivalent bug with a hardlinked app.log; the fix here is the
      same discipline applied to a whole subtree instead of one file.
    - Sidecar removal happens inside sidecar_lock(entry.meta_path), because a
      note being typed in the running app is a read-modify-write against
      that same file (see sidecar_lock's docstring); deleting it unlocked
      could race a write and leave a half-applied note in a file that then
      vanishes under it.
    - Every step is independent and wrapped in its own try/except OSError:
      one artifact failing (permissions, already gone, a lock held
      elsewhere) must not stop the rest from being attempted, but it also
      must never be swallowed. This is why the return value is a list, not a
      bool.

    Returns a list of human-readable strings, one per artifact this call
    could NOT remove; an empty list means everything named above is gone (or
    was already gone -- discard() is idempotent, so calling it twice reports
    no failures the second time either). A bool would collapse "removed
    nothing" and "removed four of five" into the same False, and the caller
    -- whatever UI eventually offers a delete button -- needs to tell the
    user which file to go remove by hand rather than just "something went
    wrong".
    """
    rec_dir = entry.segdir.parent
    failures = []

    def _rm_tree(root):
        if not root.is_dir() or root.is_symlink():
            return
        if not _within(rec_dir, root):
            raise OSError(f"{root} is outside {rec_dir}")
        for dirpath, dirnames, filenames in os.walk(root, topdown=False,
                                                      followlinks=False):
            dp = Path(dirpath)
            for fname in filenames:
                # os.unlink() removes the directory entry itself and never
                # follows it, whether fname names a plain file or a symlink
                # -- so no is_symlink() branch is needed here, unlike the
                # subdirectory case below where the removal *call* differs.
                os.unlink(dp / fname)
            for dname in dirnames:
                sub = dp / dname
                if sub.is_symlink():
                    os.unlink(sub)  # remove the link, not its target
                else:
                    os.rmdir(sub)
        root.rmdir()

    with sidecar_lock(entry.meta_path):
        # 1. the concatenated audio
        try:
            fp = entry.final_path
            if fp.exists() or fp.is_symlink():
                if not _within(rec_dir, fp):
                    raise OSError(f"{fp} is outside {rec_dir}")
                os.unlink(fp)
        except OSError as e:
            failures.append(f"audio ({entry.final_path}): {e}")

        # 2. the segment directory
        try:
            _rm_tree(entry.segdir)
        except OSError as e:
            failures.append(f"segments ({entry.segdir}): {e}")

        # 3. the screenshot directory -- same derivation as overlay.py's
        # self.shots_dir, kept in exactly one place there and mirrored here
        # rather than imported, because overlay.py is GTK-facing and this
        # module never imports it (see the module docstring's boundary).
        try:
            shots_dir = entry.final_path.parent / "screenshots" / entry.session
            _rm_tree(shots_dir)
        except OSError as e:
            failures.append(f"screenshots ({shots_dir}): {e}")

        # 4. the sidecar itself, still holding the lock
        try:
            mp = entry.meta_path
            if mp.exists() or mp.is_symlink():
                if not _within(rec_dir, mp):
                    raise OSError(f"{mp} is outside {rec_dir}")
                os.unlink(mp)
        except OSError as e:
            failures.append(f"sidecar ({entry.meta_path}): {e}")

    # 5. the lock file, AFTER releasing it -- sidecar_lock's own finally block
    # still needs to flock/close it on the way out of the `with` above.
    try:
        lock_path = entry.meta_path.with_name(f".{entry.meta_path.name}.lock")
        if lock_path.exists() or lock_path.is_symlink():
            if not _within(rec_dir, lock_path):
                raise OSError(f"{lock_path} is outside {rec_dir}")
            os.unlink(lock_path)
    except OSError as e:
        failures.append(f"lock file ({lock_path}): {e}")

    return failures

# --- formatting, for whatever draws the list --------------------------------

def human_size(n):
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def human_duration(secs):
    secs = int(secs or 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def human_when(dt):
    """Date as the official client shows it in its list: relative, then absolute."""
    now = datetime.now()
    same_day = (dt.year, dt.month, dt.day) == (now.year, now.month, now.day)
    if same_day:
        return f"Hoje {dt:%H:%M}"
    delta = (now.date() - dt.date()).days
    if delta == 1:
        return f"Ontem {dt:%H:%M}"
    if 0 < delta < 7:
        return f"{['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'][dt.weekday()]} {dt:%H:%M}"
    return f"{dt:%d/%m/%Y %H:%M}"


# User-facing, so Portuguese (CLAUDE.md Conventions). Keys are the `state`
# values, which stay English because they are code.
STATE_LABEL = {
    "uploaded": "Enviado",
    "failed": "Falhou",
    "unsent": "Não enviado",
    "segments": "Só trechos",
    "tiny": "Curta demais",
    "empty": "Vazio",
}

STATE_HINT = {
    "uploaded": "Já está no Plaud.",
    "failed": "O envio falhou. Dá para reenviar.",
    "unsent": "Nunca foi enviado. Dá para reenviar.",
    "segments": "A junção dos trechos falhou — o áudio está preservado em pedaços.",
    "tiny": "Curta demais para enviar, mas o arquivo está guardado.",
    "empty": "Sem áudio.",
}


if __name__ == "__main__":
    # smoke test: print what is in recordings/ and what became of it
    print(f"recordings: {REC_DIR}")
    rows = scan()
    if not rows:
        print("(nenhuma gravação)")
    for e in rows:
        mark = "OK " if e.uploaded else "   "
        print(f"{mark}{e.session:34s} {STATE_LABEL[e.state]:12s} "
              f"{human_when(e.when):18s} {human_duration(e.elapsed_s):>8s} "
              f"{human_size(e.size_bytes):>9s}"
              + (f"  err={e.error}" if e.error else "")
              + (f"  id={e.file_id}" if e.file_id else ""))
    print(f"\n{len(rows)} gravação(ões), "
          f"{sum(1 for e in rows if e.resendable)} reenviável(is)")
