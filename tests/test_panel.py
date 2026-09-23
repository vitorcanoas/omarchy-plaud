"""Checks for the highlights panel (CAN-338).

Usage: python3 tests/test_panel.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm these still go red).
Each check prints PASS/FAIL.

No network, no ffmpeg, no real Recorder: the panel is a view over an Overlay,
so a fake Recorder is enough to drive every branch these checks touch.
"""
import os, sys, pathlib, tempfile

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="plaudpanel-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# The panel is new in this branch, so on the pre-change tree the import itself
# is what fails. Caught rather than left to traceback, so the older tree still
# prints FAIL lines run.py can count instead of dying with none.
try:
    from plaud_linux import overlay as ov
    from plaud_linux import panel as pn
    HAVE = True
except Exception as e:  # noqa: BLE001
    HAVE = False
    why = f"{type(e).__name__}: {e}"


class FakeRec:
    """Stands in for Recorder. Records what got attached, nothing else."""
    def __init__(self):
        self.state = "recording"
        self.screenshots = []
        self.notes = []
        self.session = "chk"
        self.final_path = pathlib.Path(home) / "chk.opus"
        self.flags = []
        # The ring the flag dumps from. None is a legitimate production state
        # -- _start_mark_buffer() leaves it None when the tap fails to spawn --
        # and the panel must handle it rather than raise, so that is what the
        # fake presents. Check 6 substitutes _flag_dump anyway; this only has
        # to be an attribute that exists.
        self.mark_buffer = None

    def start(self): pass

    def stop(self):
        self.state = "stopped"
        return self.final_path

    def elapsed(self): return 201          # 03:21, as in media/image24.png

    def add_note(self, text, elapsed): self.notes.append((text, elapsed))

    def add_screenshot(self, p): self.screenshots.append(p)

    def add_flag(self, path, t): self.flags.append((path, t))


def make():
    """An Overlay with a fake Recorder, plus the Panel its pencil opens."""
    real = ov.audio.Recorder
    ov.audio.Recorder = lambda **kw: FakeRec()
    try:
        o = ov.Overlay(mode="system", on_stop=lambda rec: stopped.append(rec))
    finally:
        ov.audio.Recorder = real
    # _notify shells out to notify-send on every note and pause. Silenced so a
    # check run does not spray the user's desktop with notifications.
    o._notify = lambda *a, **k: None
    # The PENCIL opens it, not pn.open_for(). Calling open_for here would make
    # check 1 assert its own setup: measured -- stubbing out the body of
    # overlay._on_note left "pencil opens the panel" still passing, because the
    # panel it found was the one this helper had built. Going through the button
    # is what makes the check about the wiring under test.
    o.btn_note.clicked()
    # getattr, not attribute access: a pencil that opens nothing must
    # reach the check as a None to report, not an AttributeError in the
    # helper -- that dies before any PASS/FAIL line is printed, and
    # run.py sees a file that produced no results rather than a failure.
    return o, getattr(o, "_panel", None)


stopped = []

if not HAVE:
    # Every name below, in order, so the count matches the green run. run.py
    # asserts a per-file check count, and a short list here would fail it for
    # the wrong reason -- "ran 6, expected 8" instead of the import error that
    # actually went wrong.
    for n in ("pencil opens the panel", "pencil reuses the open panel",
              "panel carries the flag button",
              "flag and crop go insensitive while paused",
              "note saves with the elapsed time sampled first",
              "blank note attaches nothing",
              "panel does not outlive the recording",
              "flag stub refuses after the recording stopped"):
        check(n, False, why)
else:
    # --- CHECK 1: the pencil opens the panel, and only one of them ----------
    #
    # The pencil used to open a modal Gtk.Dialog. Its whole job in the official
    # client is toggleWindowDisplay(WindowEnum.RecordingHighlight) -- it opens
    # this window (index-DotEOTW4.js:288-300).
    o, p = make()
    check("pencil opens the panel", isinstance(p, pn.Panel),
          f"got {type(p).__name__}")
    if p is None:
        # Everything below dereferences the panel the pencil was supposed to
        # open. Without this the run dies on AttributeError and prints one FAIL
        # instead of eight, which run.py counts as the wrong kind of broken.
        for n in ("pencil reuses the open panel",
                  "panel carries the flag button",
                  "flag and crop go insensitive while paused",
                  "note saves with the elapsed time sampled first",
                  "blank note attaches nothing",
                  "panel does not outlive the recording",
                  "flag stub refuses after the recording stopped"):
            check(n, False, "no panel to test")
        print(f"\n{sum(results)}/{len(results)} passed")
        sys.exit(1)

    # Pressing it again must present the open one, not stack a second window.
    o.btn_note.clicked()
    check("pencil reuses the open panel", o._panel is p,
          f"_panel is {'the same' if o._panel is p else 'a DIFFERENT'} object")

    # --- CHECK 2: the flag is present, and it is the official glyph ---------
    #
    # Presence alone would pass on a button drawn by eye. The path constant is
    # what says it is SvgIconFlag (index-CoA7AblW.js:855) rather than an
    # approximation, so the check asserts the geometry, not just the widget.
    has_btn = isinstance(getattr(p, "btn_flag", None), Gtk.Button)
    official = pn.FLAG_PATH.startswith("M5.14583 17.1668L4.3125 3.41682")
    check("panel carries the flag button", has_btn and official,
          f"button={has_btn} official_path={official}")

    # --- CHECK 3: flag and crop are inert while paused ----------------------
    #
    # index-CoA7AblW.js:1865,1885 compose "pointer-events-none" onto both while
    # isPauseRecording. For the flag this is load-bearing beyond looks: the ring
    # buffer keeps filling through a pause (CAN-309 Experiment 4), so a flag
    # pressed just after resume would return audio the recording does not hold.
    live = p.btn_flag.get_sensitive() and p.btn_shot.get_sensitive()
    o.rec.state = "paused"
    p._sync()
    inert = not p.btn_flag.get_sensitive() and not p.btn_shot.get_sensitive()
    o.rec.state = "recording"
    p._sync()
    back = p.btn_flag.get_sensitive() and p.btn_shot.get_sensitive()
    check("flag and crop go insensitive while paused", live and inert and back,
          f"recording={live} paused_inert={inert} resumed={back}")

    # --- CHECK 4: the note keeps the timestamp contract ---------------------
    #
    # The dialog this replaced sampled the clock BEFORE it blocked, because the
    # note belongs to the moment the user decided to write it. The elapsed time
    # stored must be the recorder's, not zero and not wall-clock.
    p.note.set_text("  ideia importante  ")
    p._on_note_activate()
    saved = o.rec.notes
    ok = (len(saved) == 1 and saved[0][0] == "ideia importante"
          and saved[0][1] == 201)
    check("note saves with the elapsed time sampled first", ok, f"notes={saved}")

    # An empty entry must attach nothing -- the dialog required text too.
    p.note.set_text("   ")
    p._on_note_activate()
    check("blank note attaches nothing", len(o.rec.notes) == 1,
          f"notes={o.rec.notes}")

    # --- CHECK 5: the panel dies with the recording ------------------------
    #
    # _do_stop() destroys the Overlay and hands the session to the upload
    # thread. A panel left open past that would show a frozen timer over a
    # stopped Recorder, with buttons wired to it.
    gone = []
    p.connect("destroy", lambda *_: gone.append(True))
    o.destroy()
    while Gtk.events_pending():
        Gtk.main_iteration()
    check("panel does not outlive the recording", bool(gone),
          f"destroyed={bool(gone)}")

    # --- CHECK 6: the flag refuses once the recording has stopped -----------
    #
    # Past _do_stop() the lists belong to the upload thread, so attaching there
    # promises the user a mark that is never sent -- the same failure
    # _shot_done already refuses. Substituting _flag_dump rather than calling
    # it keeps this check about the guard in _on_flag: the real body spawns
    # ffmpeg, which is test_markring.py's subject, not this file's.
    o2, p2 = make()
    fired = []
    p2._flag_dump = lambda elapsed: fired.append(elapsed)
    p2._on_flag()
    ran_live = fired == [201]
    o2.rec.state = "stopped"
    p2._on_flag()
    check("flag refuses after the recording stopped",
          ran_live and fired == [201],
          f"while recording={ran_live} calls after stop={len(fired) - 1}")
    o2.destroy()

    # --- CHECK 7: the REAL _flag_dump body runs without exploding -----------
    #
    # Check 6 substitutes _flag_dump, and the comment above defends that: the
    # real body spawns ffmpeg, which belongs to test_markring.py. That
    # reasoning is right about ffmpeg and was WRONG about everything before it.
    # _flag_dump shipped calling datetime.now() while panel.py had no datetime
    # import, so every flag press raised NameError -- and _on_flag has only a
    # `finally`, no `except`, so it escaped to GTK's default handler, the mark
    # was never recorded, and the upload went out with an empty flags list. It
    # was caught by a live end-to-end run against the real API, not here.
    #
    # A fake ring whose dump_ogg() returns None, NOT mark_buffer=None: the
    # empty-ring guard sits BEFORE the timestamp, so a None buffer returns
    # early and walks past the defective line without touching it. This one
    # goes through the real path -- building the output filename, which is
    # where datetime.now() lives -- and then bails at the empty dump, which is
    # the last point before ffmpeg would be spawned. That keeps ffmpeg in
    # test_markring.py's hands while still executing every line above it.
    # Verified red-then-green by deleting the import: this check goes red.
    class EmptyRing:
        def __init__(self):
            self.asked = []

        def dump_ogg(self, path):
            self.asked.append(path)
            return None

    o3, p3 = make()
    ring = EmptyRing()
    o3.rec.mark_buffer = ring
    said = []
    o3._notify = lambda title, body: said.append(body)
    blew_up = None
    try:
        p3._on_flag()
    except BaseException as e:
        blew_up = f"{type(e).__name__}: {e}"
    # The path it built is asserted too: it must carry the elapsed seconds and
    # land beside the screenshots, not in segdir, which _concat() deletes.
    built = ring.asked[0] if ring.asked else None
    check("the real _flag_dump body runs without raising",
          blew_up is None and len(said) == 1 and o3.rec.flags == []
          and built is not None and built.name.startswith("flag_000201_")
          and built.suffix == ".ogg" and built.parent == o3.shots_dir,
          f"raised={blew_up!r} said={said!r} built={built!r}")
    o3.destroy()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
