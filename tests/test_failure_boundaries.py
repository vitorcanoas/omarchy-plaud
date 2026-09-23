"""SPEC-0014: nine isolated recording-failure boundary regressions.

Run under a separate D-Bus session. All files use a scratch PLAUD_LINUX_HOME;
network and ffmpeg are replaced with synthetic doubles.
"""
import json
import os
import pathlib
import sys
import tempfile
import types
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp(prefix="plaud-boundaries-")
sys.path.insert(0, str(ROOT))

requests_stub = types.ModuleType("requests")
def no_network(*args, **kwargs):
    raise AssertionError("NETWORK CALL — test invalid")
requests_stub.get = requests_stub.post = requests_stub.put = no_network
requests_stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = requests_stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib
from plaud_linux import audio, library, main, overlay, paths

results = []

def check(ac, ok, detail):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  SPECSFY:{ac} {detail}", flush=True)

@contextmanager
def patch(obj, **values):
    old = {key: getattr(obj, key) for key in values}
    try:
        for key, value in values.items():
            setattr(obj, key, value)
        yield
    finally:
        for key, value in old.items():
            setattr(obj, key, value)

def drain_idle():
    ctx = GLib.MainContext.default()
    for _ in range(100):
        if not ctx.pending():
            break
        ctx.iteration(False)

class Control:
    def __init__(self):
        self.sensitive = True
        self.active = True
    def set_sensitive(self, value):
        self.sensitive = bool(value)
    def set_active(self, value):
        self.active = bool(value)

class StopOwner:
    """The real Overlay stop methods run on this small widget/recorder surface."""
    def __init__(self, rec, on_stop):
        self.rec = rec
        self.on_stop = on_stop
        self._stopping = False
        self._shots_taken = False
        self.btn_stop = Control()
        self.wave = Control()
        self.destroyed = 0
        self.messages = []
    def destroy(self):
        self.destroyed += 1
    def _notify(self, title, body):
        self.messages.append(body)

def stop_request(owner):
    # Model the button's immediate visual acknowledgement, then exercise the
    # real shared owner synchronously (the signal uses this same method).
    owner.btn_stop.set_sensitive(False)
    owner.wave.set_active(False)
    try:
        overlay.Overlay._do_stop(owner)
        return None
    except Exception as error:
        return error

class Process:
    def __init__(self):
        self.alive = True
        self.signals = 0
    def poll(self):
        return None if self.alive else 0
    def send_signal(self, value):
        self.signals += 1
    def wait(self, timeout=None):
        self.alive = False
        return 0
    def kill(self):
        raise AssertionError("unfinalized audio must not be force-killed")

# SPECSFY: US-001 FR-001 NFR-001 AC-001
class RetryRec:
    def __init__(self):
        self.proc = Process()
        self.state = "recording"
        self.calls = 0
    def stop(self):
        self.calls += 1
        if self.calls == 1:
            raise OSError("synthetic stop failure")
        self.proc.alive = False
        self.proc = None
        self.state = "stopped"

rec = RetryRec()
handoffs = []
owner = StopOwner(rec, lambda value: handoffs.append(value))
first_error = stop_request(owner)
first_safe = (rec.proc is not None and rec.proc.poll() is None and owner.destroyed == 0
              and not handoffs and owner.btn_stop.sensitive and owner.wave.active
              and not owner._stopping)
second_error = stop_request(owner)
overlay.Overlay._do_stop(owner)

class UnstoppableTap:
    """The optional mark ffmpeg emits raw PCM, not an Opus file to finalize."""
    def __init__(self):
        self.proc = Process()
    def stop(self):
        raise OSError("synthetic tap kill/wait failure")

tap_rec = audio.Recorder(mode="system", name="ac001_tap")
tap_rec.proc = Process()
tap_rec.state = "recording"
tap = UnstoppableTap()
tap_rec.mark_buffer = tap
tap_rec._concat = lambda: None
tap_rec._save_meta = lambda: None
tap_handoffs = []
tap_owner = StopOwner(tap_rec, lambda value: tap_handoffs.append(value))
tap_error = stop_request(tap_owner)
tap_visible = (tap.proc.poll() is None and tap_rec.mark_buffer is tap
               and tap_owner.destroyed == 0 and not tap_handoffs
               and tap_owner.btn_stop.sensitive and tap_owner.wave.active)

restarted = audio.Recorder(mode="system", name="ac001_resume")
live_tap = UnstoppableTap()
restarted.mark_buffer = live_tap
new_taps = []
with patch(audio, pick_system_monitor=lambda: ("test.sink.monitor", "test.sink", "sink-default"),
           default_source=lambda: "test.mic",
           MarkBuffer=lambda *args: new_taps.append(object()) or new_taps[-1]):
    restarted._start_mark_buffer()
tap_handle_kept = restarted.mark_buffer is live_tap and not new_taps
check("AC-001", first_safe and second_error is None and rec.calls == 2
      and owner.destroyed == 1 and len(handoffs) == 1 and tap_visible
      and tap_handle_kept,
      f"active_visible_retry={first_safe} tap_visible={tap_visible} "
      f"tap_handle_kept={tap_handle_kept} "
      f"calls={rec.calls} handoffs={len(handoffs)}")

def stopped_rec(name, failure):
    rec = audio.Recorder(mode="system", name=name)
    rec.proc = Process()
    rec.state = "recording"
    segment = rec.segdir / "seg_000.opus"
    segment.write_bytes(b"OggS" + b"a" * 4000)
    rec.segments.append(segment)
    rec.final_path.write_bytes(b"OggS" + b"o" * 4000)  # stale or pre-finalized
    if failure == "concat":
        rec._concat = lambda: (_ for _ in ()).throw(OSError("concat failed"))
    else:
        rec._concat = lambda: rec.final_path
        rec._save_meta = lambda: (_ for _ in ()).throw(OSError("metadata failed"))
    return rec, segment

class NoUploadClient:
    def __init__(self, calls):
        self.calls = calls
    def is_logged_in(self):
        return True
    def upload_and_generate(self, *args, **kwargs):
        self.calls.append("upload")
        return "file-test"

def failed_stop_case(name, failure):
    rec, segment = stopped_rec(name, failure)
    uploads, ended, notifications = [], [], []
    def handoff(value):
        main._do_upload(value, lambda: ended.append(True))
    owner = StopOwner(rec, handoff)
    with patch(main.plaud_api, PlaudClient=lambda *a, **k: NoUploadClient(uploads)), \
         patch(main, notify=lambda title, body: notifications.append(body),
               peak_dbfs=lambda path: -20.0):
        error = stop_request(owner)
        drain_idle()
        overlay.Overlay._do_stop(owner)
        drain_idle()
    return (error, owner, rec, segment, uploads, ended, notifications)

# SPECSFY: US-001 FR-001 NFR-001 AC-002
error, owner, rec, segment, uploads, ended, notices = failed_stop_case("ac002", "concat")
check("AC-002", rec.proc is None and segment.exists() and owner.destroyed == 1
      and len(ended) == 1 and not uploads,
      f"quiescent={rec.proc is None} segment={segment.exists()} ended={len(ended)} uploads={len(uploads)}")

# SPECSFY: US-001 FR-001 NFR-001 AC-003
error, owner, rec, segment, uploads, ended, notices = failed_stop_case("ac003", "metadata")
check("AC-003", rec.proc is None and rec.final_path.exists() and owner.destroyed == 1
      and len(ended) == 1 and not uploads and bool(notices or owner.messages),
      f"audio={rec.final_path.exists()} ended={len(ended)} uploads={len(uploads)} feedback={bool(notices or owner.messages)}")

class SpawnedProcess(Process):
    pass

def source_case(mode, probes):
    calls = []
    iterator = iter(probes)
    def probe():
        return next(iterator)
    def spawn(cmd, **kwargs):
        calls.append(cmd)
        return SpawnedProcess()
    rec = audio.Recorder(mode=mode, name=f"source_{mode}_{len(results)}")
    rec._start_mark_buffer = lambda: None
    with patch(audio, pick_system_monitor=probe, default_source=lambda: "test.mic",
               ), patch(audio.subprocess, Popen=spawn):
        try:
            rec.start()
            error = None
        except Exception as exc:
            error = exc
    return rec, calls, error

# SPECSFY: US-002 FR-002 NFR-002 AC-004
rec, calls, error = source_case("system", [(None, None, "sem-sink")])
session_calls, session_messages = [], []
constructed, destroyed = [], []
def build_minimal_window(window):
    # Real Gtk.Window/Overlay constructor and run path; the small UI stand-in
    # only avoids an unrelated visual-layout dependency in this boundary test.
    constructed.append(window)
    window.btn_stop = Control()
    window.wave = Control()
    window.connect("destroy", lambda *_: destroyed.append(window))
with patch(main, ensure_login_or_prompt=lambda: True,
           pick_sources=lambda: (True, False), wire_login_handler=lambda app: None,
           notify=lambda title, body: session_messages.append(body)), \
     patch(paths, prune_old_logs=lambda: None), \
     patch(overlay.Overlay, _init_movable=lambda self: True,
           _apply_css=lambda self: None, _build_ui=build_minimal_window), \
     patch(audio, pick_system_monitor=lambda: (None, None, "sem-sink"),
           default_source=lambda: "test.mic"), \
     patch(audio.subprocess, Popen=lambda cmd, **kwargs: session_calls.append(cmd) or SpawnedProcess()):
    try:
        session_result = main.start_session()
    except Exception:
        session_result = "uncaught error"
check("AC-004", not calls and rec.proc is None and rec.state != "recording"
      and error is not None and not session_calls and session_result is False
      and bool(session_messages) and len(constructed) == len(destroyed),
      f"spawn={len(calls) + len(session_calls)} refused={session_result is False} "
      f"feedback={bool(session_messages)} windows={len(constructed)}/{len(destroyed)}")

# SPECSFY: US-002 FR-002 NFR-002 AC-005
rec, calls, error = source_case("meeting", [(None, None, "sem-sink")])
check("AC-005", not calls and rec.proc is None and rec.state != "recording"
      and error is not None,
      f"spawn={len(calls)} state={rec.state} refused={error is not None}")

# SPECSFY: US-002 FR-002 NFR-002 AC-006
reprobes = iter([(None, None, "sem-sink"),
                 ("test.sink.monitor", "test.sink", "sink-default")])
calls = []
rec = audio.Recorder(mode="system", name="ac006_system")
rec._start_mark_buffer = lambda: None
with patch(audio, pick_system_monitor=lambda: next(reprobes),
           default_source=lambda: "test.mic"), \
     patch(audio.subprocess, Popen=lambda cmd, **kwargs: calls.append(cmd) or SpawnedProcess()):
    try:
        rec.start()
    except Exception:
        pass
    try:
        rec.start()
    except Exception:
        pass
system_calls = list(calls)
mic, mic_calls, mic_error = source_case("mic", [(None, None, "sem-sink")])
switch_calls = []
switch_probes = iter([(None, None, "sem-sink"),
                      (None, None, "sem-sink"),
                      ("new.sink.monitor", "new.sink", "sink-default")])
switch_rec = audio.Recorder(mode="mic", name="ac006_switch")
switch_rec._start_mark_buffer = lambda: None
with patch(audio, pick_system_monitor=lambda: next(switch_probes),
           default_source=lambda: "test.mic"), \
     patch(audio.subprocess, Popen=lambda cmd, **kwargs: switch_calls.append(cmd) or SpawnedProcess()):
    switch_rec.start()
    refused_switch = switch_rec.set_sources(True, False)
    still_mic = switch_rec.mode == "mic" and switch_rec.state == "recording"
    accepted_switch = switch_rec.set_sources(True, False)
check("AC-006", len(system_calls) == 1 and "test.sink.monitor" in system_calls[0]
      and "default" not in system_calls[0] and len(mic_calls) == 1
      and "test.mic" in mic_calls[0] and mic_error is None
      and refused_switch is False and still_mic and accepted_switch is True
      and len(switch_calls) == 2 and "new.sink.monitor" in switch_calls[-1],
      f"system_spawns={len(system_calls)} mic_spawns={len(mic_calls)} "
      f"switch_refused={refused_switch is False} switch_spawns={len(switch_calls)}")

SENTINEL = "FAKE_SECRET_SPEC0014_DO_NOT_PERSIST"

def upload_rec(name, *, notes=()):
    rec = audio.Recorder.__new__(audio.Recorder)
    rec.session, rec.ts, rec.mode, rec.mic = name, "20260923_120000", "system", False
    rec.state, rec.started_at, rec.paused_accum, rec.paused_at = "stopped", None, 0.0, None
    rec.segdir = paths.RECORDINGS / f".seg_{name}"
    rec.segdir.mkdir(parents=True, exist_ok=True)
    rec.segments = []
    rec.final_path = paths.RECORDINGS / f"{name}.opus"
    rec.final_path.write_bytes(b"OggS" + b"x" * 12000)
    rec.meta_path = paths.RECORDINGS / f"{name}.json"
    rec.screenshots, rec.notes, rec.flags = [], list(notes), []
    rec.upload, rec.concat_failed, rec.segments_lost, rec._merged = None, False, 0, False
    rec._save_meta()
    return rec

class CloudFailure:
    def __init__(self, *, fail="upload"):
        self.fail = fail
        self.tokens = {"ws_id": "test"}
        self.device = "00000000-0000-0000-0000-000000000000"
    def is_logged_in(self):
        return True
    def upload_and_generate(self, *args, **kwargs):
        if self.fail == "upload":
            raise RuntimeError("upload rejected " + SENTINEL)
        return "file0014"
    def attach_screenshots(self, *args, **kwargs):
        if self.fail == "annotation":
            raise RuntimeError("annotation rejected " + SENTINEL)
        return 1
    def generate(self, *args, **kwargs):
        if self.fail == "generation":
            raise RuntimeError("generation rejected " + SENTINEL)
        return {"status": 0}

def run_cloud(rec, *, fail, choice=None, resend=False):
    messages, ended = [], []
    client = CloudFailure(fail=fail)
    with patch(main.plaud_api, PlaudClient=lambda *a, **k: client), \
         patch(main, notify=lambda title, body: messages.append(body),
               peak_dbfs=lambda path: -20.0,
               _ask_generation_mode_sync=lambda: choice,
               _open_generation=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("custom " + SENTINEL))):
        if resend:
            main.resend(rec, done=lambda: ended.append(True))
        else:
            main._do_upload(rec, lambda: ended.append(True))
        # Workers perform only synthetic CPU/filesystem work; join their real threads.
        import threading
        for thread in list(threading.enumerate()):
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(timeout=5)
        drain_idle()
    return messages, ended

def sidecar(rec):
    return rec.meta_path.read_text()

def safe(messages, persisted=""):
    upload = json.loads(persisted).get("upload") or {} if persisted else {}
    stored_error = upload.get("error") or ""
    return (bool(messages) and SENTINEL not in "\n".join(messages) + persisted
            and "rejected" not in "\n".join(messages) + stored_error
            and all(len(message) <= 240 for message in messages)
            and len(stored_error) <= 240)

# SPECSFY: US-003 FR-003 NFR-003 AC-007
rec = upload_rec("ac007")
messages, ended = run_cloud(rec, fail="upload")
check("AC-007", safe(messages, sidecar(rec)) and len(ended) == 1
      and rec.final_path.exists() and "error" in sidecar(rec),
      f"safe={safe(messages, sidecar(rec))} done={len(ended)} audio={rec.final_path.exists()}")

# SPECSFY: US-003 FR-003 NFR-003 AC-008
rec = upload_rec("ac008", notes=[{"text": "keep local note", "t": 1}])
entry = library.Entry(rec.meta_path, json.loads(sidecar(rec)))
mark_failed_values = []
original_mark_failed = entry.mark_failed
def observed_mark_failed(value):
    mark_failed_values.append(value)
    return original_mark_failed(value)
entry.mark_failed = observed_mark_failed
messages, ended = run_cloud(entry, fail="upload", resend=True)
persisted = entry.meta_path.read_text()
check("AC-008", safe(messages, persisted) and len(ended) == 1
      and entry.final_path.exists() and "keep local note" in persisted
      and len(mark_failed_values) == 1 and isinstance(mark_failed_values[0], str)
      and SENTINEL not in mark_failed_values[0] and len(mark_failed_values[0]) <= 240,
      f"safe={safe(messages, persisted)} done={len(ended)} "
      f"safe_mark_failed={bool(mark_failed_values) and isinstance(mark_failed_values[0], str) and SENTINEL not in mark_failed_values[0]}")

# SPECSFY: US-003 FR-003 NFR-003 AC-009
subcases = []
for resend in (False, True):
    for stage, choice in (("annotation", None), ("generation", "auto"),
                          ("custom", "custom")):
        rec = upload_rec(f"ac009_{int(resend)}_{stage}", notes=[{"text": "note", "t": 1}])
        entry = library.Entry(rec.meta_path, json.loads(sidecar(rec))) if resend else rec
        messages, ended = run_cloud(entry, fail=stage, choice=choice, resend=resend)
        persisted = entry.meta_path.read_text()
        upload_state = json.loads(persisted).get("upload") or {}
        subcases.append((safe(messages, persisted), len(ended) == 1,
                         upload_state.get("file_id") == "file0014",
                         entry.final_path.exists()))

# The custom picker and completion monitor report through separate asynchronous
# helper sinks after the upload worker has already returned.
async_messages = []
with patch(main, notify=lambda title, body: async_messages.append(body),
           _generation_requested=lambda *a, **k: (_ for _ in ()).throw(
               RuntimeError("custom monitor " + SENTINEL))):
    main._custom_generation_requested(object(), "file0014")
    drain_idle()
custom_async_safe = safe(async_messages)

class FailingMonitor:
    def wait_for_generation(self, *args, **kwargs):
        raise RuntimeError("monitor response " + SENTINEL)

async_messages = []
with patch(main, notify=lambda title, body: async_messages.append(body),
           _desktop_progress=object()):
    main._generation_requested(FailingMonitor(), "file0014", {"status": 0})
    import threading
    for thread in list(threading.enumerate()):
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(timeout=5)
monitor_async_safe = safe(async_messages)

check("AC-009", all(all(parts) for parts in subcases)
      and custom_async_safe and monitor_async_safe,
      f"safe={sum(parts[0] for parts in subcases)}/{len(subcases)} "
      f"identity={sum(parts[2] for parts in subcases)}/{len(subcases)} "
      f"async_safe={int(custom_async_safe) + int(monitor_async_safe)}/2")

print(f"\n{sum(results)}/{len(results)} passed", flush=True)
sys.exit(0 if all(results) else 1)
