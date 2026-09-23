#!/usr/bin/env python3
"""
plaud-linux :: main entry point

Flow (mirrors Plaud Desktop):
  1. if not logged in -> prompt to log in (opens browser once)
  2. pick audio sources (system and/or mic, independently)   [user chose "decide at record time"]
  3. floating overlay records quietly; expand for pause / screenshot / stop
  4. on stop -> upload to Plaud cloud + trigger auto-generation (login-replicated)
  5. notify when the note is generating

Usage:
  main.py            -> start a recording session
  main.py --login    -> begin login
  main.py --status   -> print login status
"""
import subprocess
import sys
import threading
from urllib.parse import urlencode

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gio  # noqa: E402

try:
    from . import generation_web  # noqa: E402
    from . import library  # noqa: E402
    from . import login as login_mod  # noqa: E402
    from . import paths  # noqa: E402
    from . import plaud_api  # noqa: E402
except ImportError:
    import generation_web  # noqa: E402
    import library  # noqa: E402
    import login as login_mod  # noqa: E402
    import paths  # noqa: E402
    import plaud_api  # noqa: E402


def _bus_notify(title, body, replaces_id):
    """Post a notification and return its id, or 0 if the bus is unavailable.

    Straight to org.freedesktop.Notifications instead of notify-send, because
    only the bus hands back a usable id. `notify-send -p` prints "1" for every
    call here -- libnotify 0.8.3 falls back to Portal notifications in confined
    mode and the Portal has no ids -- so using its output as a replaces_id
    would make every notification overwrite every other one, warnings included.
    Measured against gnome-shell 46.0: replaces_id=0 mints 39 then 40, while
    reusing 39 updates that popup in place.

    Gio is already in the process (PyGObject, imported for Gtk), so this adds
    no dependency.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)",
                         ("Plaud Linux", replaces_id, "", title, body, [], {}, 8000)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 3000, None)
        return r.unpack()[0]
    except Exception:
        # No bus, or a daemon that refuses the call: fall back to the CLI. The
        # id is lost, so progress stops collapsing and simply behaves as before
        # -- noisier, never silent.
        try:
            # stderr to DEVNULL: libnotify prints three "confined mode / Portal
            # Notifications" warnings per call, and this inherits the launcher's
            # redirect, so they land in app.log. Measured on a real session:
            # 674 of 1040 bytes were that noise -- 65% of the log someone would
            # be reading to debug something else.
            subprocess.Popen(["notify-send", "-a", "Plaud Linux", title, body],
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return 0


def notify(title, body):
    """A notification the user must see on its own. Never replaces anything."""
    _bus_notify(title, body, 0)


# The id of the live progress notification, 0 when none is on screen. One
# upload emits 12 messages for a 2 h recording -- 5 fixed plus one per 5 MB
# part, and 2 h at the 36.2 kbps this app really produces is 31 MB, so 7 parts
# -- which as separate popups is 12 things to dismiss. Reset per session so a
# stale id cannot replace a notification the user already dismissed.
_progress_id = 0
_desktop_progress = None
_desktop_stopped = None
_desktop_ready = None


def progress(body):
    """A step in a running operation. Updates one notification in place.

    Deliberately not used for warnings or outcomes: those go through notify()
    and keep their own popup. Collapsing them is what this fix must not do --
    the silence and segment-loss warnings are the messages worth protecting.
    """
    global _progress_id
    if _desktop_progress is not None:
        GLib.idle_add(_desktop_progress, body)
    else:
        _progress_id = _bus_notify("Plaud Linux", body, _progress_id)


# A digitally silent Opus is a perfectly ordinary file: rc=0, and 12 s of it
# measures 5 880 bytes -- three times over the `st_size` gate below. Size and
# exit code have never been able to see this failure, and three separate bugs
# have shipped -91 dB recordings past them (CAN-316). Only the samples can.
SILENCE_DBFS = -85.0


def peak_dbfs(path):
    """Peak level of a recording in dBFS, or None if it could not be measured.

    `max_volume`, not `mean_volume`: a real recording that is mostly quiet
    averages low without being silent -- measured, a 60 s file with 1.5 s of
    speech in it reads mean -43.0 dB but max -23.6 dB. The peak answers the
    question actually being asked, which is whether any sample ever rose above
    the noise floor.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, timeout=1800,
        )
    except Exception:
        # Unmeasurable is not silent: returning None warns about nothing, which
        # is the right way to be wrong. Decoding runs ~200x real time (34.7 s
        # for 2 h), so the timeout only bites past a day of audio.
        return None
    # volumedetect prints a first, empty summary before decoding; the real
    # figure is the last one. Taking the first reports every file as silent.
    peak = None
    for line in (r.stderr or b"").decode("utf-8", "replace").splitlines():
        if "max_volume:" in line:
            try:
                peak = float(line.split("max_volume:")[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
    return peak


def ensure_login_or_prompt():
    c = plaud_api.PlaudClient()
    if c.is_logged_in():
        return True
    login_mod.begin_login()
    return False


def pick_sources():
    """The sources a new session opens with. Returns (system, mic).

    No dialog. The official client has no pre-recording mode picker at all --
    clicking record opens the recording window already capturing, with the two
    channels as live toggles inside it. A modal that has to be answered before
    any audio exists is the one thing the real app never does, and it costs the
    first seconds of whatever the user was trying to catch.

    So this is now just the opening state of those toggles, and the choice
    itself moved into the overlay, where it can be changed while recording
    (Overlay._on_src_toggled -> Recorder.set_sources).

    Still a function, and still returning the pair, because it is the one place
    the default lives -- and because it is the seam the tests and the settings
    window can substitute. It can no longer return None: nothing is being asked,
    so there is nothing to cancel. start_session() still handles None, since
    stubs and a future settings window can still produce one.

    BOTH ON, matching the official client exactly.

    This reverses an earlier deliberate divergence, and the reversal was the
    user's own call on 2026-09-06, made with the consequence spelled out and
    confirmed twice -- so do not quietly flip it back.

    What the official client does, PROVEN in the bundle:
    recordingService-Mile2cgU.js:297 opens the native recorder with a literal
    `enableMicrophone: true`, and micDeviceStateAtom (states-eNBxWOSQ.js:473)
    defaults to selectedOptionValue "smart" -- i18n smart_mic_option,
    "Automatic" -- with "Off" (not_record_mic) merely one option in a device
    selector. Its onboarding copy names the two channels "Use microphone to
    record my voice" and "Use speaker to record others' voice".

    THE CONSEQUENCE, which is real and was explained before the choice:
    muting yourself in Meet or Zoom does NOT stop this. The meeting app and
    the recorder open separate OS-level captures of the same microphone, and
    the meeting app cannot reach the recorder's. So a call where you believe
    a mute is protecting you is still recording your voice, and so is a
    lecture you are only listening to on headphones.

    Why the user chose it anyway, and why it is right for him: a meeting
    recording that captures only the other people is useless, and needing to
    remember a toggle before every call is exactly the friction that made him
    ask for "identical to Windows". The mic button in the overlay turns it off
    in one click for the lecture case, with no interface change from the
    official layout -- which is where the official client puts that control
    too (OFFICIAL-UI-SPEC.md §0.4).
    """
    try:
        from . import settings
    except ImportError:
        import settings
    return (settings.get_system_audio_enabled(), settings.get_mic_device() != settings.MIC_OFF)


def _web_note_url(file_id, ws_id, device_uuid):
    """URL of one recording on Plaud Web, with the custom-generation dialog open.

    "Gerar personalizada" delegates to the web rather than reimplementing the
    model / language / speaker-labelling picker locally, because that is what
    the official client does (docs/plaud-desktop/README.md §6; media/image45.png
    shows web.plaud.ai with "Selecionar método de geração" open on the note).

    The query string follows its openWebPage() (app.asar
    out-global-online/main/appService--_Y4noyZ.js): from=desktop, the encrypted
    desktop_uuid, workspace_id, plus the caller's params -- and the custom
    button adds transcribeDialog=custom, which is what makes the web app pop
    that panel instead of just showing the note. Dropping it lands the user on
    the file with no dialog, which is not the flow the button promises.
    """
    q = [("from", "desktop"), ("desktop_uuid", device_uuid),
         ("workspace_id", ws_id), ("transcribeDialog", "custom")]
    return f"{login_mod.WEB_HOST}/file/{file_id}?{urlencode(q)}"


def ask_generation_mode(on_dialog=None):
    """After upload: automatic generation, or the custom flow on Plaud Web?

    Returns "auto", "custom", or None when the user dismissed the dialog.
    `on_dialog`, when given, is handed the live Gtk.Dialog before it is run, so
    the caller can close it from elsewhere -- see _ask_generation_mode_sync,
    which uses it to retract a dialog the worker has already given up on.
    Mirrors media/image44.png ("Pronto para gerar" / "Escolha como pretende
    gerar esta nota"), which the official client shows once the upload is done.

    None is a first-class answer, not an error: the audio is already on the
    server by the time this runs, so a dismissed dialog costs the summary and
    nothing else. The caller must treat it that way -- never as a reason to
    skip a notification or to leave the session unfinished.

    Runs on the GTK main thread only. The upload worker reaches it through
    GLib.idle_add, like every other hop back into GTK in this file.
    """
    try:
        from . import desktop_ui
    except ImportError:
        import desktop_ui
    dlg = desktop_ui.generation_dialog()
    dlg.show_all()
    if on_dialog:
        on_dialog(dlg)
    resp = dlg.run()
    dlg.destroy()
    if resp == Gtk.ResponseType.YES:
        return "auto"
    if resp == Gtk.ResponseType.NO:
        return "custom"
    return None


def _open_url(url):
    """Open a URL in the user's browser, best-effort.

    Same mechanism as login.begin_login() -- xdg-open in a child process, so a
    missing or slow browser cannot block the caller. Best-effort because this
    runs on the upload worker after the audio is already safe: a browser that
    will not start must cost the convenience, never the recording.
    """
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _open_generation(url, client=None, file_id=None):
    """Show the official generation picker in its own window, like Windows.

    generation_web runs the selector in a separate WebKit process that reuses
    the app session and hides the rest of the note page, so the user sees the
    picker without browsing their account in a tab. Best-effort, same as
    _open_url: if the child cannot be started the browser tab is the fallback.
    The audio is already on the server when this runs.

    The child's exit status says whether the official page issued its
    generation request (0) or the picker closed without one (4). With a
    `client` and `file_id`, a request starts the same monitor and completion
    notice the automatic path uses (_generation_requested), so a custom
    generation ends with "Sua nota está pronta" like Windows 1.3.7 does.
    The wait for that status happens on its own daemon thread: the upload
    worker returns as soon as the picker is known to be up, so the next
    recording can start while the picker is still open.
    """
    try:
        child = generation_web.launch(url)
        # Still on the upload worker, never on GTK. A child that dies at
        # once (no WebKit and no browser handler, GTK init failure) exits
        # within this window; 0, 1 and 4 are its own outcomes, anything else
        # means nothing opened.
        try:
            code = child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            threading.Thread(target=_await_generation_child,
                             args=(child, client, file_id), daemon=True).start()
            return
        if code == 0:
            _custom_generation_requested(client, file_id)
        elif code not in (1, 4):
            _open_url(url)
    except Exception:
        _open_url(url)


def _await_generation_child(child, client, file_id):
    """Wait for the picker process on a worker; monitor on a request (exit 0).

    Never the GTK thread: the child lives as long as the user keeps the
    picker open, and it has its own bounded timers (60 s load, 6/20 s after
    the dialogs close, the session-expiry stop), so this wait always ends.
    Any status but 0 means no request was observed: no monitor, no notice.
    """
    try:
        code = child.wait()
    except Exception:
        return
    if code == 0:
        _custom_generation_requested(client, file_id)


def _custom_generation_requested(client, file_id):
    """The picker saw the page's generation request: monitor it like auto.

    Called from a worker; the monitor is started from the GTK thread through
    GLib.idle_add, the one legal way back, so the notice path is exactly the
    automatic one (_generation_requested -> wait_for_generation ->
    _generation_complete). `{"status": 0}` is the pending answer the
    automatic path polls on (PLAUD_API_NOTES §Upload step 5): the request
    itself was the page's, so nothing is generated or re-requested here,
    only read back at the same 3 s cadence until status 1.
    Without a client or file id there is nothing to monitor.
    """
    if client is None or not file_id:
        return

    def start():
        try:
            _generation_requested(client, file_id, {"status": 0})
        except Exception as exc:
            notify("Plaud Linux", f"Áudio enviado. {exc}")
        return False

    GLib.idle_add(start)


def _generation_requested(client, file_id, result):
    """Monitor generation independently so another recording can begin."""
    if isinstance(result, dict) and result.get("status") not in (0, 1):
        raise RuntimeError(result.get("msg") or "O Plaud não aceitou a geração.")
    if _desktop_progress is None:
        return

    def monitor():
        try:
            client.wait_for_generation(file_id, result)
            GLib.idle_add(_generation_complete, file_id)
        except Exception as exc:
            notify("Plaud Linux", f"Áudio enviado. {exc}")

    threading.Thread(target=monitor, daemon=True).start()


def _generation_complete(file_id):
    try:
        from . import desktop_ui, settings
    except ImportError:
        import desktop_ui, settings
    if settings.get_option("notify_complete"):
        desktop_ui.completed(lambda: _open_url(f"{login_mod.WEB_HOST}/file/{file_id}"))
    return False


def _ask_generation_mode_sync(timeout=300):
    """Ask ask_generation_mode() on the GTK thread from the upload worker.

    Returns "auto", "custom" or None. **Never raises, and always returns.**
    That is the whole point of this function: it sits between a finished upload
    and the code that notifies the user and ends the session, so anything that
    could leave it waiting forever would strand a recording the user was never
    told about -- which is worse than never asking (Principle II).

    Three things could do that, and each is closed here:
      * the dialog raising -> the idle callback's `finally` sets the event;
      * no GTK loop running to service idle_add at all -> the timeout;
      * a user who walks away -> the same timeout, answering None, which the
        caller already treats as a complete outcome.

    On that last path the dialog is still on screen, and leaving it there is
    its own bug: the session has ended, so a user who comes back and clicks
    "Gerar automaticamente" gets a button that destroys the dialog and
    generates nothing, having been told a choice was recorded. So the timeout
    RETRACTS the dialog -- GLib.idle_add(dlg.destroy), because the dialog
    belongs to the GTK thread -- which makes dlg.run() return DELETE_EVENT and
    both threads agree on None. The same idle_add also cancels a pending `ask`
    that has not run yet, so a timed-out session cannot pop a dialog later.

    We DIVERGE from the official client on the number, deliberately. It
    auto-closes this popup after 15 s of un-hovered time
    (out-global-online/main/index-BnqLG8Ib.js, AUTO_CLOSE_DELAY = 15e3) and
    leaves the note ungenerated, never retried. Ours is 300 s because the two
    timers are not the same thing: theirs is a UX auto-dismiss on a popup whose
    upload already finished in another process, ours is the backstop on a
    BLOCKING WAIT held by the daemon worker that still has to notify the user
    and end the session. Copying 15 s here would answer None for anyone who
    looked away, on a dialog we do not draw a countdown on. The outcome on
    expiry is identical to theirs -- ungenerated, and the user is told.
    """
    done_ev = threading.Event()
    # Both fields are written on the GTK thread and read on the worker, ordered
    # by done_ev / the giving_up flag rather than a lock -- the same argument
    # tray._busy makes. `dlg` is the live dialog while one is on screen.
    box = {"choice": None, "dlg": None, "giving_up": False}

    def ask():
        if box["giving_up"]:
            # The worker already answered None for this session. Drawing the
            # dialog now would put a live window in front of a user whose
            # session is over and whose click could no longer be honoured.
            return False
        try:
            if _desktop_ready is not None:
                _desktop_ready()
            box["choice"] = ask_generation_mode(
                on_dialog=lambda d: box.__setitem__("dlg", d))
        except Exception:
            # A dialog that cannot be drawn answers None, like a dismissal.
            box["choice"] = None
        finally:
            box["dlg"] = None
            done_ev.set()
        return False  # one-shot

    GLib.idle_add(ask)
    if not done_ev.wait(timeout):
        # Give up, and take the dialog with us. Set the flag first so an `ask`
        # that has not run yet becomes a no-op; then retract one that is
        # already on screen, from the GTK thread, which ends its nested loop
        # with DELETE_EVENT -> "auto"/"custom" is never returned to nobody.
        box["giving_up"] = True
        dlg = box["dlg"]
        if dlg is not None:
            GLib.idle_add(dlg.destroy)
        return None
    return box["choice"]


def _record_outcome(rec, **fields):
    """Persist the upload outcome to the sidecar, best-effort.

    Best-effort on purpose: this runs on the worker thread beside the paths
    that notify the user, and a sidecar write that raises must never be what
    loses the notification or the `done` in the enclosing finally. The .opus
    is untouched either way -- this only ever writes the .json.
    """
    try:
        rec.set_upload(**fields)
    except Exception:
        pass


def on_discard(rec, done):
    """Called after the overlay discards a recording (CAN-315). No upload.

    overlay.Overlay.discard() has already deleted the .opus, the segment
    directory and the sidecar by the time this runs -- this function's only
    job is ending the session, the same contract on_stop() keeps. There is no
    worker thread here: unlike on_stop(), nothing left to do is I/O-bound, so
    running it straight on the GTK thread that called it (via GLib.idle_add,
    same as every other hop back into GTK in this file) does not risk blocking
    the loop the way a 2 h peak_dbfs() scan would.
    """
    done()


def on_stop(rec, done):
    """Called after overlay stops: upload + generate in a background thread.

    `done` means *this session is over* — not *this process should exit*. The
    tray passes something that resets its icon; a caller with no loop of its own
    passes Gtk.main_quit and exits when the upload finishes. Deciding that is
    the entry point's job, and this function no longer has an opinion.

    Ending the session is this function's contract, and it is kept even when the
    body fails. Everything below runs in a GTK callback with the overlay already
    destroyed, so an escaping exception is not a crash — GTK prints it and keeps
    spinning, leaving a process with no window that only dies by kill. The
    worker's `finally` cannot cover that: it does not exist until Thread.start()
    returns, and `RuntimeError: can't start new thread` is raised by CPython on
    exactly that line. So `done` is called here, by whichever path gets there
    first, and the flag makes "whichever" mean "only one".
    """
    # A new upload starts a new progress notification. Reusing the previous
    # session's id would either overwrite a popup the user already dismissed or
    # silently go nowhere, depending on the daemon.
    global _progress_id
    _progress_id = 0

    ended = []

    def end_session():
        # No lock: every caller is on the GTK main thread. The early returns run
        # there directly, and the worker reaches this only through
        # GLib.idle_add, which is what marshals it back.
        #
        # Marked as ended only once done() has actually returned. Appending
        # first meant a done() that raised still counted as ended, so no later
        # caller would retry it and the loop kept spinning -- the flag recorded
        # the intent rather than the outcome.
        if not ended:
            done()
            ended.append(True)

    try:
        try:
            from . import settings
        except ImportError:
            import settings
        if not settings.get_option("cloud_sync"):
            end_session()
            notify("Plaud Linux", "Gravação salva neste computador. Envie quando quiser em Envios recentes.")
            return
        _do_upload(rec, end_session)
    except BaseException as e:
        # BaseException, not Exception: KeyboardInterrupt is the case this
        # handler was written for -- the app spawns ffmpeg and the user may
        # Ctrl-C the launcher -- and it is not an Exception, so the narrower
        # catch missed exactly what it cited.
        #
        # End the session FIRST. Notifying first made the whole guarantee
        # contingent on the noisiest part of the path: _bus_notify itself
        # catches only Exception, and the f-string calls str(e), which an
        # exception with a hostile __str__ raises from. Either one skipped
        # end_session() and hung the loop windowless -- reopening exactly the
        # bug this function exists to close. Ending the session does not depend
        # on the message being delivered, so it must not be sequenced behind it.
        end_session()
        # Then say something: the recording is on disk and the user is owed its
        # location, not silence.
        notify("Plaud Linux", f"❌ Falha ao iniciar o envio: {e}\nArquivo mantido em Gravações.")


def _do_upload(rec, done):
    path = rec.final_path
    if getattr(rec, "concat_failed", False):
        # The audio still exists as segments — say so, and say where. Mention a
        # segment loss here too: _concat() can set both, and this early return
        # is the only message that path ever prints, so "áudio preservado"
        # alone would imply the segments are all still there.
        lost = getattr(rec, "segments_lost", 0)
        extra = f" ({lost} trecho(s) se perderam, o áudio está mais curto)" if lost else ""
        # done() before notify(), for the same reason as in on_stop: a notify()
        # that raises here would skip it and hang the loop windowless. This is
        # not a theoretical path -- concat failure is a real mode this file
        # actively handles.
        # Nothing this session recorded survived: _concat() raises the same flag
        # for that, because the refusal it has to produce is the same one. The
        # message must not be, though -- "falha ao juntar os trechos" points at
        # a segdir with nothing in it, and it would not tell the user the thing
        # that actually matters here: a file of that name already existed, and
        # it was NOT sent. Saying nothing would leave them assuming it was.
        # `getattr` for the same reason as `concat_failed` above: rec is a
        # Recorder in production but a double in the checks, and this path must
        # not become the one that needs a fuller one. No segments recorded at
        # all reads as "nothing survived", which is exactly this case.
        segs = getattr(rec, "segments", None)
        nothing = segs is not None and not any(
            s.exists() and s.stat().st_size > 0 for s in segs)
        if nothing:
            _record_outcome(rec, ok=False, error="no audio recorded this session")
            done()
            stale = " O arquivo anterior com esse nome foi mantido e NÃO foi enviado." \
                if path.exists() else ""
            notify("Plaud Linux",
                   f"Nada foi gravado nesta sessão — nada enviado.{stale}")
            return
        _record_outcome(rec, ok=False, error="concat failed", segments_lost=lost)
        done()
        notify("Plaud Linux",
               f"Falha ao juntar os trechos{extra}. Áudio preservado em {rec.segdir}")
        return
    if not path.exists() or path.stat().st_size < 2000:
        _record_outcome(rec, ok=False, error="recording too short or empty")
        done()
        notify("Plaud Linux", "Gravação muito curta ou vazia — nada enviado.")
        return

    def worker():
        try:
            try:
                from . import highlights
            except ImportError:
                import highlights
            highlights.finish_pending(rec)
            client = plaud_api.PlaudClient()
            if not client.is_logged_in():
                _record_outcome(rec, ok=False, error="not logged in")
                notify("Plaud Linux", "Não logado — arquivo salvo em Gravações. Faça login e reenvie.")
                return
            # Warn, never discard: the upload goes ahead either way. A recording
            # the app thinks is silent is still the user's only copy of it
            # (Principle II), and the measurement can be wrong -- being wrong
            # must cost a notification, never the audio.
            #
            # Measured here and not in on_stop() because on_stop() runs on the
            # GTK main thread: decoding a 2 h recording takes 34.7 s, and that
            # is 34.7 s with the main loop blocked, so the overlay stays painted
            # on screen ignoring input instead of disappearing on "Parar". And
            # after the login check, not before it, so a logged-out user is not
            # promised "enviando mesmo assim" and then told nothing was sent.
            peak = peak_dbfs(path)
            if peak is not None and peak <= SILENCE_DBFS:
                notify("Plaud Linux",
                       f"⚠️ A gravação parece muda ({peak:.1f} dB) — provável captura "
                       "da fonte errada. Enviando mesmo assim; o arquivo está em Gravações.")
            # A segment that produced no usable file is dropped by _concat() and
            # the upload is simply shorter than the session the user watched --
            # silently, which is the same invisible-failure class as the check
            # above. `getattr` because audio.py only grew the attribute recently.
            #
            # Says what is true right now and claims no outcome: the upload has
            # not run yet, so "o que sobrou foi enviado" would be a promise this
            # code cannot keep, and the failure path below would contradict it.
            lost = getattr(rec, "segments_lost", 0)
            if lost:
                notify("Plaud Linux",
                       f"⚠️ {lost} trecho(s) da gravação se perderam — o áudio está "
                       "mais curto que a sessão.")
            # auto_generate=False: the choice belongs to the user now, and it
            # is asked below once the upload has actually finished -- which is
            # when the official client asks it (README §6, media/image44.png).
            fid = client.upload_and_generate(
                path,
                progress=lambda m: progress(f"Enviando: {m}"),
                auto_generate=False,
            )
            # Written before the attachment step, not after: the audio is on
            # the server at this point, and a failure while attaching marks
            # must not leave the sidecar claiming the upload never happened.
            _record_outcome(rec, ok=True, file_id=fid)
            # attach screenshots and notes to the cloud recording (best-effort)
            # getattr, for the same reason _save_meta() uses it on this field
            # (audio.py): this path is reached with Recorders built without
            # __init__, and a hard reference turns a missing attribute into an
            # AttributeError raised AFTER the upload succeeded -- reporting
            # "falha no upload" for a recording already safe on the server,
            # which is exactly the false failure Principle II forbids.
            rec_flags = getattr(rec, "flags", None) or []
            if rec.screenshots or rec.notes or rec_flags:
                try:
                    n = client.attach_screenshots(
                        fid, rec.screenshots, notes=rec.notes, flags=rec_flags,
                        progress=lambda m: progress("Sincronizando destaques…"),
                    )
                except Exception as exc:
                    notify("Plaud Linux", f"Áudio enviado, mas os destaques não foram anexados: {exc}")

            # Ask on the GTK thread, wait here. Everything after this point --
            # the notification, the screenshot attachment, the `done` in the
            # enclosing finally -- must happen whatever the user answers, so
            # the wait is bounded by _ask_generation_mode_sync's own guarantee
            # that it always sets the event, on every path including a raising
            # dialog.
            choice = _ask_generation_mode_sync()
            if choice == "custom":
                # Hand off to Plaud Web and generate nothing here: the custom
                # picker lives there (media/image45.png), and firing the
                # automatic summary first would be the one thing the user just
                # declined.
                #
                # Guarded for the same reason the auto branch below is: the
                # audio is already on the server, so a failure building or
                # opening the URL -- encrypt_uuid on an unexpected device
                # value, say -- must not reach the outer handler and tell the
                # user "falha no upload" about a recording that uploaded fine.
                try:
                    url = _web_note_url(
                        fid, client.tokens.get("ws_id"),
                        plaud_api.encrypt_uuid(client.device))
                    _open_generation(url, client, fid)
                    notify("Plaud Linux",
                           "✅ Enviado! Abrindo as opções de geração.")
                except Exception as e:
                    notify("Plaud Linux",
                           f"✅ Enviado, mas não consegui abrir o Plaud Web: {e}\n"
                           "A gravação está em web.plaud.ai — gere por lá.")
            elif choice == "auto":
                try:
                    result = client.generate(fid, progress=lambda m: progress(f"Gerando: {m}"))
                    _generation_requested(client, fid, result)
                    notify("Plaud Linux", "✅ Enviado! Gerando nota.\nVeja em web.plaud.ai")
                except Exception as e:
                    # The audio is on the server; only the summary failed. Say
                    # so precisely rather than letting the outer handler claim
                    # "falha no upload" for a recording that uploaded fine.
                    notify("Plaud Linux",
                           f"✅ Enviado, mas a geração falhou: {e}\n"
                           "A gravação está em web.plaud.ai — gere por lá.")
            else:
                # Dismissed. The recording is safe and transcribing already
                # (PLAUD_API_NOTES §Upload step 5: confirm_upload starts the
                # transcription, transsumm only asks for the summary), so this
                # is a complete, non-failing outcome -- just an ungenerated one.
                notify("Plaud Linux",
                       "✅ Enviado! Nenhuma geração escolhida.\n"
                       "A gravação está em web.plaud.ai.")

        except Exception as e:
            _record_outcome(rec, ok=False, error=str(e))
            notify("Plaud Linux", f"❌ Falha no upload: {e}\nArquivo mantido em Gravações.")
        finally:
            # Still idle_add: this runs on the worker thread, and the only legal
            # way back to GTK is through the main loop. Only the payload changed
            # -- "the session ended", not "quit the process".
            GLib.idle_add(done)

    rec._flag_upload_owned = True
    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        rec._flag_upload_owned = False
        raise


def resend(entry, done=None):
    """Re-send a recording that is already on disk. CAN-315 item 5.

    Principle II keeps a failed upload's .opus in recordings/ and tells the
    user; nothing led back from there, so every failure became manual work the
    user did not know was waiting. This is the way back.

    `entry` is a library.Entry, NOT a Recorder, and that is deliberate rather
    than incidental: `Recorder.__init__` calls segdir.mkdir() and _save_meta()
    unconditionally (audio.py:390, 407), so building one for a session that
    already exists would overwrite its sidecar with a blank one and destroy the
    notes the user typed. Those notes live nowhere else. The Entry is a read of
    the sidecar, and the only write on this path is the outcome patch, which
    re-reads from disk first.

    Runs its own worker thread for the same reason _do_upload does -- the
    upload is seconds to minutes of network, and this is called from a GTK
    handler. `done` is optional here, unlike in _do_upload: no session is in
    progress, so there is no session to end and no icon to reset. Callers that
    want the row refreshed pass one; the tray does not.
    """
    path = entry.final_path

    def finish():
        if done is not None:
            GLib.idle_add(done)

    # Guarded before the thread, not inside it: these are cheap filesystem
    # facts, and answering them here means the user gets the refusal
    # immediately instead of after a thread start that does nothing.
    if entry.uploaded:
        # Not an error -- a no-op with a reason. Re-sending would create a
        # second note on the server for one recording, and the user would have
        # to work out which to delete.
        notify("Plaud Linux", "Essa gravação já foi enviada — nada a fazer.")
        finish()
        return False
    if not entry.has_audio:
        # Covers both "no .opus at all" and the loose-segment case: sending
        # segments would mean concatenating first, which writes into
        # recordings/. That is a repair, not a resend, and this function does
        # not do it silently.
        if entry.loose_segments:
            notify("Plaud Linux",
                   f"O áudio dessa sessão está em trechos separados, ainda não juntados.\n"
                   f"Os arquivos estão em {entry.segdir} — nada foi perdido.")
        else:
            notify("Plaud Linux", "Sem áudio para enviar nessa gravação.")
        finish()
        return False

    global _progress_id
    _progress_id = 0

    def worker():
        try:
            client = plaud_api.PlaudClient()
            if not client.is_logged_in():
                notify("Plaud Linux",
                       "Não logado — faça login e reenvie. O arquivo continua em Gravações.")
                return
            fid = client.upload_and_generate(
                path,
                progress=lambda m: progress(f"Reenviando: {m}"),
                auto_generate=False,
            )
            # Before the attach step and before the generation choice, exactly
            # as _do_upload orders it: the audio is on the server at this
            # point, and a later failure must not leave the sidecar still
            # claiming this was never sent -- which would offer the user a
            # resend that duplicates it.
            entry.mark_uploaded(fid)
            choice = _ask_generation_mode_sync()
            if choice == "custom":
                try:
                    url = _web_note_url(
                        fid, client.tokens.get("ws_id"),
                        plaud_api.encrypt_uuid(client.device))
                    _open_generation(url, client, fid)
                    notify("Plaud Linux", "✅ Reenviado! Abrindo as opções de geração.")
                except Exception as e:
                    notify("Plaud Linux",
                           f"✅ Reenviado, mas não consegui abrir o Plaud Web: {e}\n"
                           "A gravação está em web.plaud.ai — gere por lá.")
            elif choice == "auto":
                try:
                    result = client.generate(fid, progress=lambda m: progress(f"Gerando: {m}"))
                    _generation_requested(client, fid, result)
                    notify("Plaud Linux", "✅ Reenviado! Gerando nota.\nVeja em web.plaud.ai")
                except Exception as e:
                    notify("Plaud Linux",
                           f"✅ Reenviado, mas a geração falhou: {e}\n"
                           "A gravação está em web.plaud.ai — gere por lá.")
            else:
                notify("Plaud Linux",
                       "✅ Reenviado! Nenhuma geração escolhida.\n"
                       "A gravação está em web.plaud.ai.")
            # Marks and notes from the original session, replayed onto the new
            # cloud recording. Best-effort like the live path: this must never
            # be what turns a successful resend into a reported failure.
            if entry.screenshots or entry.notes or entry.flags:
                try:
                    n = client.attach_screenshots(
                        fid, entry.screenshots, notes=entry.notes, flags=entry.flags,
                        progress=lambda m: progress("Sincronizando destaques…"),
                    )
                except Exception as e:
                    notify("Plaud Linux",
                           f"✅ Reenviado, mas os destaques não foram anexados: {e}")
        except Exception as e:
            # The .opus is untouched -- only the .json is written -- so the
            # recording is exactly as resendable after this as it was before.
            entry.mark_failed(e)
            notify("Plaud Linux",
                   f"❌ Falha ao reenviar: {e}\nArquivo mantido em Gravações.")
        finally:
            finish()

    threading.Thread(target=worker, daemon=True).start()
    return True


def open_web_home():
    """Match the Desktop folder action without exchanging another login code."""
    client = plaud_api.PlaudClient()
    query = {"from": "desktop", "desktop_uuid": plaud_api.encrypt_uuid(client.device)}
    if client.tokens.get("ws_id"):
        query["workspace_id"] = client.tokens["ws_id"]
    _open_url(f"{login_mod.WEB_HOST}/?{urlencode(query)}")


def login_available():
    return plaud_api.PlaudClient().is_logged_in()


def wire_login_handler(app, on_complete=None):
    if app is None or getattr(app, "_plaud_login_handler", None) is not None:
        return
    app._plaud_login_handler = app.connect(
        "open", lambda a, files, n, hint: [
            login_mod.handle_url_async(f.get_uri(), on_complete) for f in files])


def open_library():
    """Show the "Envios recentes" list, wired to the resend path.

    The seam, exactly like on_stop: librarywin.py is GUI and knows nothing
    about the network, resend() is network and knows nothing about GTK, and
    main.py is the only place they meet (CLAUDE.md Layer rule). Imported here
    rather than at module scope for the same reason tray.py is -- keeping GTK
    window modules out of the import graph of `--status`, which builds no UI.
    """
    try:
        from . import librarywin
    except ImportError:
        import librarywin
    return librarywin.open_library(on_resend=resend)


def start_session(app=None, on_session_end=None, on_state_change=None):
    """Run one recording session. Returns the live overlay, or False.

    The return value is truthy exactly when a session began, which is all the
    callers test; it carries the overlay so a signal handler can stop the
    recorder it is waiting on.

    Does NOT run the GTK loop -- the tray owns it (CAN-310), and a session must
    not own the thing that outlives it.

    The return value is what lets a caller tell "a session started and its `done`
    will fire" from the two early returns below, which start nothing. On those
    paths `done` must NOT be called: no session began, so there is none to end.
    """
    wire_login_handler(app)
    if not ensure_login_or_prompt():
        # Nothing to wait for here: dlg.run() already ran its own nested loop,
        # and begin_login() only hands off to the browser -- the auth_code comes
        # back to a *separate* process through the plaud:// handler. A Gtk.main()
        # at this point has no window and nothing that could ever quit it, so it
        # spun forever as an invisible process that only died by kill.
        return False
    sources = pick_sources()
    # `is None`, not falsy: None is the cancel sentinel, and (False, False) is
    # also falsy. pick_sources() no longer produces either -- it asks nothing --
    # but both guards stay, because it is now explicitly the substitution seam
    # for tests and for a settings window that CAN cancel.
    if sources is None:
        return False
    system, mic = sources
    if not (system or mic):
        notify("Plaud Linux", "Ative o microfone ou o áudio do sistema em Preferências para gravar.")
        # "Neither" is the one combination with no ffmpeg command behind it, so
        # it can only produce a silent file. The overlay keeps the last-on
        # toggle insensitive and Recorder.set_sources() refuses the pair, so
        # this is the third guard on the same rule rather than the only one.
        return False
    # The engine already accepts all three legal combinations, so the pair maps
    # onto them here and nothing downstream has to learn a new vocabulary. Kept
    # in one place on purpose: when the toggles move into a settings window the
    # same pair arrives from there and this is the only join.
    mode = "meeting" if (system and mic) else ("system" if system else "mic")
    # Imported here, in the one branch that draws it, for the same reason tray
    # is (see the comment at the bottom of main()): overlay.py calls
    # require_version("GtkLayerShell", "0.1") at ITS module scope, and a missing
    # typelib raises ValueError -- not ImportError, so the dual-import shim
    # around it does NOT catch it. At module scope that made --status, --login
    # and the plaud:// callback -- three paths that draw no overlay at all --
    # die with a raw traceback on any machine without gtk-layer-shell (measured
    # by making require_version raise for that namespace only: all three exited
    # 1 with a traceback and no answer). The launcher sends stdout to app.log,
    # so a GUI launch showed the user nothing whatsoever.
    try:
        try:
            from . import overlay as overlay_mod
        except ImportError:
            import overlay as overlay_mod
    except ValueError:
        # Genuinely missing typelib on the one path that truly needs it. Say so
        # where the user can see it -- a notification, not a traceback into
        # app.log -- and decline the session rather than half-starting one.
        notify("Plaud Linux",
               "gtk-layer-shell não está instalado; a gravação não pode "
               "iniciar. Instale o pacote gtk-layer-shell.")
        print("gtk-layer-shell typelib missing: install the gtk-layer-shell "
              "system package", file=sys.stderr)
        return False
    # Only on the recording path, and only once the user has committed to a
    # session. Not at the top of main(): --status and the plaud:// callback run
    # as *separate processes while a recording is in flight* -- the browser
    # launches the callback handler mid-session -- so pruning there truncated
    # the live session's app.log out from under it, which is the one file
    # CLAUDE.md names as the only way to debug a GUI-launched run.
    paths.prune_old_logs()
    # The exit policy lives here, in one visible line, instead of being asserted
    # four times inside on_stop(): the caller decides what "the session ended"
    # means. The tray passes a callback that resets its icon; a caller that has
    # no loop of its own passes Gtk.main_quit and still exits when the upload
    # finishes. on_stop() is untouched either way.
    done = on_session_end if on_session_end is not None else Gtk.main_quit
    ov = overlay_mod.run(mode=mode, on_stop=lambda rec: (_desktop_stopped or on_stop)(rec, done),
                         on_state_change=on_state_change,
                         on_discard=lambda rec: on_discard(rec, done))
    # The mic can be asked for and not be there: with no default source,
    # _ffmpeg_cmd() drops to the system-only branch and records on, so a user
    # who chose "Microfone" gets a recording with none of their own voice. The
    # engine has no way to say so -- it is not a failure it can act on -- but by
    # the time run() returns it has already resolved its sources into _last_src,
    # so the fact is readable here. Warn only; the recording continues.
    if mic and not (getattr(ov.rec, "_last_src", None) or (None, None, None, None))[2]:
        notify("Plaud Linux",
               "⚠️ Microfone pedido, mas nenhum foi encontrado — gravando "
               "sem a sua voz.")
    # The overlay itself, not True. Still just "a session began" to every caller
    # that only tests it -- both of them do -- but a signal handler has to be
    # able to STOP the recording it is deferring for, and this is the handle
    # that lets it, without a second way of finding the live session.
    return ov


# The bus name that makes "is a session already running?" answerable. Must be a
# valid D-Bus/GApplication id -- `plaud-linux` is not (Gio rejects the hyphen and
# the missing dot), which is why this is not simply the command name.
APP_ID = "ai.plaud.LinuxRecorder"


def _claim_instance():
    """Register on the session bus. Returns (owner, remote).

    Gio.Application is used for the bus name and its `open` routing ONLY -- it is
    registered, never run(). `app.run()` would take over the loop and `app.quit()`
    would end the *process*, re-merging "session ended" with "process exits" --
    the exact conflation #32 spent its budget separating, and the thing that let
    CAN-310 move the loop without touching on_stop(). `Gtk.main()` now lives in
    tray.run_tray(), and it is still ours rather than Gio's.

    `owner` is the app when we hold the name, else None. `remote` is the app when
    somebody else holds it and we can talk to them, else None. Both None means the
    bus could not answer, so there is neither a session to defer to nor one to
    route to -- the caller proceeds standalone.

    Two values rather than one because the callers ask different questions, and
    the single-value version made the plaud:// path register a second time to
    re-ask what this call already knew. Measured against a blocked owner, that
    cost 50.1 s before the fallback instead of 25.0 s.
    """
    app = Gio.Application(application_id=APP_ID,
                          flags=Gio.ApplicationFlags.HANDLES_OPEN)
    try:
        app.register(None)
    except GLib.Error:
        # Two real failures land here, and both must degrade rather than break.
        # Measured on this machine: with the owner's loop blocked -- which
        # peak_dbfs() really does for 34.7 s on a 2 h file -- register() blocks
        # 25.0 s and then raises g-io-error-quark 24 (timed out). A bare
        # register() would therefore let a GLib.Error escape the plaud:// path
        # and lose a single-use auth_code. No session bus at all is the gentler
        # case (measured: returns immediately, is_remote False), but it costs
        # nothing to cover both here.
        return None, None
    return (None, app) if app.get_is_remote() else (app, None)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--status":
        # Deliberately before any bus work: --status is a read-only CLI probe
        # that must answer when nothing is running, and must never wait on
        # another process's main loop. Routing it would make a 25 s stall
        # (measured, above) the cost of asking a local question.
        c = plaud_api.PlaudClient()
        print("logged_in:", c.is_logged_in())
        return

    # Claimed lazily, and never by --login: the name must mean "a session is
    # running", and only a process with a main loop can answer on it. --login
    # holds no loop -- begin_login() hands off to the browser and returns -- so
    # claiming there left the name owned by a process that cannot reply. A
    # concurrent launch then got NoReply rather than remote, read it as "no
    # session", and started recording anyway (measured: probe saw
    # owner=False remote=False and would have started a session; with a real
    # loop running it correctly saw remote=True).
    owner = remote = None
    if arg != "--login":
        owner, remote = _claim_instance()

    if arg == "--login":
        # Already logged in? Opening the browser would burn a fresh auth_code
        # against a live session and fail with "authorization code invalid or
        # expired", which reads like the tokens are broken when they are fine.
        if plaud_api.PlaudClient().is_logged_in() and "--force" not in sys.argv:
            print("Já logado. Use --login --force para entrar com outra conta.")
        else:
            if "--force" in sys.argv:
                login_mod.begin_login(force=True)
            else:
                login_mod.begin_login()
    elif arg == "--shortcut":
        # Global-shortcut entry point (CAN-315): a Hyprland `bind` runs
        # `plaud-linux --shortcut <name>` and this process, which never has a
        # loop of its own for this call, forwards it to the resident instance
        # that does. See tray._install_actions for the four valid names
        # (record/stop/pause/mark) and why activate_action is safe to call
        # from here -- it runs on the OWNER's main thread, not this one.
        #
        # Deliberately does NOT fall back to starting a session standalone
        # when nothing is running: a hotkey firing at an app that is not open
        # should say so, not silently launch a new recording the user has no
        # tray to control afterwards. Matches the plain-launch branch below,
        # which already declines rather than doubling up when remote exists --
        # this is the mirror case, declining when remote does NOT exist.
        name = sys.argv[2] if len(sys.argv) > 2 else ""
        if name not in ("record", "stop", "pause", "mark", "shot"):
            print(f"plaud-linux --shortcut: ação desconhecida: {name!r} "
                  "(use record|stop|pause|mark|shot)", file=sys.stderr)
        elif remote is None:
            notify("Plaud Linux", "Plaud não está em execução.")
        else:
            remote.activate_action(name, None)
            remote.get_dbus_connection().flush_sync(None)
    elif arg.startswith("plaud://"):
        # Route to the live session if there is one, so the auth_code is consumed
        # by the process the user is actually recording in. That separate process
        # is what truncated the live session's app.log (see prune_old_logs).
        #
        # If we cannot reach it, handle the code HERE rather than dropping it.
        # The auth_code is single-use and the browser will not re-issue it, so a
        # redundant process is a nuisance while a lost code is a failed login the
        # user must redo. Losing the code is the worse of the two.
        routed = False
        if remote is not None:
            try:
                remote.open([Gio.File.new_for_uri(arg)], "")
                remote.get_dbus_connection().flush_sync(None)
                routed = True
            except GLib.Error:
                # The guard in _claim_instance() stopped one line short of the
                # call that actually transmits the code, so the reasoning above
                # did not cover the case it was written for: the owner dying
                # between register() returning a remote and open() landing.
                # Narrow, but the cost is the whole login, so fall through.
                pass
        if not routed:
            login_mod.handle_url(arg)
    elif remote is not None:
        # A live instance owns the name, so a tray is already resident. Starting
        # a second one would record the same audio twice and let two
        # prune_old_logs() runs fight over one app.log. Surfacing the running
        # tray instead of declining is a later issue; this only declines.
        if arg != "--background":
            remote.activate_action("show", None)
            remote.get_dbus_connection().flush_sync(None)
    else:
        # Imported in the one branch that needs it, not at module scope and not
        # at the top of main(): tray.py imports this module back (it calls
        # start_session and notify), so a top-level import would be a cycle, and
        # it pulls in AyatanaAppIndicator3 at ITS module scope. At the top of
        # main() that made --status -- a read-only CLI probe that draws nothing
        # -- die on any machine without the typelib. Measured, by making
        # require_version raise for that namespace only: ValueError, no answer.
        try:
            try:
                from . import tray
            except ImportError:
                import tray
        except ValueError:
            # A missing typelib on the one path that truly needs it. Say so
            # where the user can see it -- a notification, not a traceback into
            # app.log -- and decline to run the tray.
            #
            # tray.py no longer imports AyatanaAppIndicator3 (it publishes its
            # own StatusNotifierItem), so this no longer fires for that library.
            # It is kept, and its wording generalised, because the module still
            # imports Gtk through gi and the failure shape is identical: an
            # unusable tray must not take down --status or the plaud:// callback,
            # which run as separate processes while a recording is in flight.
            notify("Plaud Linux",
                   "Uma biblioteca gráfica necessária não está instalada; "
                   "a barra de tarefas não pode iniciar.")
            print("tray import failed on a missing typelib", file=sys.stderr)
            return
        # `owner` is held for the life of the process: dropping it would drop the
        # bus name with it and stop being single-instance halfway through. It is
        # None when the bus is unreachable, and run_tray handles that by simply
        # having no `open` to wire -- recording still works standalone.
        tray.run_tray(owner, show_on_launch=arg != "--background")


if __name__ == "__main__":
    main()
