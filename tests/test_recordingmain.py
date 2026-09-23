"""Checks for the recording-main window (280x215 main recording panel).

Usage: python3 tests/test_recordingmain.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm these still go red).
Each check prints PASS/FAIL.

No network, no ffmpeg, no real Recorder: the window is a view over an
Overlay, so a fake Overlay/Recorder pair is enough to drive every branch
these checks touch.
"""
import os, sys, pathlib, re, tempfile

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="plaudrecmain-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# recording_main.py is new in this branch, so on the pre-change tree the
# import itself is what fails. Caught rather than left to traceback, so the
# older tree still prints FAIL lines run.py can count instead of dying with
# none.
try:
    from plaud_linux import recording_main as rm
    HAVE = True
except Exception as e:  # noqa: BLE001
    HAVE = False
    why = f"{type(e).__name__}: {e}"


class FakeRec:
    """Stands in for Recorder. Only what _sync()/_draw_pause_white() read."""
    def __init__(self):
        self.state = "recording"

    def elapsed(self): return 83          # 01:23


class FakeOverlay(Gtk.Window):
    """Stands in for Overlay. Records delegated calls, nothing else."""
    def __init__(self):
        super().__init__()
        self.rec = FakeRec()
        self.pause_calls = 0
        self.stop_calls = 0
        self.note_calls = 0

    def _on_pause(self, *_): self.pause_calls += 1

    def _on_stop(self, *_): self.stop_calls += 1

    def _on_note(self, *_): self.note_calls += 1


if not HAVE:
    # Every name below, in order, so the count matches the green run. run.py
    # asserts a per-file check count, and a short list here would fail it for
    # the wrong reason -- "ran 4, expected 6" instead of the import error that
    # actually went wrong.
    for n in ("window geometry matches the official spec",
              "layer-shell margins match the official spec",
              "timer label reflects elapsed time after _sync()",
              "pause delegates to the overlay's own handler",
              "stop delegates to the overlay's own handler",
              "_remove_tick() clears the GLib timer"):
        check(n, False, why)
else:
    # --- CHECK 1: the window is 280x215 -------------------------------------
    #
    # main/index-BzODulx0.js: the whole window, transparent margin included --
    # the card fills it, so this is also the card's own size.
    check("window geometry matches the official spec",
          rm.WINDOW_WIDTH == 280 and rm.WINDOW_HEIGHT == 215,
          f"WIDTH={rm.WINDOW_WIDTH} HEIGHT={rm.WINDOW_HEIGHT}")

    # --- CHECK 2: placement margins ------------------------------------------
    #
    # x = workArea.width - 280 - 10, y = 20 -- expressed as layer-shell margins
    # from the top-right corner, which is the same corner the official
    # arithmetic anchors to.
    check("layer-shell margins match the official spec",
          rm.MARGIN_TOP == 20 and rm.MARGIN_RIGHT == 10,
          f"MARGIN_TOP={rm.MARGIN_TOP} MARGIN_RIGHT={rm.MARGIN_RIGHT}")

    # --- CHECK 3: the timer follows the Recorder's elapsed time -------------
    fake = FakeOverlay()
    w = rm.RecordingMain(fake)
    w._sync()
    text = w.timer.get_text()
    check("timer label reflects elapsed time after _sync()",
          bool(re.match(r"^\d\d:\d\d$", text)), f"timer={text!r}")

    # --- CHECK 4: pause delegates, the window does not reimplement it -------
    #
    # btn_pause is wired to RecordingMain._on_pause, which must call the
    # Overlay's own _on_pause -- the state machine and notification live
    # there, once.
    w.btn_pause.clicked()
    check("pause delegates to the overlay's own handler",
          fake.pause_calls == 1, f"pause_calls={fake.pause_calls}")

    # --- CHECK 5: stop delegates, guarding the single double-stop guard -----
    #
    # _do_stop()'s single-execution guard lives on the Overlay. A window that
    # reimplemented stop here would be a second way into the upload with no
    # guard on it.
    # The real stop button is built inside _build_record_control() and is not
    # kept as an attribute, so it is found the same way a user would reach
    # it: walking the RecordControl bar's children for the button whose
    # tooltip is "Parar".
    bar = w.get_children()[0].get_children()[1].get_children()[0]
    real_stop = next(c for c in bar.get_children()
                      if isinstance(c, Gtk.Button)
                      and c.get_tooltip_text() == "Parar")
    real_stop.clicked()
    check("stop delegates to the overlay's own handler",
          fake.stop_calls == 1, f"stop_calls={fake.stop_calls}")

    # --- CHECK 6: _remove_tick() clears the GLib timer -----------------------
    #
    # The tick outlives the window unless removed explicitly -- destroy() does
    # not touch GLib sources on its own, so a closed window would leave a
    # timer firing 2x/s on dead widgets.
    had_tick = w._tick_id is not None
    w._remove_tick()
    check("_remove_tick() clears the GLib timer",
          had_tick and w._tick_id is None,
          f"had_tick={had_tick} after={w._tick_id}")

    w.destroy()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
