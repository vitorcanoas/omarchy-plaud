#!/usr/bin/env python3
"""Checks for CAN-309 phase 1: the 40 s rolling mark ring in audio.py.

Usage: python3 tests/test_markring.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).

No network, no GTK, no PulseAudio server needed: the tap is a real subprocess,
but it is `cat`/`ffmpeg -f lavfi` reading synthetic input rather than a sound
card, so these run anywhere. The one thing that CANNOT be faked and is
therefore checked against real ffmpeg is dump_ogg's encoding.

The invariant these exist to protect is not the feature. It is Principle II:
a tap that fails to spawn, dies mid-session, or hangs must leave the recording
completely unaffected. Checks 5-8 are that, and they are the ones worth having.
"""
import os, pathlib, subprocess, sys, tempfile, time

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="markringchk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

from plaud_linux import audio

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))
    ok_all = ok_all and bool(ok)


# ---------------------------------------------------------------- 1: constant
# 40, not 60. The official client's own constant
# (recordingService-Mile2cgU.js:183) and a settled user decision. A drift to 60
# would quietly change what every flag transcribes, and nothing else would say so.
check("1 window is 40 s of 48 kHz s16le mono, per the official constant",
      audio.MARK_SECONDS == 40 and audio.MARK_RATE == 48000
      and audio.MARK_BYTES == 48000 * 2 * 40,
      f"MARK_SECONDS={audio.MARK_SECONDS} MARK_BYTES={audio.MARK_BYTES}")


# ------------------------------------------------------- 2: the ring is rolling
# Drive the REAL pump with more than the cap and assert it evicts from the FRONT:
# the newest audio survives, the oldest is dropped. Written the first way -- doing
# the eviction inline in the check and asserting the result -- this passed against
# a pump with its eviction deleted, i.e. it could not fail. So the tap here is a
# real subprocess emitting a known byte pattern, and the pump is the only thing
# that trims it. A ring that grew without bound, or that kept the OLDEST bytes,
# would both look fine in a short smoke test and be wrong in a long meeting.
mb = audio.MarkBuffer(["-f", "lavfi", "-i", "anullsrc"])
over = audio.MARK_BYTES + (1 << 20)
# `head -c` on urandom is a stand-in for the tap: it floods the pipe with more
# than the cap and exits, so the pump must do the trimming or the ring overflows.
mb._input_args = None
mb.proc = subprocess.Popen(
    ["sh", "-c", f"head -c {over - 8} /dev/zero; printf TAILTAIL"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
import threading as _t
_pump = _t.Thread(target=mb._pump, args=(mb.proc,), daemon=True)
_pump.start()
_pump.join(timeout=30)
snap = mb.snapshot()
check("2 the pump caps the ring at 40 s and evicts the oldest bytes",
      len(snap) == audio.MARK_BYTES and snap.endswith(b"TAILTAIL"),
      f"len={len(snap)} cap={audio.MARK_BYTES} newest_kept={snap.endswith(b'TAILTAIL')}")
mb.stop()


# ------------------------------------------- 3: the tap reads the SESSION's mix
# The requirement that is easiest to get subtly wrong: a tap that re-picks its
# own source would take the monitor alone, so a flag in a meeting would omit the
# user's own voice. Assert the tap's input args are IDENTICAL to the capture's,
# for every mode -- including that a meeting carries the amix filter.
def tap_args(mode, mon, mic):
    r = audio.Recorder.__new__(audio.Recorder)
    r.mode = mode
    r.mic = mic or mode == "meeting"
    r._src = (mon, "sink0", "micsrc", "forced")
    return r._input_args()[0]


sys_args = tap_args("system", "sink0.monitor", False)
mic_args = tap_args("mic", "sink0.monitor", False)
meet_args = tap_args("meeting", "sink0.monitor", True)
check("3 the tap's input args are the session's own, mic-only and mixed alike",
      sys_args == ["-f", "pulse", "-i", "sink0.monitor"]
      and mic_args == ["-f", "pulse", "-i", "micsrc"]
      and "amix=inputs=2" in " ".join(meet_args)
      and "micsrc" in meet_args and "sink0.monitor" in meet_args,
      f"system={sys_args} mic={mic_args} meeting_has_amix={'amix=inputs=2' in ' '.join(meet_args)}")


# ------------------------------------ 4: the ring does not survive a pause
# Requirement 4. The tap must not capture while paused, and resume must start
# from an empty ring -- otherwise a flag just after resume returns audio that is
# not in the recording, timestamped against an elapsed() that froze.
rec = audio.Recorder(mode="system", name="markring_pause")
rec._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
rec.start()
buf_during = rec.mark_buffer
if buf_during is not None:
    buf_during._buf.extend(b"\xff" * 1000)   # pretend audio accumulated
had = buf_during is not None and len(buf_during.snapshot()) > 0
rec.pause()
gone = rec.mark_buffer is None
tap_dead = buf_during is None or buf_during.proc is None
rec.resume()
after = rec.mark_buffer
fresh = after is not None and len(after.snapshot()) < 1000
check("4 pause tears the tap down and resume starts from an empty ring",
      had and gone and tap_dead and fresh,
      f"had_audio={had} cleared_on_pause={gone} tap_released={tap_dead} empty_after_resume={fresh}")
rec.stop()


# ============================================================================
# Principle II. Everything below is "a broken tap must not cost a recording".
# ============================================================================

# ------------------------------------------ 5: a tap that cannot spawn is silent
# The precedent is _run()'s FileNotFoundError: a missing tool degrades, it does
# not take the recording down. If MarkBuffer.start() ever raised, Recorder.start()
# would raise, and Overlay.__init__ calls start() before show_all() -- the pill
# would never appear and the only trace would be a traceback in app.log.
broken = audio.MarkBuffer(["-f", "pulse", "-i", "x"])
raised = None
try:
    orig = subprocess.Popen
    subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no ffmpeg"))
    broken.start()
except BaseException as e:
    raised = e
finally:
    subprocess.Popen = orig
check("5 a tap that cannot spawn raises nothing and leaves no process",
      raised is None and broken.proc is None,
      f"raised={raised!r} proc={broken.proc!r}")


# ------------------------- 6: a tap that explodes cannot abort Recorder.start()
# The real guarantee, at the level that matters: whatever MarkBuffer does,
# start() must still produce a capture process and a segment.
class Exploding(audio.MarkBuffer):
    def start(self):
        raise RuntimeError("tap detonated")


orig_mb = audio.MarkBuffer
audio.MarkBuffer = Exploding
rec2 = audio.Recorder(mode="system", name="markring_explode")
rec2._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
start_raised = None
try:
    rec2.start()
except BaseException as e:
    start_raised = e
finally:
    audio.MarkBuffer = orig_mb
recording_live = rec2.state == "recording" and rec2.proc is not None and len(rec2.segments) == 1
check("6 a tap that raises on start does not abort the recording",
      start_raised is None and recording_live and rec2.mark_buffer is None,
      f"raised={start_raised!r} state={rec2.state} segments={len(rec2.segments)}")
rec2.stop()


# ------------------------------ 7: a tap killed mid-session does not reach stop()
# Measured for real against a sound card (SIGKILL mid-recording: tap poll -9,
# capture poll None, .opus intact at -21.1 dB). Here the same shape is asserted
# without hardware: kill the tap, then require stop() to complete and the
# recorder to reach "stopped" with no exception.
rec3 = audio.Recorder(mode="system", name="markring_kill")
rec3._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
rec3.start()
tap3 = rec3.mark_buffer
killed = False
if tap3 is not None and tap3.proc is not None:
    tap3.proc.kill()
    tap3.proc.wait(timeout=5)
    killed = True
stop_raised = None
t3 = time.time()
try:
    rec3.stop()
except BaseException as e:
    stop_raised = e
stop_dt = time.time() - t3
# Bounded, not just "did not raise": a teardown that blocks on a dead tap does
# not raise either -- it never returns. Mutating stop() to wait() on the killed
# process hung this file until the suite's own 900 s timeout, which reports as
# "exited with no result" rather than naming the defect. The bound makes it a
# FAIL line instead.
check("7 a tap killed mid-session lets stop() finish, promptly and without raising",
      stop_raised is None and rec3.state == "stopped" and stop_dt < 15.0,
      f"tap_killed={killed} raised={stop_raised!r} state={rec3.state} took={stop_dt:.1f}s")


# ------------------------------------------ 8: stop() is not delayed by the tap
# A tap that could stall stop() would hold the upload -- and the tray refuses to
# quit while an upload is live, so a wedged tap would make the app unquittable.
# The tap writes raw PCM to a pipe, so there is no container to finalize and it
# is SIGKILLed rather than SIGINTed. Assert the teardown is prompt.
rec4 = audio.Recorder(mode="system", name="markring_timing")
rec4._src = ("nonexistent-source-for-test.monitor", "s", "m", "forced")
rec4.start()
t0 = time.time()
rec4._stop_mark_buffer()
dt = time.time() - t0
check("8 tearing the tap down is prompt, so it can never stall stop()",
      dt < 2.0 and rec4.mark_buffer is None, f"took {dt*1000:.0f} ms")
rec4.stop()


# ---------------------------------------------- 9: empty ring makes no artifact
# Mirrors the official client's zero-length bail
# (recordingService-Mile2cgU.js:1033): a flag with nothing buffered is skipped
# client-side, before any network call. Returning a path to an empty file here
# would send an empty upload in phase 3 and attach a mark that transcribes
# nothing.
empty = audio.MarkBuffer(["-f", "lavfi", "-i", "anullsrc"])
target = pathlib.Path(HOME) / "should_not_exist.ogg"
check("9 an empty ring dumps nothing and writes no file",
      empty.dump_ogg(target) is None and not target.exists())


# ------------------------------------- 10: dump_ogg's encoding is the real thing
# The check that must use real ffmpeg. Encode a known 440 Hz tone through
# dump_ogg and verify BOTH that the container/codec match what the backend
# expects (Ogg/Opus mono) AND that the audio is actually there. Duration and
# size prove nothing -- a silent Opus is a plausible-looking file -- so this
# gates on measured mean volume being far above the silence floor.
gen = subprocess.run(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
     "-i", "sine=frequency=440:duration=3:sample_rate=48000",
     "-ac", "1", "-f", "s16le", "-"], capture_output=True)
tone_pcm = gen.stdout
mb2 = audio.MarkBuffer(["-f", "lavfi", "-i", "anullsrc"])
mb2._buf.extend(tone_pcm)
out = pathlib.Path(HOME) / "mark.ogg"
got = mb2.dump_ogg(out)

probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries",
     "stream=codec_name,channels:format=format_name,duration",
     "-of", "default=nw=1", str(out)], capture_output=True, text=True).stdout
vol = subprocess.run(
    ["ffmpeg", "-hide_banner", "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
    capture_output=True, text=True).stderr
mean = None
for line in vol.splitlines():
    if "mean_volume:" in line:
        mean = float(line.split("mean_volume:")[1].strip().split()[0])
dur = None
for line in probe.splitlines():
    if line.startswith("duration="):
        dur = float(line.split("=")[1])

check("10 dump_ogg writes real Ogg/Opus mono, not a plausible-looking silence",
      got == out and "codec_name=opus" in probe and "channels=1" in probe
      and "ogg" in probe and dur is not None and 2.5 < dur < 3.5
      and mean is not None and mean > -85.0,
      f"codec/format={probe.strip().replace(chr(10), ' ')} dur={dur} mean_volume={mean} dB")


# ------------------------------------- 11: the tap is not seen as playing audio
# pick_system_monitor() follows sink-inputs. A capture client is a source-output,
# so the tap must not perturb the choice -- otherwise turning the flag on would
# change which device the RECORDING captures. CAN-320 just changed this selection
# code, so it is re-asserted here rather than assumed. Stubbed, so it holds as a
# regression guard on any machine: the tap adds a source-output and nothing else.
seen = {"sink_inputs": 0}
orig_run = audio._run


def fake_run(cmd):
    if "sink-inputs" in cmd:
        seen["sink_inputs"] += 1
        return ""            # the tap contributes NO sink-input
    if "sources" in cmd or "source-outputs" in cmd:
        return "0\tcan309.monitor\tPipeWire\ts16le\tRUNNING\n"
    if cmd[:3] == ["pactl", "get-default-sink"]:
        return "sink0\n"
    if "sinks" in cmd:
        return "0\tsink0\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    return ""


audio._run = fake_run
try:
    before = audio.pick_system_monitor()
    probe_mb = audio.MarkBuffer(["-f", "lavfi", "-i", "anullsrc"])
    probe_mb.start()
    time.sleep(0.5)
    after = audio.pick_system_monitor()
    grew = len(probe_mb.snapshot()) > 0
    probe_mb.stop()
finally:
    audio._run = orig_run
check("11 a live tap leaves pick_system_monitor's answer identical",
      before == after and grew and seen["sink_inputs"] > 0,
      f"before={before} after={after} ring_grew={grew}")


# --------------------------------- 12: a flag is recorded and survives a crash
# The snippet is worthless if nothing remembers when it was taken: phase 3 sends
# `t` as the mark's timestamp. It goes in the sidecar for the same reason the
# notes do -- that file is the only record that outlives the process. `t` is
# passed in rather than re-derived so the timestamp is the moment of the press,
# not whenever the upload thread happens to look.
rec5 = audio.Recorder(mode="system", name="markring_flag")
snip = pathlib.Path(HOME) / "snip.ogg"
snip.write_bytes(b"x")
rec5.add_flag(snip, 17)
side = audio.read_json(rec5.meta_path) or {}
entry = (rec5.flags or [None])[0]
check("12 add_flag records the snippet and its timestamp, in the sidecar too",
      entry is not None and entry["path"] == str(snip) and entry["t"] == 17
      and side.get("flags") == rec5.flags,
      f"flags={rec5.flags} sidecar_flags={side.get('flags')}")


print(f"\n{'ALL PASS' if ok_all else 'SOME FAILED'}")
sys.exit(0 if ok_all else 1)
