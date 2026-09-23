"""Checks for CAN-310: the "Gerar automaticamente vs Gerar personalizada"
choice the official client offers once an upload has finished.

Usage: python3 tests/test_genchoice.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

What the official client does, from docs/plaud-desktop/README.md §6 and the
two screenshots it cites: once the upload completes, a small separate window
titled "Pronto para gerar" asks "Escolha como pretende gerar esta nota" and
offers **Gerar automaticamente** (defaults) or **Gerar personalizada**, which
hands off to Plaud Web (media/image45.png shows web.plaud.ai with the
"Selecionar método de geração" panel open on that note).

The Linux side already had half of it: plaud_api.upload_and_generate() has
taken an `auto_generate` parameter since it was written, and main.py never
passed it -- every recording was generated automatically with no choice.

The load-bearing risk is NOT the dialog, it is Principle II. This code runs on
the upload worker AFTER the audio is on the server, so anything that can hang
or raise between the upload and the notification strands a recording the user
is never told about. CHECKS 4-8 are all about that: dismissing, a dialog that
raises, and no GTK loop at all must each still finish the session, still
notify, and still attach the screenshots.
"""
import os, pathlib, sys, tempfile, threading, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="genchoice-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

# Any real network call means the check is measuring the internet, not this.
stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from plaud_linux import audio, paths
import plaud_linux.main as main_mod
import plaud_linux.plaud_api as plaud_api

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


AUDIO = b"OggS" + b"x" * 12000   # over the 2000-byte floor _do_upload enforces


def make_rec(name, shots=(), notes=()):
    """A stopped Recorder with a real .opus on disk and no ffmpeg anywhere.

    __new__ rather than __init__ for the same reason as test_staleupload: the
    constructor probes for a capture source. Every attribute _do_upload()
    reads is set explicitly.
    """
    r = audio.Recorder.__new__(audio.Recorder)
    r.session, r.ts, r.mode, r.mic = name, "20260903_120000", "system", False
    r.state, r.started_at, r.paused_accum, r.paused_at = "stopped", None, 0.0, None
    r.segdir = paths.RECORDINGS / f".seg_{name}"
    r.segdir.mkdir(parents=True, exist_ok=True)
    r.segments = []
    r.final_path = paths.RECORDINGS / f"{name}.opus"
    r.final_path.write_bytes(AUDIO)
    r.meta_path = paths.RECORDINGS / f"{name}.json"
    r.screenshots, r.notes, r.upload = list(shots), list(notes), None
    r.concat_failed, r.segments_lost, r._merged = False, 0, False
    return r


class FakeClient:
    """Stands in for PlaudClient. Records exactly which calls the worker made."""
    def __init__(self, generate_raises=None):
        self.tokens = {"ws_id": "ws_TEST123"}
        self.device = "11111111-2222-3333-4444-555555555555"
        self.calls = []
        self.generate_raises = generate_raises

    def is_logged_in(self):
        return True

    def upload_and_generate(self, path, filename=None, progress=None, auto_generate=True):
        self.calls.append(("upload", auto_generate))
        return "file_ABC"

    def generate(self, file_id, progress=None):
        self.calls.append(("generate", file_id))
        if self.generate_raises:
            raise self.generate_raises
        return {"status": 0}

    def attach_screenshots(self, file_id, shots, notes=None, progress=None,
                           flags=None):
        # The signature has to track the real one. A double that is missing a
        # keyword the caller passes raises TypeError inside _do_upload()'s
        # try, which reports "falha no upload" for a recording already on the
        # server -- so the drift shows up as a wrong message, not as an error.
        self.calls.append(("attach", file_id))
        return len(shots) + len(notes or []) + len(flags or [])


def run_upload(rec, choice, generate_raises=None, ask=None):
    """Drive the REAL main._do_upload() with a fake client and a fixed choice.

    Only three seams are replaced: the client (no network), notify (captured),
    and the GTK-thread hop (which needs a real loop otherwise). Everything
    between the upload call and the notification is the production code path.
    """
    client = FakeClient(generate_raises=generate_raises)
    said, opened, ended = [], [], []
    orig = (main_mod.plaud_api.PlaudClient, main_mod.notify, main_mod.peak_dbfs,
            main_mod._open_url, main_mod._ask_generation_mode_sync,
            main_mod._record_outcome, main_mod._open_generation)
    main_mod.plaud_api.PlaudClient = lambda *a, **k: client
    main_mod.notify = lambda t, b, **k: said.append(b)
    main_mod.peak_dbfs = lambda p: -20.0
    main_mod._open_url = lambda url: opened.append(url) or True
    main_mod._open_generation = lambda url, *a, **k: main_mod._open_url(url)
    main_mod._ask_generation_mode_sync = ask or (lambda *a, **k: choice)
    main_mod._record_outcome = lambda r, **k: None
    try:
        main_mod._do_upload(rec, lambda: ended.append(True))
        # _do_upload starts a daemon thread; wait for it to finish.
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=30)
    finally:
        (main_mod.plaud_api.PlaudClient, main_mod.notify, main_mod.peak_dbfs,
         main_mod._open_url, main_mod._ask_generation_mode_sync,
         main_mod._record_outcome, main_mod._open_generation) = orig
    # `done` comes back through GLib.idle_add, so drain the loop to see it.
    for _ in range(200):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)
    return client, said, opened, ended


# ---------------------------------------------------------------- CHECK 1
# The literal defect: main.py never passed auto_generate, so the backend half
# was dead code and generation was unconditional. The upload must now be asked
# for WITHOUT generation, so the choice can be offered after it lands.
c1, said1, _, _ = run_upload(make_rec("s1"), "auto")
upload_calls = [c for c in c1.calls if c[0] == "upload"]
check("1 upload is requested with auto_generate=False (choice comes after)",
      upload_calls == [("upload", False)],
      f"upload calls={upload_calls!r}")

# ---------------------------------------------------------------- CHECK 2
# "Gerar automaticamente" must actually generate. Splitting the call out of
# upload_and_generate() is only correct if something still makes it.
check("2 choosing 'auto' issues the generate call for the uploaded file",
      ("generate", "file_ABC") in c1.calls,
      f"calls={c1.calls!r}")

# ---------------------------------------------------------------- CHECK 3
# "Gerar personalizada" hands off to Plaud Web (media/image45.png) and must
# NOT fire the automatic summary -- that is the one thing the user declined.
c3, said3, opened3, ended3 = run_upload(make_rec("s3"), "custom")
gen3 = [c for c in c3.calls if c[0] == "generate"]
check("3 choosing 'custom' opens Plaud Web for that note and skips generate",
      not gen3 and len(opened3) == 1
      and "file_ABC" in opened3[0] and "ws_TEST123" in opened3[0],
      f"generate_calls={gen3!r} opened={opened3!r}")

# ---------------------------------------------------------------- CHECK 4
# Principle II, the dismissal path. The audio is already on the server when
# the dialog appears, so closing it must be a complete outcome: the session
# ends, the user is told the recording is safe, and nothing claims a failure.
c4, said4, opened4, ended4 = run_upload(make_rec("s4"), None)
msg4 = " ".join(said4).lower()
check("4 dismissing the dialog still ends the session and still notifies",
      bool(ended4) and bool(said4) and "enviado" in msg4 and "falha" not in msg4,
      f"ended={bool(ended4)} said={said4!r}")

# ---------------------------------------------------------------- CHECK 5
# The other half of the dismissal contract: a declined dialog must not become
# a silent generate. If the code fell back to generating anyway the choice
# would be cosmetic.
check("5 dismissing generates nothing (the choice is real, not cosmetic)",
      not [c for c in c4.calls if c[0] == "generate"],
      f"calls={c4.calls!r}")

# ---------------------------------------------------------------- CHECK 6
# Screenshots and notes are attached after the choice. A dismissal must not
# skip them -- they are part of the recording the user already made.
c6, said6, _, ended6 = run_upload(
    make_rec("s6", shots=[{"path": "/tmp/x.png", "t": 1}]), None)
check("6 dismissing still attaches the screenshots taken during the session",
      ("attach", "file_ABC") in c6.calls,
      f"calls={c6.calls!r}")

# ---------------------------------------------------------------- CHECK 7
# A failing generate is NOT a failing upload. The audio is on the server, so
# the message must not tell the user the upload failed and leave them
# re-uploading a recording that is already there.
c7, said7, _, ended7 = run_upload(
    make_rec("s7"), "auto", generate_raises=RuntimeError("boom"))
msg7 = " ".join(said7).lower()
check("7 a failed generate reports 'enviado' + generation error, not upload failure",
      bool(ended7) and "enviado" in msg7 and "falha no upload" not in msg7,
      f"said={said7!r}")

# ---------------------------------------------------------------- CHECK 8
# The GTK hop itself. _ask_generation_mode_sync() runs on the worker and waits
# on the main loop; with no loop running it must TIME OUT and answer None
# rather than block the worker forever, which would strand the recording with
# no notification and no `done`. Real function, tiny timeout, no loop.
t0 = threading.Event()
box = {}
def probe():
    box["v"] = main_mod._ask_generation_mode_sync(timeout=0.4)
    t0.set()
th = threading.Thread(target=probe, daemon=True)
th.start()
finished = t0.wait(10)
check("8 with no GTK loop running, the ask times out and answers None",
      finished and box.get("v", "UNSET") is None,
      f"finished={finished} value={box.get('v', 'UNSET')!r}")

# ---------------------------------------------------------------- CHECK 9
# A dialog that raises must answer like a dismissal, not propagate into the
# worker. Drives the real _ask_generation_mode_sync with a real GTK loop and
# an ask_generation_mode that blows up.
#
# The timing is the check, not the return value. Both a caught exception and an
# uncaught one end up returning None -- the uncaught one only because the wait
# times out -- so asserting "is None" alone passes on the broken code, which is
# a tautology (verified: removing the try/finally in the idle callback left this
# check green until it was rewritten to measure elapsed time). A generous
# timeout with a tight deadline separates them: the fix answers immediately,
# the bug can only answer by waiting the whole timeout out.
import time as _time
orig_ask = main_mod.ask_generation_mode
def _raiser():
    raise RuntimeError("no display")
main_mod.ask_generation_mode = _raiser
box9 = {}
ev9 = threading.Event()
def probe9():
    t = _time.monotonic()
    try:
        box9["v"] = main_mod._ask_generation_mode_sync(timeout=30)
    except Exception as e:
        box9["exc"] = e
    box9["elapsed"] = _time.monotonic() - t
    ev9.set()
    GLib.idle_add(Gtk.main_quit)
threading.Thread(target=probe9, daemon=True).start()
GLib.timeout_add_seconds(40, Gtk.main_quit)
Gtk.main()
main_mod.ask_generation_mode = orig_ask
check("9 a dialog that raises answers None at once, without waiting out the timeout",
      ev9.is_set() and "exc" not in box9 and box9.get("v", "UNSET") is None
      and box9.get("elapsed", 999) < 5,
      f"box={box9!r}")

# ---------------------------------------------------------------- CHECK 10
# The backend half must still exist and still be reachable: plaud_api.generate
# is what upload_and_generate(auto_generate=True) now delegates to, and the
# standalone method is what main.py calls. Both must be real.
check("10 plaud_api exposes a standalone generate() for the post-upload choice",
      callable(getattr(plaud_api.PlaudClient, "generate", None)),
      f"generate={getattr(plaud_api.PlaudClient, 'generate', None)!r}")

# ---------------------------------------------------------------- CHECK 11
# The dialog itself, built by the REAL ask_generation_mode(). Never call the
# blocking run() -- it waits on a human -- and never synthesize a key event
# either: GTK3's own injection (Gtk.test_widget_send_key,
# Gtk.Window.activate_default) is non-functional in this Wayland/Hyprland
# session, because Wayland compositors do not allow synthetic input into
# arbitrary windows (CLAUDE.md: xdotool is gone for the same reason). So this
# drives the response mechanism GTK's own C code uses once a key reaches it.
#
# (This reasoning was previously cross-referenced to test_modedialog.py, which
# CAN-315 deleted along with the mode dialog it tested. Restated here rather
# than pointed at, so it survives the next deletion too.)
captured = {}
real_run, real_destroy = Gtk.Dialog.run, Gtk.Dialog.destroy

def fake_run_factory(resp):
    def fake_run(self):
        captured["dlg"] = self
        self.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration()
        return resp
    return fake_run

def fake_destroy(self):
    captured["destroyed"] = True

def ask_with(resp):
    Gtk.Dialog.run = fake_run_factory(resp)
    Gtk.Dialog.destroy = fake_destroy
    try:
        return main_mod.ask_generation_mode()
    finally:
        Gtk.Dialog.run, Gtk.Dialog.destroy = real_run, real_destroy

a = ask_with(Gtk.ResponseType.YES)
b = ask_with(Gtk.ResponseType.NO)
d = ask_with(Gtk.ResponseType.DELETE_EVENT)
check("11 the dialog maps YES->auto, NO->custom, close->None",
      (a, b, d) == ("auto", "custom", None),
      f"yes={a!r} no={b!r} delete={d!r}")

# ---------------------------------------------------------------- CHECK 12
# Both buttons must exist and carry the official client's wording
# (media/image44.png), and automatic must be the default response -- it is the
# filled primary button there, and the user's stated habit.
dlg = captured.get("dlg")
btn_auto = dlg.get_widget_for_response(Gtk.ResponseType.YES)
btn_custom = dlg.get_widget_for_response(Gtk.ResponseType.NO)
labels = (btn_auto.get_label() if btn_auto else "",
          btn_custom.get_label() if btn_custom else "")
check("12 both official buttons exist, automatic is the default",
      btn_auto is not None and btn_custom is not None
      and "automaticamente" in labels[0].lower()
      and "personalizada" in labels[1].lower()
      and dlg.get_default_widget() is btn_auto,
      f"labels={labels!r} default={dlg.get_default_widget()!r}")
real_destroy(dlg)

# ---------------------------------------------------------------- CHECK 13
# The custom URL must carry transcribeDialog=custom. Without it the web app
# just shows the note and never opens "Selecionar método de geração", so the
# button silently does something other than what it says. Verified against the
# official client's openWebPage() (app.asar main/appService--_Y4noyZ.js), which
# builds from=desktop + desktop_uuid + workspace_id + the caller's params.
u = main_mod._web_note_url("file_ABC", "ws_TEST123", "enc_UUID")
check("13 the custom URL opens the web generation dialog, not just the note",
      "transcribeDialog=custom" in u and "/file/file_ABC?" in u
      and "workspace_id=ws_TEST123" in u and "from=desktop" in u
      and "desktop_uuid=enc_UUID" in u,
      f"url={u!r}")

# ---------------------------------------------------------------- CHECK 14
# The custom branch builds a URL and shells out, and both can raise. The audio
# is already on the server by then, so a failure there must report "enviado"
# plus what went wrong -- never fall through to the outer handler, which says
# "falha no upload" and sends the user to re-upload a recording that is
# already there. Same contract CHECK 7 asserts for the auto branch.
class BoomDevice(FakeClient):
    """A client whose device id cannot be read, so building the URL raises."""
    def __init__(self):
        super().__init__()
        del self.device   # attribute gone -> attribute lookup raises below

    def __getattr__(self, name):
        if name == "device":
            raise RuntimeError("no device id")
        raise AttributeError(name)

client14 = BoomDevice()
said14, ended14 = [], []
_o = (main_mod.plaud_api.PlaudClient, main_mod.notify, main_mod.peak_dbfs,
      main_mod._open_url, main_mod._ask_generation_mode_sync,
      main_mod._record_outcome, main_mod._open_generation)
main_mod.plaud_api.PlaudClient = lambda *a, **k: client14
main_mod.notify = lambda t, b, **k: said14.append(b)
main_mod.peak_dbfs = lambda p: -20.0
main_mod._open_url = lambda url: None
main_mod._open_generation = lambda url, *a, **k: main_mod._open_url(url)
main_mod._ask_generation_mode_sync = lambda *a, **k: "custom"
main_mod._record_outcome = lambda r, **k: None
try:
    main_mod._do_upload(make_rec("s14"), lambda: ended14.append(True))
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=30)
finally:
    (main_mod.plaud_api.PlaudClient, main_mod.notify, main_mod.peak_dbfs,
     main_mod._open_url, main_mod._ask_generation_mode_sync,
     main_mod._record_outcome, main_mod._open_generation) = _o
for _ in range(200):
    if not Gtk.events_pending():
        break
    Gtk.main_iteration_do(False)
msg14 = " ".join(said14).lower()
check("14 a custom hand-off that raises still reports 'enviado', not upload failure",
      bool(ended14) and "enviado" in msg14 and "falha no upload" not in msg14,
      f"said={said14!r} ended={bool(ended14)}")

# ---------------------------------------------------------------- CHECK 15
# The orphaned dialog. When the worker's wait times out it answers None and the
# session ends -- but the dialog it asked for is drawn on the GTK thread and
# nothing on the worker can reach it. Left alone it stays on screen after the
# session is over, and a user who comes back and clicks "Gerar automaticamente"
# gets a button that closes the window and generates nothing, having just been
# told a choice was recorded. The timeout must therefore RETRACT the dialog.
#
# This is the composition the mocked checks above cannot see: the real
# _ask_generation_mode_sync, the real ask_generation_mode, and a real GTK loop.
# CHECKS 11-12 build real dialogs with a stubbed destroy() so their widgets
# stay readable, so the toplevel list is already dirty here. Snapshot it first
# and count only what THIS check adds, or the leftovers read as an orphan.
_before15 = set(Gtk.Window.list_toplevels())
box15 = {}
ev15 = threading.Event()

def probe15():
    box15["v"] = main_mod._ask_generation_mode_sync(timeout=1)
    ev15.set()

threading.Thread(target=probe15, daemon=True).start()
# Run a real loop long enough for the ask to be dispatched, the timeout to
# fire, and the retraction to be serviced.
GLib.timeout_add_seconds(6, Gtk.main_quit)
Gtk.main()
alive15 = [w for w in Gtk.Window.list_toplevels()
           if w not in _before15 and w.get_visible()
           and (w.get_title() or "").startswith("Pronto para gerar")]
for w in alive15:
    w.destroy()
check("15 a timed-out ask retracts its dialog instead of orphaning it on screen",
      ev15.is_set() and box15.get("v", "UNSET") is None and not alive15,
      f"answer={box15.get('v', 'UNSET')!r} still_on_screen={len(alive15)}")

# ---------------------------------------------------------------- CHECK 16
# The other half of the retraction, and a different mechanism from CHECK 15:
# there, a dialog was already drawn and had to be closed. Here the loop never
# ran at all, so each timed-out ask leaves a PENDING idle source behind. If
# those are not neutralised they all fire the moment a loop next runs, popping
# one dialog per abandoned session in front of a user who asked for none.
# Measured before the fix: three timed-out asks produced three orphan dialogs.
_before16 = set(Gtk.Window.list_toplevels())
answers16 = [main_mod._ask_generation_mode_sync(timeout=0.3) for _ in range(3)]
GLib.timeout_add_seconds(4, Gtk.main_quit)
Gtk.main()
alive16 = [w for w in Gtk.Window.list_toplevels()
           if w not in _before16 and w.get_visible()
           and (w.get_title() or "").startswith("Pronto para gerar")]
for w in alive16:
    w.destroy()
check("16 asks abandoned with no loop running pop no dialogs when one starts",
      answers16 == [None, None, None] and not alive16,
      f"answers={answers16!r} orphans={len(alive16)}")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
