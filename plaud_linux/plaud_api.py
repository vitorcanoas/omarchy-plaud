#!/usr/bin/env python3
"""
plaud-linux :: Plaud cloud client (replicates the official desktop REST flow)

Implements the exact sequence recovered from app.asar v1.3.2 (see PLAUD_API_NOTES.md):
  login (auth_code -> UT) -> list workspaces -> mint WT
  upload: presigned -> PUT parts -> merge -> confirm -> file_id
  generate: POST /ai/transsumm/{file_id}  (poll)

Auth uses the USER's own legitimate login (auth_code from the plaud:// OAuth handoff).
Tokens are stored encrypted at rest (Fernet key derived from machine-id + uid),
mirroring how the official app keeps them in an encrypted store.
"""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken

try:
    from . import paths
except ImportError:
    import paths

BASE = paths.DATA_HOME
STATE = paths.STATE
TOKENS_FILE = STATE / "tokens.enc"
DEVICE_FILE = STATE / "device.json"
SETTINGS_FILE = STATE / "settings.json"

DEFAULT_API = "https://api.plaud.ai"
APP_VERSION = "1.3.2"
CLIENT_ID = "desktop"
# HMAC salt used by the official app to derive desktop_uuid from the machine uuid
UUID_HMAC_SALT = "PLAUD-D_E_S_K_T_O_P"


# ---------------------------------------------------------------- encryption
def _fernet():
    """Derive a stable Fernet key from machine-id + uid (same-machine only)."""
    try:
        mid = Path("/etc/machine-id").read_text().strip()
    except OSError:
        mid = "no-machine-id"
    seed = f"{mid}:{os.getuid()}:plaud-linux".encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(key)


# Serializes refresh + save. Two locks, because there are two races.
#
# _TOKEN_LOCK covers threads within this process (main.py uploads on a worker
# thread). It is module-level, not per-instance, because the clients that would
# race are separate instances — per-instance would lock nothing.
#
# _token_flock covers SEPARATE PROCESSES, which is the race that actually exists
# today: the app is single-shot, so a plaud:// callback or a second launch is a
# second process, and a threading.Lock is invisible across processes.
_TOKEN_LOCK = threading.RLock()
LOCK_FILE = STATE / "tokens.lock"


_flock_depth = 0


@contextlib.contextmanager
def _token_flock():
    """Hold both the in-process lock and an inter-process flock.

    Reentrant, like the RLock it wraps: _wt() takes it and then calls
    _refresh_wt(), which takes it again. flock() on a SECOND descriptor of the
    same file blocks against the first even within one process, so nesting must
    reuse the outermost hold rather than open a new descriptor.

    Best-effort on the flock itself: if the lock file cannot be created or
    locked (read-only home, a filesystem without flock), proceed with the thread
    lock alone rather than block the upload. A rare double refresh is a better
    failure than a recording that cannot be sent — Principle II.

    The lock IS held across the refresh POST, so a waiter can block for up to
    the 30 s request timeout. That does not stall the UI today: uploads run on
    main.py's worker thread, and ensure_login_or_prompt() runs before that
    thread exists — is_logged_in() only reads self.tokens and never takes this
    lock. Re-check this when the app goes resident (CAN-310), where a settings
    or history window could call in from the GTK main thread.
    """
    global _flock_depth
    with _TOKEN_LOCK:
        if _flock_depth:                      # already held by this thread
            _flock_depth += 1
            try:
                yield
            finally:
                _flock_depth -= 1
            return
        fd = None
        try:
            fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            # Say so: otherwise a read-only home silently loses the
            # inter-process guarantee and the next unexplained logout has no
            # trace in app.log.
            print(f"[auth] sem trava entre processos ({type(e).__name__}: {e}) — "
                  f"seguindo apenas com a trava de threads")
            if fd is not None:
                os.close(fd)
                fd = None
        _flock_depth = 1
        try:
            yield
        finally:
            _flock_depth = 0
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _save_tokens(d):
    """Write tokens.enc atomically: temp file in the same dir, then os.replace.

    A bare write_bytes() truncates before it refills, so a crash or a concurrent
    reader in that window sees a short buffer -> InvalidToken -> {} -> the user
    appears logged out. os.replace() is atomic on the same filesystem, so a
    reader sees either the whole old file or the whole new one.
    """
    blob = _fernet().encrypt(json.dumps(d).encode())
    # pid alone collides between threads of one process: one writer's O_TRUNC
    # destroys the other's temp file and os.replace then raises FileNotFoundError.
    tmp = TOKENS_FILE.with_name(
        TOKENS_FILE.name + f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # os.write may write fewer bytes than asked; a short write here is a
            # truncated token file, which is the very failure this function exists
            # to prevent.
            view = memoryview(blob)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(TOKENS_FILE))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _load_tokens():
    if not TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(_fernet().decrypt(TOKENS_FILE.read_bytes()).decode())
    except (InvalidToken, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------- device id
def device_uuid():
    if DEVICE_FILE.exists():
        try:
            return json.loads(DEVICE_FILE.read_text())["uuid"]
        except (json.JSONDecodeError, KeyError):
            pass
    u = str(uuid.uuid4())
    DEVICE_FILE.write_text(json.dumps({"uuid": u}))
    return u


def encrypt_uuid(raw_uuid):
    """desktop_uuid = HMAC-SHA256(salt, uuid) hex — matches the official app."""
    import hmac

    return hmac.new(UUID_HMAC_SALT.encode(), raw_uuid.encode(), hashlib.sha256).hexdigest()


def _request_id():
    return uuid.uuid4().hex[:11]


def _settings_language():
    """The app-language set from the settings window (CAN-315), or "pt".

    Reads settings.json directly rather than importing settings.py: that
    module pulls in Gtk at module scope, and plaud_api.py never touches GTK
    (CLAUDE.md layer rule). "pt" was this function's hardcoded literal before
    the settings window existed, kept as the default for a user who has never
    opened it.
    """
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f).get("language", "pt")
    except (OSError, ValueError):
        return "pt"


class PlaudClient:
    def __init__(self):
        self.api = DEFAULT_API
        self.tokens = _load_tokens()  # {ut, wt, wrt, ws_id, api_domain, wt_exp}
        if self.tokens.get("api_domain"):
            self.api = self.tokens["api_domain"]
        self.device = device_uuid()

    # -------- header builders
    def _headers(self, bearer=None, lang=None):
        h = {
            "edit-from": "desktop",
            "app-platform": "desktop",
            "app-versionNumber": APP_VERSION,
            "app-language": lang or _settings_language(),
            "X-Device-Id": self.device,
            "X-Request-Id": _request_id(),
            "Content-Type": "application/json",
        }
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        return h

    def is_logged_in(self):
        return bool(self.tokens.get("wt") and self.tokens.get("ws_id"))

    # -------- login: auth_code -> UT -> workspaces -> WT
    def login_with_auth_code(self, auth_code):
        # desktop_uuid is the HMAC-encrypted uuid; X-Device-Id stays the raw uuid.
        # Not routed through _biz(): that sends the WT, and there is none yet.
        # This is where a user outside the default region meets -302 first --
        # with an empty tokens.enc there is no stored api_domain to start from,
        # so the redirect has to be followed here or login cannot complete at
        # all. The auth_code is single-use, so the retry must reuse the same
        # one rather than sending the user back through the browser.
        def _post_login():
            return requests.post(
                f"{self.api}/auth/access-token-auth-code",
                headers=self._headers(),
                json={
                    "client_id": CLIENT_ID,
                    "auth_code": auth_code,
                    "desktop_uuid": encrypt_uuid(self.device),
                },
                timeout=30,
            ).json()

        data = _post_login()
        if self._adopt_region(data):
            data = _post_login()
        ut = _extract(data, ["access_token", "token", "user_token", "ut"])
        if not ut:
            raise RuntimeError(f"auth code exchange failed (status={data.get('status')})")
        # Never combine a new user's UT with an earlier workspace's WT/WRT.
        self.tokens = {"api_domain": self.api}
        self.tokens["ut"] = str(ut).removeprefix("bearer ").removeprefix("Bearer ").strip()
        _save_tokens(self.tokens)  # persist UT immediately so it's never lost
        # list workspaces — real shape: {"status":0,"data":{"workspaces":[{...}]}}
        def _get_ws():
            return requests.get(
                f"{self.api}/team-app/workspaces/list",
                headers=self._headers(bearer=self.tokens["ut"]),
                timeout=30,
            ).json()

        ws = _get_ws()
        if self._adopt_region(ws):
            ws = _get_ws()
        ws_list = _workspaces(ws)
        if not ws_list:
            raise RuntimeError(f"workspace lookup failed (status={ws.get('status')})")
        # prefer an active workspace; else the first
        ws0 = next((w for w in ws_list if w.get("status") in (None, "active", 1)), ws_list[0])
        ws_id = ws0.get("workspace_id") or ws0.get("id")
        # The workspace record can carry its own regional host. It is adopted
        # through the same allowlist as a -302 redirect: this value arrives in
        # a response body, so it is attacker-controlled under exactly the
        # threat model _is_plaud_host() exists for, and _mint_wt() below sends
        # the UT as a bearer token to whatever host this sets. An unvalidated
        # assignment here handed that token to any host the response named.
        dom = _normalize_host(ws0.get("api_domain"))
        if dom and _is_plaud_host(dom):
            self.api = dom
        self._mint_wt(ws_id)
        _save_tokens(self.tokens)
        return True

    def _mint_wt(self, ws_id):
        # W2: mint WT with UT. POST with empty body {}.
        # Follows -302 like every other call: a redirect here used to abort
        # login with "mint WT failed: {...domains...}" after the auth_code was
        # already burned, which reads as a broken account rather than a
        # routing problem.
        def _post():
            return requests.post(
                f"{self.api}/user-app/auth/workspace/token/{ws_id}",
                headers=self._headers(bearer=self.tokens["ut"]),
                json={},
                timeout=30,
            ).json()

        r = _post()
        if self._adopt_region(r):
            r = _post()
        d = r.get("data", r)
        wt = d.get("workspace_token") or d.get("access_token") or d.get("token")
        if not wt:
            raise RuntimeError(f"workspace token exchange failed (status={r.get('status')})")
        self.tokens["wt"] = wt
        self.tokens["wrt"] = d.get("refresh_token")
        self.tokens["ws_id"] = ws_id
        self.tokens["api_domain"] = self.api
        exp = d.get("expires_in")
        rexp = d.get("refresh_expires_in")
        self.tokens["wt_exp"] = int(time.time()) + int(exp) if exp else None
        self.tokens["wrt_exp"] = int(time.time()) + int(rexp) if rexp else None

    def _refresh_wt(self, after_rejection=False):
        """Refresh the WT. Serialized so one expiry costs one WRT rotation.

        Held under the lock: two clients hitting the same expiry would otherwise
        POST the same WRT twice. A rotating backend rejects the second (already
        consumed), and the loser's _save_tokens would persist its stale WRT over
        the winner's — a dead refresh token in tokens.enc, which reaches the user
        as an unexplained logout.

        `after_rejection` says the backend just refused a token. In that case we
        ALWAYS mint a new one: whether some other token on disk is alive is a
        question only the server can answer, so adopting one would turn _biz's
        single retry into a coin flip. Adoption is a saving, never a repair.
        """
        with _token_flock():
            # Proactive refresh only: another client — in this process or
            # another one — may have just refreshed, so pick up its result
            # instead of spending a second WRT rotation.
            fresh = _load_tokens() if not after_rejection else {}
            exp = fresh.get("wt_exp")
            if (
                # the WT is the point of adopting: without it _wt() returns None
                # and _biz sends the call with no Authorization header at all
                fresh.get("wt")
                and fresh.get("wrt")
                and fresh.get("wrt") != self.tokens.get("wrt")
                # only the same workspace, and only the same host: adopting
                # across either would send this session's upload_id to a
                # backend that never issued it
                and fresh.get("ws_id") == self.tokens.get("ws_id")
                and fresh.get("api_domain") == self.tokens.get("api_domain")
                # a missing wt_exp means "age unknown", which is not "fresh"
                and exp and time.time() <= exp - 60
            ):
                # Update, not replace. A token file written without `ut` would
                # otherwise drop this session's, and the next _mint_wt() raises
                # KeyError. Adoption takes what is fresher; it does not get to
                # discard a key it has no opinion about.
                self.tokens.update(fresh)
                return True
            return self._refresh_wt_locked()

    def _refresh_wt_locked(self):
        # W3: refresh WT with WRT. POST empty body, Authorization: Bearer <WRT>.
        if not self.tokens.get("wrt") or not self.tokens.get("ws_id"):
            return False

        # A -302 here used to fall through to `return False`, which the caller
        # reads as "refresh failed" -- so a wrong-host answer surfaced as a
        # dead session rather than a redirect, and the WRT looked spent when
        # it was fine.
        def _post():
            return requests.post(
                f"{self.api}/user-app/auth/workspace/refresh/{self.tokens['ws_id']}",
                headers=self._headers(bearer=self.tokens["wrt"]),
                json={},
                timeout=30,
            ).json()

        r = _post()
        if self._adopt_region(r):
            r = _post()
        if r.get("status") == -420:
            # The workspace session was revoked, but the user login can still
            # be valid. Reuse W2 once under the same lock; _biz still owns the
            # single business retry. Error replies may have data=null.
            if not self.tokens.get("ut"):
                return False
            previous, previous_api = self.tokens.copy(), self.api
            try:
                self._mint_wt(self.tokens["ws_id"])
                _save_tokens(self.tokens)
            except (RuntimeError, KeyError, AttributeError, TypeError, ValueError, OSError,
                    requests.exceptions.RequestException):
                # Mint updates fields before parsing expiry. A bad reply or
                # failed atomic save must not leave a half-installed session.
                self.tokens.clear()
                self.tokens.update(previous)
                self.api = previous_api
                return False
            return True
        d = r.get("data", r)
        wt = d.get("workspace_token") or d.get("access_token") or d.get("token")
        if not wt:
            return False
        self.tokens["wt"] = wt
        if d.get("refresh_token"):
            self.tokens["wrt"] = d["refresh_token"]
        exp = d.get("expires_in")
        rexp = d.get("refresh_expires_in")
        self.tokens["wt_exp"] = int(time.time()) + int(exp) if exp else None
        if rexp:
            self.tokens["wrt_exp"] = int(time.time()) + int(rexp)
        _save_tokens(self.tokens)
        return True

    def _wt(self):
        # Check-then-refresh must be one critical section: otherwise both callers
        # read the same stale expiry before either has refreshed.
        with _token_flock():
            exp = self.tokens.get("wt_exp")
            if exp and time.time() > exp - 60:
                self._refresh_wt()
            return self.tokens.get("wt")

    def _adopt_region(self, resp):
        """Adopt the regional host from a -302 response. True if it changed.

        The host is persisted into tokens.enc immediately, so a user outside
        the default region rediscovers it once rather than every launch -- and,
        more importantly, does not lose it when a login fails after the
        auth_code was already burned. See the comment on the write below.

        Same host is not an adoption -- returning True there would let _biz
        retry the identical call forever against the identical backend. The
        comparison is between NORMALIZED forms: "HTTPS://API.PLAUD.AI",
        "api.plaud.ai." and "api.plaud.ai:443" are the host we are already on,
        and treating any of them as new would ping-pong, rewriting tokens.enc
        on every call and never returning a usable result.
        """
        host = _redirect_host(resp)
        if not host or host == (_normalize_host(self.api) or self.api):
            return False
        self.api = host
        # Persist unconditionally, even with no tokens yet. The empty-tokens
        # case is exactly the pre-login redirect, and it is the one that must
        # not be dropped: if the backend burns the auth_code and then answers
        # -302, the retry fails and the region would be lost along with the
        # code, sending every later browser re-login back to the default host
        # to burn a fresh code the same way -- a permanent lockout. A file
        # holding only api_domain still reads as logged out (is_logged_in()
        # wants wt and ws_id), so this costs nothing but the routing hint.
        self.tokens["api_domain"] = host
        _save_tokens(self.tokens)
        return True

    def _biz(self, method, path, **kw):
        """Business call with WT; auto-refresh on 419/401-ish, re-host on -302."""
        # Pop once into locals: the retry used to re-read kw for headers/timeout
        # that the first attempt had already popped, so it sent {} and 60 s.
        extra = kw.pop("headers", {})
        timeout = kw.pop("timeout", 60)

        def _send():
            # Built per attempt, not captured once: a -302 retry changes
            # self.api, and a url frozen before the redirect would resend to
            # the host that just refused.
            h = self._headers(bearer=self._wt())
            h.update(extra)
            r = requests.request(method, f"{self.api}{path}", headers=h,
                                 timeout=timeout, **kw)
            try:
                return r, r.json()
            except ValueError:
                # Non-JSON (502, HTML error page). The retry used to let this
                # raise JSONDecodeError as the user-facing error message.
                return r, None

        r, j = _send()
        if j is None:
            return {"_raw": r.text, "_status": r.status_code}
        status = j.get("status")
        # -302 is checked first and separately: it is a wrong-host answer, not
        # a token answer, and refreshing the WT against the wrong host would
        # burn a WRT rotation without ever fixing the routing. One redirect is
        # enough -- the response names its own replacement, so a second -302
        # from the named host is the backend contradicting itself, and looping
        # on it would hang the upload.
        if status == -302 and self._adopt_region(j):
            r, j = _send()
            if j is None:
                return {"_raw": r.text, "_status": r.status_code}
            status = j.get("status")
        if status in (-419, -10000) or r.status_code == 401:
            # The token was refused, so mint a new one — never adopt.
            if self._refresh_wt(after_rejection=True):
                r, j = _send()
                if j is None:
                    return {"_raw": r.text, "_status": r.status_code}
        return j

    # -------- upload sequence
    def upload_and_generate(self, filepath, filename=None, progress=None, auto_generate=True):
        filepath = Path(filepath)
        blob = filepath.read_bytes()
        filesize = len(blob)
        start_ms = int(filepath.stat().st_mtime * 1000)
        fname = filename or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ms / 1000))

        def rep(msg):
            if progress:
                progress(msg)

        print(f"[upload] {filepath.name} ({filesize} bytes) como {fname!r}")
        rep("presigned…")
        pre = self._biz("POST", "/file/get_upload_presigned_url",
                        json={"filesize": filesize, "file_type": "OGG"})
        d = pre.get("data", pre)
        upload_id = d.get("upload_id")
        object_name = d.get("object_name")
        part_urls = d.get("part_urls") or []
        if not part_urls:
            raise RuntimeError(f"no presigned urls: {json.dumps(pre)[:300]}")

        # PUT 5 MB parts
        CHUNK = 5 * 1024 * 1024
        # The backend decides how many parts there are; we only assume the size.
        # If it hands out fewer URLs than the file needs, the tail would never be
        # PUT and merge/confirm would still succeed — the user gets "Enviado!" for
        # a truncated recording. Refuse before anything is registered.
        if not filesize:
            # 0 bytes passes every part guard (needed=0, no chunk to PUT) and
            # would still get a file_id — "Enviado!" for nothing.
            raise RuntimeError("arquivo vazio (0 bytes) — nada a enviar")
        needed = -(-filesize // CHUNK)  # ceil
        if len(part_urls) < needed:
            raise RuntimeError(
                f"presigned urls insuficientes: {len(part_urls)} para {filesize} bytes "
                f"({needed} partes de {CHUNK}) — upload abortado para nao truncar"
            )
        etags = []
        sent = 0
        for i, url in enumerate(part_urls):
            chunk = blob[i * CHUNK:(i + 1) * CHUNK]
            if not chunk:
                break
            rep(f"upload {i+1}/{len(part_urls)}…")
            pr = requests.put(url, data=chunk, timeout=300)
            # A rejected PUT used to become Etag:"" and sail into the merge.
            if pr.status_code not in (200, 201):
                raise RuntimeError(
                    f"parte {i+1}/{len(part_urls)} rejeitada: HTTP {pr.status_code} "
                    f"{pr.text[:200]}"
                )
            etag = (pr.headers.get("ETag") or "").replace('"', "")
            if not etag:
                raise RuntimeError(
                    f"parte {i+1}/{len(part_urls)} sem ETag na resposta — "
                    f"merge truncaria o arquivo"
                )
            etags.append({"Etag": etag, "PartNumber": i + 1})
            sent += len(chunk)

        # Counts what was actually PUT, so it still fires if the loop ends early
        # for a reason the guards above do not anticipate.
        if sent != filesize:
            raise RuntimeError(
                f"upload incompleto: {sent} de {filesize} bytes enviados — abortado"
            )

        rep("merge…")
        self._biz("POST", "/file/merge_multipart",
                  json={"upload_id": upload_id, "object_name": object_name, "parts": etags})

        rep("confirm…")
        tz = -time.timezone // 3600
        conf = self._biz("POST", "/file/confirm_upload", json={
            "upload_id": upload_id, "object_name": object_name,
            "scene": 102, "is_tmp": 0, "support_mul_summ": True,
            "file_type": "OGG", "filename": fname,
            "start_time": start_ms, "session_id": start_ms // 1000,
            "serial_number": str(start_ms), "timezone": tz,
        })
        cd = conf.get("data", conf)
        file_id = cd.get("id") or cd.get("file_id")
        if not file_id:
            raise RuntimeError(f"confirm failed: {json.dumps(conf)[:300]}")
        print(f"[upload] file_id={file_id}")

        if auto_generate:
            self.generate(file_id, progress=progress)
        rep("done")
        return file_id

    def generate(self, file_id, progress=None):
        """Ask the backend for the automatic summary of an already-uploaded file.

        Split out of upload_and_generate so the "Gerar automaticamente" choice
        can be made *after* the upload finishes, which is when the official
        client asks (docs/plaud-desktop/README.md §6, media/image44.png).
        Skipping it leaves the note uploaded and transcribed but unsummarised --
        per PLAUD_API_NOTES §Upload step 5, confirm_upload is what starts the
        transcription, so a skipped call costs the summary, never the audio.
        """
        if progress:
            progress("generating…")
        tz = -time.timezone // 3600
        return self._biz("POST", f"/ai/transsumm/{file_id}", json={
            "is_reload": 0, "summ_type": "AUTO-SELECT", "summ_type_type": "system",
            "info": '{"language":"auto","diarization":1,"llm":"auto"}',
            "support_mul_summ": True, "timezone": tz,
        })

    def wait_for_generation(self, file_id, initial, timeout=1800):
        """Poll the official transsumm contract: 0 pending, 1 complete.

        A bounded wait stops desktop monitoring, never cancels the cloud task.
        """
        deadline = time.monotonic() + timeout
        result = initial
        while isinstance(result, dict) and result.get("status") == 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("O Plaud ainda está processando. Confira a nota em Envios recentes.")
            time.sleep(min(3, remaining))
            if time.monotonic() >= deadline:
                raise TimeoutError("O Plaud ainda está processando. Confira a nota em Envios recentes.")
            result = self.generate(file_id)
        if not isinstance(result, dict) or result.get("status") != 1:
            raise RuntimeError((result or {}).get("msg", "Resposta inesperada ao gerar nota")
                               if isinstance(result, dict) else "Resposta inesperada ao gerar nota")
        return result

    # -------- attach screenshots (images) to a recording
    def prepare_audio_mark(self, flag):
        """Prepare a standalone highlight before the recording has a file id.

        The same desktop payload is used both for live previews and recovery.
        Result text stays in the returned value; never log recording contents.
        """
        p = Path(flag["path"]) if isinstance(flag, dict) else Path(flag)
        if not p.is_file():
            raise FileNotFoundError("Trecho de áudio indisponível")
        link = self._biz("POST", "/file/get_s3_upload_link", json={
            "op_type": "mark", "file_suffixs": ["ogg"], "ppc_status": 1})
        ld = link.get("data", link)
        if isinstance(ld, list):
            ld = ld[0] if ld else {}
        upload_link = ld.get("upload_link") or (ld.get("upload_links") or [None])[0]
        storage = ld.get("storage_location") or (ld.get("storage_locations") or [None])[0]
        if not upload_link or not storage:
            raise RuntimeError("Não foi possível preparar a marcação")
        with p.open("rb") as stream:
            payload = stream.read()
        response = requests.put(upload_link, data=payload, timeout=120)
        if response.status_code != 200:
            raise RuntimeError("Falha no envio da marcação")
        task = self._biz("POST", "/ai/mark/create_task", json={
            "mark_type": 1,
            "mark_content": [{"content": storage, "type": "audio"}],
            "ppc_status": 1})
        td = task.get("data", task)
        mark_id = td.get("mark_id") or td.get("id")
        if not mark_id:
            raise RuntimeError("A marcação não foi criada")
        for attempt in range(10):
            result = self._biz("POST", "/ai/mark/get_mark_result", json={
                "mark_ids": [mark_id], "ppc_status": 1})
            data = result.get("data", result)
            items = data.get("mark_results", []) if isinstance(data, dict) else []
            item = next((entry for entry in items if entry.get("mark_id") == mark_id), {})
            if item.get("status") == -2:
                raise RuntimeError("Nenhuma fala identificada neste trecho")
            mr = item.get("mark_result")
            if mr:
                return {
                    "mark_type": 1, "mark_id": mark_id,
                    "mark_content": mr.get("result", "") if isinstance(mr, dict) else str(mr),
                    "picture_link": storage,
                    "timestamp": int(flag.get("t", 0)) * 1000 if isinstance(flag, dict) else 0,
                }
            if attempt < 9:
                time.sleep(3)
        raise RuntimeError("A análise da marcação ainda não terminou")

    def attach_screenshots(self, file_id, shots, notes=None, progress=None, flags=None):
        """Attach screenshots ({'path','t',...}), typed notes ({'text','t',...})
        and flag snippets ({'path','t',...}).
        Mirrors the official highlight/mark flow (PLAUD_API_NOTES §7):
          get_s3_upload_link -> PUT file -> mark/create_task -> mark/get_mark_result
          -> update_source_info
        Notes are InputMarks and skip straight to update_source_info; everything
        is batched into that one call.
        Best-effort: failures per image are logged, not fatal."""
        def rep(m):
            if progress:
                progress(m)

        marks = []
        for i, s in enumerate(shots):
            p = Path(s["path"]) if isinstance(s, dict) else Path(s)
            if not p.exists():
                print(f"[mark] {i+1}: arquivo ausente, ignorado: {p}")
                continue
            try:
                rep(f"screenshot {i+1}/{len(shots)}…")
                link = self._biz("POST", "/file/get_s3_upload_link",
                                 json={"op_type": "mark", "file_suffixs": ["png"], "ppc_status": 1})
                # data is a LIST of link objects, one per requested suffix.
                ld = link.get("data", link)
                if isinstance(ld, list):
                    ld = ld[0] if ld else {}
                upload_link = ld.get("upload_link") or (ld.get("upload_links") or [None])[0]
                storage = ld.get("storage_location") or (ld.get("storage_locations") or [None])[0]
                if not upload_link or not storage:
                    print(f"[mark] {i+1}: sem upload_link/storage: {json.dumps(link)[:300]}")
                    continue
                # No Content-Type: the URL is signed without it, and sending one
                # makes S3 reject the PUT with 403 SignatureDoesNotMatch.
                pr = requests.put(upload_link, data=p.read_bytes(), timeout=120)
                print(f"[mark] {i+1}: s3 put {pr.status_code} ({p.stat().st_size} bytes)")
                if pr.status_code != 200:
                    print(f"[mark] {i+1}: s3 rejeitou: {pr.text[:200]}")
                    continue
                task = self._biz("POST", "/ai/mark/create_task", json={
                    "mark_type": 2,
                    "mark_content": [{"content": storage, "type": "picture"}],
                    "ppc_status": 1,
                })
                print(f"[mark] {i+1}: create_task -> {json.dumps(task)[:300]}")
                td = task.get("data", task)
                mark_id = td.get("mark_id") or td.get("id")
                if not mark_id:
                    print(f"[mark] {i+1}: sem mark_id na resposta — nao anexando")
                    continue
                # Step 4 of PLAUD_API_NOTES §Screenshot: wait for the backend to
                # analyse the image. Observed: data.mark_results[0].status goes
                # 1 -> mark_result populated. Bounded and non-fatal — on timeout
                # we attach anyway rather than drop the screenshot.
                content = ""
                for _ in range(10):
                    res = self._biz("POST", "/ai/mark/get_mark_result",
                                    json={"mark_ids": [mark_id], "ppc_status": 1})
                    rd = res.get("data", res)
                    items = rd.get("mark_results") or [] if isinstance(rd, dict) else []
                    mr = items[0].get("mark_result") if items else None
                    done = bool(mr)
                    print(f"[mark] {i+1}: get_mark_result done={done} -> {json.dumps(res)[:200]}")
                    if done:
                        content = mr.get("result", "") if isinstance(mr, dict) else str(mr)
                        break
                    time.sleep(2)
                # Field order and units match highlightService.ts (app.asar 1.3.2):
                # timestamp is MILLISECONDS, and mark_content carries the AI result.
                marks.append({
                    "mark_type": 2,
                    "mark_id": mark_id,
                    "mark_content": content,
                    "picture_link": storage,
                    "timestamp": (int(s.get("t", 0)) if isinstance(s, dict) else 0) * 1000,
                })
            except Exception as e:
                print(f"[mark] {i+1}: falhou: {type(e).__name__}: {e}")
                continue

        # AudioMark (type 1): the picture path with two literals changed --
        # 'ogg' instead of 'png' in the upload link, and mark_type 1 / type
        # "audio" instead of 2 / "picture" in create_task
        # (highlightService-CUAOVE3u.js:162,289). There is deliberately no
        # duration or time-range field: the clip is a separately uploaded file,
        # not an offset into the recording -- when the official client runs this
        # the recording has no file_id yet, so an offset would have nothing to
        # be an offset into.
        for i, f in enumerate(flags or []):
            try:
                rep(f"marcação {i+1}/{len(flags)}…")
                cached = f.get("cloud_mark") if isinstance(f, dict) else None
                mark = dict(cached) if cached else self.prepare_audio_mark(f)
                if isinstance(f, dict) and f.get("edited"):
                    mark["mark_content"] = f.get("text", "")
                marks.append(mark)
            except Exception as exc:
                print(f"[flag] {i+1}: falhou: {type(exc).__name__}")
                continue

        # InputMark (type 3) skips the whole S3/create_task/get_mark_result flow.
        # Per highlightService.ts:110-119 it goes straight into update_source_info
        # with an empty mark_id and picture_link, carrying the user's raw text.
        for nt in (notes or []):
            marks.append({
                "mark_type": 3,
                "mark_id": "",
                "mark_content": nt["text"],
                "picture_link": "",
                "timestamp": int(nt.get("t", 0)) * 1000,
            })
        # The official client relies on insertion order (it never sorts); we collect
        # screenshots and notes in separate lists, so we restore the order explicitly.
        marks.sort(key=lambda m: m["timestamp"])
        print(f"[mark] {len(shots)} screenshot(s) + {len(notes or [])} anotacao(oes)"
              f" + {len(flags or [])} marcacao(oes)")

        if marks:
            try:
                upd = self._biz("POST", "/ai/update_source_info", json={
                    "file_id": file_id,
                    "source_type": "mark_memo",
                    "source_title": "Destaques da gravação",
                    "source_content": json.dumps(marks),
                })
                print(f"[mark] update_source_info ({len(marks)} marks) -> {json.dumps(upd)[:300]}")
            except Exception as e:
                print(f"[mark] update_source_info falhou: {type(e).__name__}: {e}")
        return len(marks)


def _workspaces(resp):
    """Pull the workspaces list out of the /workspaces/list response.
    Real shape: {"status":0,"data":{"workspaces":[...]}}. Tolerant of variants."""
    if not isinstance(resp, dict):
        return []
    data = resp.get("data", resp)
    if isinstance(data, dict):
        for k in ("workspaces", "list", "workspace_list", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    if isinstance(data, list):
        return data
    return []


# The only hosts a -302 may send us to. Tolerance about response *shape* is a
# virtue here (see _extract/_workspaces); tolerance about *where the WT goes*
# is not. A redirect names the host that will receive `Authorization: Bearer
# <WT>` on every later call, and nothing re-validates a stored api_domain, so
# one hostile or MITM'd response would otherwise redirect this client
# persistently with no reset path short of deleting tokens.enc.
API_HOST_SUFFIX = ".plaud.ai"
API_HOST_EXACT = "plaud.ai"


def _normalize_host(host):
    """Normalize a base URL to `https://host[:port]`, or None if unusable.

    Comparison-grade: two spellings of the same host must normalize to the same
    string, or the same-host guard in _adopt_region misses and the client
    ping-pongs, rewriting tokens.enc on every call and never returning a result.
    Case, the scheme, a trailing dot and an explicit :443 are all cosmetic.
    """
    if not isinstance(host, str):
        return None
    host = host.strip()
    # A control character here would be a header/URL injection vector, and no
    # legitimate host contains one.
    if not host or any(c in host for c in "\r\n\t\x00 "):
        return None
    low = host.lower()
    if low.startswith("https://"):
        host = host[8:]
    elif low.startswith("http://"):
        # Refused, not upgraded: a -302 that downgrades the transport is not a
        # redirect we follow. Upgrading it silently would hide the attempt.
        return None
    elif "://" in host:
        # javascript:, file:, or a second scheme glued on by an earlier
        # normalization. Never adopt.
        return None
    # A single trailing slash is cosmetic -- "https://api.plaud.ai/" is the same
    # base URL, and an older tokens.enc may hold one.
    if host.endswith("/"):
        host = host[:-1]
    # Anything else after the authority: a path, query or fragment in a "host"
    # is not a base URL, and concatenating f"{api}{path}" onto one is nonsense.
    for sep in ("/", "?", "#"):
        if sep in host:
            return None
    # userinfo@ is the exfiltration primitive: "api.plaud.ai@evil.example" has
    # authority evil.example while reading as ours to a suffix check.
    if "@" in host:
        return None
    # :443 is the default for https and so is cosmetic; any other port is a
    # host we do not talk to.
    if ":" in host:
        host, _, port = host.partition(":")
        if port != "443":
            return None
    host = host.lower().rstrip(".")
    if not host:
        return None
    # Fold to IDNA before the allowlist sees it. .lower() is codepoint-wise, so
    # a Cyrillic a in "\u0430pi.plaud.ai" survives it and then passes an
    # endswith(".plaud.ai") check while resolving to xn--pi-6kc.plaud.ai -- a
    # different host entirely. Encoding first makes the comparison operate on
    # what DNS will actually resolve. Pure-ASCII hosts are unchanged.
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None
    return f"https://{host}"


def _is_plaud_host(url):
    """True if a normalized URL points at Plaud's own domain."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    host = url[8:].partition(":")[0]
    # Reject internationalized labels outright. Plaud has no IDN hosts, so an
    # xn-- label under plaud.ai can only come from a response trying to look
    # like one that isn't -- "\u0430pi.plaud.ai" (Cyrillic a) folds to
    # xn--pi-6kc.plaud.ai, which really is under plaud.ai and so passes the
    # suffix test, while reading as "api.plaud.ai" to a human in a log.
    if "xn--" in host:
        return False
    return host == API_HOST_EXACT or host.endswith(API_HOST_SUFFIX)


def _redirect_host(resp):
    """Pull the regional API host out of a `status: -302` response.

    The backend answers -302 when the client is talking to the wrong regional
    host, and carries the right one at `data.domains.api`. Returns a normalized
    `https://host` string, or None if this is not a usable redirect.

    Tolerant about shape in the same way as _extract/_workspaces, and for the
    same reason: the -302 shape has never been seen on the wire from here, so
    `data` may or may not be a wrapper and the host may or may not arrive
    scheme-qualified. Strict about the host itself -- see API_HOST_SUFFIX.
    """
    if not isinstance(resp, dict) or resp.get("status") != -302:
        return None
    data = resp.get("data", resp)
    if not isinstance(data, dict):
        return None
    domains = data.get("domains", data)
    if not isinstance(domains, dict):
        return None
    host = _normalize_host(domains.get("api"))
    if not host or not _is_plaud_host(host):
        return None
    return host


def _extract(d, keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k]:
            return d[k]
    if "data" in d and isinstance(d["data"], dict):
        return _extract(d["data"], keys)
    return None


if __name__ == "__main__":
    c = PlaudClient()
    print("logged in:", c.is_logged_in())
    print("device:", c.device)
    print("api:", c.api)
    if c.tokens:
        print("have tokens:", {k: ("…" if v else None) for k, v in c.tokens.items()})
