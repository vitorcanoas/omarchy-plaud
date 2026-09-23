#!/usr/bin/env python3
"""
plaud-linux :: login flow

Two entry points:
  1. handle_url(url)  -- invoked by the plaud:// desktop handler; extracts auth_code,
                         mints tokens via PlaudClient, notifies the user.
  2. begin_login()    -- opens the Plaud web login in the browser so the user signs in;
                         the browser then redirects to plaud://login?auth_code=... which
                         fires the handler above.

The auth_code is single-use, so we consume it immediately.
"""
import hashlib
import json
import subprocess
import threading
import time
import sys
import urllib.parse

try:
    from . import plaud_api  # noqa: E402
except ImportError:
    import plaud_api  # noqa: E402

WEB_HOST = "https://web.plaud.ai"
_LOGIN_STATE = plaud_api.STATE / "login-handoff.json"
_active_workers = 0


def _handoff_state():
    try:
        value = json.loads(_LOGIN_STATE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_handoff(value):
    # Timestamp and one-way digests only; never persist auth codes or URLs.
    try:
        from .audio import write_json
    except ImportError:
        from audio import write_json
    write_json(_LOGIN_STATE, value)


def is_authenticating():
    return _active_workers > 0



def login_url():
    # Exact desktop login entry: /launch-desktop?from=desktop&desktop_uuid=<encryptUuid>
    c = plaud_api.PlaudClient()
    enc = plaud_api.encrypt_uuid(c.device)
    return f"{WEB_HOST}/launch-desktop?from=desktop&desktop_uuid={enc}"


def _notify(title, body):
    try:
        subprocess.Popen(["notify-send", "-a", "Plaud Linux", title, body],
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


def extract_auth_code(url):
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() != "plaud":
            return None
        q = parsed.query
        # Not parse_qs(): it decodes values with unquote_plus(), which treats
        # a literal '+' as an encoded space (application/x-www-form-urlencoded
        # semantics). A plaud:// callback is a URI query, not a form body, so
        # a literal '+' the backend put in the auth_code must survive as '+'.
        # unquote() still decodes %XX escapes (including %2B -> '+') -- it
        # just doesn't also fold '+' into a space.
        for key, sep, value in (p.partition("=") for p in q.split("&")):
            if key == "auth_code" and sep:
                return urllib.parse.unquote(value)
        return None
    except Exception:
        return None


def handle_url(url):
    code = extract_auth_code(url)
    if not code:
        _notify("Plaud Linux", "Não recebi um código de login válido. Abra o login pelo aplicativo.")
        return False
    try:
        # Serialize callbacks with token refresh, including fallback processes.
        with plaud_api._token_flock():
            client = plaud_api.PlaudClient()
            state = _handoff_state()
            now = time.time()
            stored_seen = state.get("seen", {})
            if not isinstance(stored_seen, dict):
                stored_seen = {}
            seen = {key: stamp for key, stamp in stored_seen.items()
                    if isinstance(stamp, (int, float)) and now - stamp < 900}
            digest = hashlib.sha256(code.encode()).hexdigest()
            if digest in seen:
                return client.is_logged_in()
            requested_at = state.get("requested_at", 0)
            pending = (isinstance(requested_at, (int, float))
                       and 0 <= now - requested_at < 900)
            if client.is_logged_in() and not pending:
                # Official service-DTJX6kep.js also ignores late callbacks
                # when logged in. Re-exchanging a one-use code causes a false error.
                _notify("Plaud Linux", "Sua conta já está conectada. Pode voltar ao aplicativo.")
                return True
            seen[digest] = now
            _save_handoff({"seen": dict(list(seen.items())[-16:])})
            client.login_with_auth_code(code)
        _notify("Plaud Linux", "Login concluído. Clique em Iniciar gravação quando quiser.")
        return True
    except Exception as exc:
        # Auth responses can contain tokens; never interpolate them into logs
        # or desktop notifications, even when another login step failed.
        _notify("Plaud Linux", "Não foi possível concluir o login. Abra o login novamente em Preferências. Sua gravação local está preservada.")
        print(f"login failed ({type(exc).__name__}); credentials omitted", file=sys.stderr)
        return False


def handle_url_async(url, on_complete=None):
    """Keep token exchange off the GTK thread and retain the worker until done."""
    global _active_workers
    from gi.repository import GLib
    _active_workers += 1

    def finish(ok):
        global _active_workers
        _active_workers -= 1
        if on_complete is not None:
            on_complete(ok)
        return False

    def worker():
        ok = False
        try:
            ok = handle_url(url)
        finally:
            GLib.idle_add(finish, ok)

    try:
        threading.Thread(target=worker, daemon=False).start()
    except Exception:
        _active_workers -= 1
        _notify("Plaud Linux", "Não consegui iniciar o login. Tente novamente.")


def begin_login(force=False):
    try:
        client = plaud_api.PlaudClient()
        if client.is_logged_in() and not force:
            _notify("Plaud Linux", "Sua conta já está conectada.")
            return False
        # A new login explicitly requested by the user may replace the account.
        # Do not delete the existing credentials before the exchange succeeds.
        state = _handoff_state()
        state["requested_at"] = time.time()
        _save_handoff(state)
        subprocess.Popen(["xdg-open", login_url()], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        _notify("Plaud Linux", "Conclua o login no navegador. Ao voltar, o aplicativo confirmará a conexão.")
        return True
    except Exception as exc:
        _notify("Plaud Linux", "Não consegui abrir o navegador para entrar no Plaud. Tente novamente.")
        print(f"opening login failed ({type(exc).__name__})", file=sys.stderr)
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("plaud://"):
        ok = handle_url(sys.argv[1])
        sys.exit(0 if ok else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--begin":
        begin_login()
    else:
        c = plaud_api.PlaudClient()
        print("logged in:", c.is_logged_in())
