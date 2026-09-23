"""Live mark preparation, with recording-owned jobs that survive panel closure."""
from concurrent.futures import Future
import threading

from gi.repository import GLib

try:
    from . import plaud_api, settings
except ImportError:
    import plaud_api
    import settings


def _apply(rec, flag, job):
    if not any(item is flag for item in rec.flags):
        return
    try:
        mark = job.result()
    except Exception:
        flag["preview_status"] = "Não foi possível analisar agora. Trecho salvo para o envio."
    else:
        flag["cloud_mark"] = mark
        if not flag.get("edited"):
            flag["text"] = mark["mark_content"]
        flag.pop("preview_status", None)
    try:
        rec._save_meta()
    except OSError:
        pass  # Keep the result in memory even if the sidecar cannot be written.


def start(rec, flag):
    if not settings.get_option("cloud_sync"):
        flag["preview_status"] = "Trecho salvo. Análise disponível ao enviar à nuvem."
        rec._save_meta()
        return
    jobs = getattr(rec, "_flag_jobs", None)
    if jobs is None:
        jobs = rec._flag_jobs = {}
    job = Future()
    jobs[id(flag)] = (flag, job)
    flag["preview_status"] = "Marcado. Analisando…"
    rec._save_meta()
    snapshot = dict(flag)

    def completed():
        # Once stopped the upload worker owns sidecar writes and consumes jobs.
        if rec.state != "discarded" and not getattr(rec, "_flag_upload_owned", False):
            _apply(rec, flag, job)
            jobs.pop(id(flag), None)
        return False

    def worker():
        try:
            client = plaud_api.PlaudClient()
            if not client.is_logged_in():
                raise RuntimeError("Login necessário")
            job.set_result(client.prepare_audio_mark(snapshot))
        except Exception as exc:
            job.set_exception(exc)
        GLib.idle_add(completed)

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception as exc:
        job.set_exception(exc)
        _apply(rec, flag, job)
        jobs.pop(id(flag), None)


def finish_pending(rec):
    """Upload worker joins existing tasks instead of creating duplicate marks."""
    jobs = getattr(rec, "_flag_jobs", {})
    for key, (flag, job) in list(jobs.items()):
        _apply(rec, flag, job)
        jobs.pop(key, None)
