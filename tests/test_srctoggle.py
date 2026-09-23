"""Checks for CAN-315: the entry flow must not gate recording behind a modal.

The official client has no pre-recording mode picker. Clicking record opens the
recording window already capturing, and microphone / system audio are two
independent toggles live inside it -- both may be on at once. This repo used to
open a modal Gtk.Dialog first and force one exclusive answer before any audio
existed, which is the user's #1 complaint on this project.

What is asserted here, in the order a session meets it:

  1-2  pick_sources() asks nothing and returns the opening pair.
  3-4  the overlay carries both toggles, reflecting that pair.
  5-8  flipping them maps onto the engine's three legal modes, live, on a
       Recorder that is already recording.
  9-11 "neither" is unreachable through the UI and refused by the engine.
  12   a mode switch cuts a new segment instead of restarting the session.
  13   the switch does not make the clock tick backwards.
  14   ADVERSARIAL, see the comment at that check.

Usage: python3 tests/test_srctoggle.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

Headless-safe: builds the real Overlay but stubs the one thing that needs a
compositor and the one thing that needs ffmpeg. Nothing here spawns a process,
touches the network, or writes outside PLAUD_LINUX_HOME.
"""
import os, sys, tempfile, pathlib, types, time

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="srctoggle-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

# Never let a check reach the network: plaud_api imports requests at module
# scope, and main imports plaud_api. Raising here means an accidental call is a
# loud failure rather than a real request against the user's live account.
stub = types.ModuleType("requests")


def _no_net(*a, **k):
    raise AssertionError("a check tried to reach the network")


stub.get = stub.post = stub.put = stub.request = _no_net
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from plaud_linux import main as main_mod
from plaud_linux import audio as audio_mod

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# --- CHECK 1: pick_sources() must not build a dialog at all -----------------
# The literal regression. Asserted by making Gtk.Dialog itself explode: if the
# entry path constructs one, this raises from inside pick_sources() and the
# check goes red naming the thing that was rebuilt. Inspecting the return value
# alone could not tell "asked nothing" from "asked and got an answer", which is
# exactly the difference the ticket is about.
real_dialog_init = Gtk.Dialog.__init__
built = []


def exploding_init(self, *a, **k):
    built.append(True)
    raise AssertionError("pick_sources() built a Gtk.Dialog")


Gtk.Dialog.__init__ = exploding_init
try:
    sources = main_mod.pick_sources()
    dialogless = True
except AssertionError:
    sources = None
    dialogless = False
finally:
    Gtk.Dialog.__init__ = real_dialog_init

check("1 pick_sources() opens no dialog", dialogless,
      f"(Gtk.Dialog constructed {len(built)}x)")

# --- CHECK 2: it returns the opening pair, never the cancel sentinel ---------
# (True, True) -- BOTH ON, matching the official client, which opens the native
# recorder with a literal enableMicrophone: true and defaults its mic selector
# to "smart"/Automatic (recordingService-Mile2cgU.js:297, states-eNBxWOSQ.js:473).
#
# This reverses an earlier deliberate divergence. The reversal was the user's
# own call on 2026-09-06, taken with the consequence stated and confirmed
# twice: muting yourself in Meet does NOT stop this, because the meeting app
# and the recorder open separate OS-level captures of the same microphone. He
# chose it anyway, because a meeting recording that captures only the other
# people is useless and remembering a toggle before every call is exactly the
# friction he asked to be rid of. Do not flip it back without asking him.
check("2 pick_sources() returns the opening (system, mic) pair",
      sources == (True, True), f"(returned {sources!r})")


# --- a Recorder that never spawns ffmpeg ------------------------------------
# Subclassed rather than monkeypatched so the real start/pause/resume/
# set_sources logic all runs -- only the Popen at the bottom is replaced. The
# segment list, the state machine and the clock are the real ones, which is the
# whole point: those are what a mid-recording source switch has to get right.
class FakeProc:
    def __init__(self):
        self.signals = []

    def send_signal(self, s):
        self.signals.append(s)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class DryRecorder(audio_mod.Recorder):
    def __init__(self, *a, **k):
        self.spawned = []
        super().__init__(*a, **k)

    def start(self):
        if self.state == "recording":
            return
        # Record the command the real _ffmpeg_cmd() would have run, then take
        # over the bookkeeping start() does. Copied deliberately rather than
        # called via super(): super() would Popen. The fields touched here are
        # the ones the checks below read.
        seg = self.segdir / f"seg_{len(self.segments):03d}.opus"
        self.spawned.append(self._ffmpeg_cmd(seg))
        self.proc = FakeProc()
        self.segments.append(seg)
        if self.started_at is None:
            self.started_at = time.time()
        elif self.paused_at is not None:
            self.paused_accum += time.time() - self.paused_at
            self.paused_at = None
        self.state = "recording"

    def _start_mark_buffer(self):
        pass

    def _stop_mark_buffer(self):
        pass

    def _resolve_src(self):
        # Both devices present, so every one of the three modes is reachable.
        # Fixed strings, so the assembled command is comparable across spawns.
        return ("mon.monitor", "sink0", "mic0", "stubbed")


rec = DryRecorder(mode="system", name="chk")
rec.start()

# --- CHECK 5-8: the three legal modes, live, on a recording Recorder --------
# Asserted through the ffmpeg command rather than through self.mode, because
# self.mode is the thing under test and reading it back would pass by
# construction. The command is what actually decides which audio is captured.
def cmd_of(r):
    return " ".join(r.spawned[-1])


ok_sys = "-i mon.monitor" in cmd_of(rec) and "mic0" not in cmd_of(rec)
check("5 system-only records the monitor and not the mic", ok_sys,
      f"(mode={rec.mode!r})")

applied = rec.set_sources(True, True)
c = cmd_of(rec)
ok_meeting = applied and "mon.monitor" in c and "mic0" in c and "amix=inputs=2" in c
check("6 both on mixes monitor + mic (mode 'meeting')", ok_meeting,
      f"(applied={applied} mode={rec.mode!r})")

applied = rec.set_sources(False, True)
c = cmd_of(rec)
ok_mic = applied and "-i mic0" in c and "mon.monitor" not in c
check("7 mic-only records the mic and not the monitor", ok_mic,
      f"(applied={applied} mode={rec.mode!r})")

applied = rec.set_sources(True, False)
c = cmd_of(rec)
ok_back = applied and "mon.monitor" in c and "mic0" not in c
check("8 switching back to system-only drops the mic again", ok_back,
      f"(applied={applied} mode={rec.mode!r})")

# --- CHECK 9: "neither" is refused by the engine ----------------------------
# A DELIBERATE divergence, asserted so it cannot be removed by accident. The
# official client allows both off and pads the resulting hole with
# recorder.fillSilence(gapMs); our ffmpeg/-c copy engine cannot manufacture
# silence, so allowing it would desynchronise the audio from the elapsed clock
# that every screenshot and note is stamped against. See set_sources().
before_mode, before_segs = rec.mode, len(rec.segments)
refused = rec.set_sources(False, False)
check("9 set_sources(False, False) is refused and changes nothing",
      refused is False and rec.mode == before_mode
      and len(rec.segments) == before_segs,
      f"(returned {refused!r}, mode={rec.mode!r}, "
      f"segments {before_segs}->{len(rec.segments)})")

# --- CHECK 12: a switch cuts a segment, it does not restart the session -----
# The failure this catches is a switch implemented as stop()+new Recorder,
# which would silently drop everything recorded before the toggle. Segments
# must accumulate and the session id must be the one we started with.
check("12 a source switch appends a segment and keeps the session",
      len(rec.segments) == 4 and rec.session == "chk"
      and rec.state == "recording",
      f"(segments={len(rec.segments)} session={rec.session!r} "
      f"state={rec.state!r})")

# --- CHECK 13: the clock must not tick backwards across a switch ------------
# set_sources() goes through pause(), which charges the gap to paused_accum.
# Left alone that subtracts the split's own duration from elapsed(), so the
# timer the user is watching jumps backwards on a toggle they never asked to
# pause. Measured against the real clock, with a real gap forced.
rec2 = DryRecorder(mode="system", name="chk2")
rec2.start()
rec2.started_at -= 30.0          # pretend 30s in
before_elapsed = rec2.elapsed()
rec2.set_sources(True, True)
after_elapsed = rec2.elapsed()
check("13 elapsed() does not go backwards across a source switch",
      after_elapsed >= before_elapsed,
      f"({before_elapsed}s -> {after_elapsed}s)")

# --- CHECK 3-4 + 10-11: the overlay's own toggles ---------------------------
# Built last because it needs the layer-shell stub in place. The Overlay is the
# real class; only the compositor call and the Recorder are replaced.
import plaud_linux.overlay as ov_mod

# Swallows every GtkLayerShell call and enum rather than listing the ones
# _init_layer_shell() happens to make today. Listing them means this file goes
# red -- with an AttributeError, not a check -- the next time the overlay asks
# the compositor for one more thing, which is a false regression about
# something this file is not testing. What matters here is that no real
# surface is created; which calls were skipped does not.
class _NoLayerShell:
    def __getattr__(self, _name):
        return _NoLayerShell()

    def __call__(self, *a, **k):
        return None


ov_mod.GtkLayerShell = _NoLayerShell()
ov_mod.audio.Recorder = DryRecorder

overlay = ov_mod.Overlay(mode="system", name="chk3")

has_sys = getattr(overlay, "btn_sys", None) is not None
has_mic = getattr(overlay, "btn_mic", None) is not None
check("3 the overlay carries both source toggles", has_sys and has_mic,
      f"(btn_sys={has_sys} btn_mic={has_mic})")

check("4 the toggles open reflecting the session's actual sources",
      has_sys and has_mic
      and overlay.btn_sys.get_active() is True
      and overlay.btn_mic.get_active() is False,
      f"(system={has_sys and overlay.btn_sys.get_active()} "
      f"mic={has_mic and overlay.btn_mic.get_active()})")

# --- CHECK 10: turning the last source off must be impossible in the UI -----
# The engine refuses the pair (check 9), but a UI that lets the user click into
# a refused state leaves the toggle showing "off" while the audio keeps
# recording -- a control that lies. So the last one on must be insensitive.
overlay.btn_mic.set_active(False)
while Gtk.events_pending():
    Gtk.main_iteration()
check("10 with only system on, the system toggle cannot be turned off",
      overlay.btn_sys.get_sensitive() is False,
      f"(sys sensitive={overlay.btn_sys.get_sensitive()}, "
      f"mic sensitive={overlay.btn_mic.get_sensitive()})")

# --- CHECK 11: and the lock lifts as soon as the other one is on ------------
overlay.btn_mic.set_active(True)
while Gtk.events_pending():
    Gtk.main_iteration()
check("11 turning the second source on unlocks the first",
      overlay.btn_sys.get_sensitive() is True
      and overlay.btn_mic.get_sensitive() is True
      and overlay.rec.mode == "meeting",
      f"(sys={overlay.btn_sys.get_sensitive()} "
      f"mic={overlay.btn_mic.get_sensitive()} mode={overlay.rec.mode!r})")

# --- CHECK 14: the adversarial one ------------------------------------------
#
# Everything above asks "does the toggle change the mode?". None of it can
# express the failure where the toggle changes the mode and the ENGINE IS NOT
# RECORDING -- because every check above runs on a Recorder that start()ed
# first. That is not hypothetical: set_sources() branches on
# `self.state != "recording"` and returns early without cutting a segment, and
# a paused session is exactly when a user reaches for these toggles ("wait, let
# me add my mic before I carry on").
#
# The bug shape: the early return sets self.mode but never spawns, so resume()
# must be the thing that applies it. If a future edit makes set_sources() spawn
# unconditionally, a paused session gains a segment while paused -- a recording
# that runs while the UI says paused, which is the "records you through a mute
# you believe protects you" failure this project refuses elsewhere.
#
# So: toggle while PAUSED, and assert both halves -- nothing captured during the
# pause, and the choice honoured on resume.
rec3 = DryRecorder(mode="system", name="chk4")
rec3.start()
rec3.pause()
segs_paused = len(rec3.segments)
applied = rec3.set_sources(True, True)
spawned_while_paused = len(rec3.segments) - segs_paused
rec3.resume()
c3 = " ".join(rec3.spawned[-1])
check("14 toggling while paused captures nothing, and applies on resume",
      applied is True and spawned_while_paused == 0
      and rec3.state == "recording"
      and "mon.monitor" in c3 and "mic0" in c3,
      f"(applied={applied} segments spawned while paused="
      f"{spawned_while_paused} state={rec3.state!r})")

overlay.destroy()
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
