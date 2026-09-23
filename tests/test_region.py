"""Check the regional -302 redirect (chore/api-region-routing).

Usage: python3 tests/test_region.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

The backend answers `status: -302` when the client is talking to the wrong
regional host and names the right one at `data.domains.api`. Hardcoding
api.plaud.ai means every user outside the default region fails at login and
at every business call, with an error that names no cause.

Everything here runs against a stubbed `requests`: the pure shape parser needs
no I/O at all, and the retry checks drive PlaudClient with a fake transport so
nothing touches the network or the user's real tokens.

<!-- inferred, not verified: the -302 shape itself. It is reported
     consistently by four independent third-party clients but has never been
     seen on the wire from here, so these checks pin OUR handling of the
     documented shape, not the shape. -->
"""
import os, sys, json, tempfile, pathlib, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="regionchk-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

# Stub requests before plaud_api imports it: these checks must never open a
# socket, and the user's tokens are dead anyway.
stub = types.ModuleType("requests")
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sent = []


class _Resp:
    def __init__(self, payload, code=200):
        self._payload = payload
        self.status_code = code
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        if isinstance(self._payload, (dict, list)):
            return self._payload
        raise ValueError("not json")


QUEUE = []


def _record(method, url, **kw):
    sent.append((method, url))
    return _Resp(QUEUE.pop(0) if QUEUE else {"status": 0, "data": {}})


stub.request = _record
stub.post = lambda url, **kw: _record("POST", url, **kw)
stub.get = lambda url, **kw: _record("GET", url, **kw)
sys.modules["requests"] = stub

from plaud_linux import plaud_api

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# ---------------------------------------------------------------- pure parser
rh = getattr(plaud_api, "_redirect_host", None)
check("0 a -302 host parser exists",
      callable(rh), "(plaud_api._redirect_host)" if rh else "(missing)")
if not callable(rh):
    print()
    print("SOME FAILED")
    sys.exit(1)

check("1 reads data.domains.api",
      rh({"status": -302, "data": {"domains": {"api": "https://api-euc1.plaud.ai"}}})
      == "https://api-euc1.plaud.ai")

check("2 adds a scheme when the host arrives bare",
      rh({"status": -302, "data": {"domains": {"api": "api-apse1.plaud.ai"}}})
      == "https://api-apse1.plaud.ai")

# Tolerant like _extract/_workspaces: the wrapper may be absent.
check("3 tolerates an unwrapped payload",
      rh({"status": -302, "domains": {"api": "https://api-euc1.plaud.ai"}})
      == "https://api-euc1.plaud.ai")

check("4 ignores a non--302 response",
      rh({"status": 0, "data": {"domains": {"api": "https://evil.example"}}}) is None)

check("5 survives junk without raising",
      all(rh(x) is None for x in [
          None, {}, [], "nope", {"status": -302},
          {"status": -302, "data": None},
          {"status": -302, "data": {"domains": None}},
          {"status": -302, "data": {"domains": {"api": ""}}},
          {"status": -302, "data": {"domains": {"api": 5}}},
      ]))


# ---------------------------------------------------------------- _biz retry
def fresh_client(tokens=None):
    c = plaud_api.PlaudClient.__new__(plaud_api.PlaudClient)
    c.api = plaud_api.DEFAULT_API
    c.tokens = dict(tokens or {})
    c.device = "test-device-uuid"
    if c.tokens.get("api_domain"):
        c.api = c.tokens["api_domain"]
    return c


EU = "https://api-euc1.plaud.ai"

sent.clear()
QUEUE[:] = [{"status": -302, "data": {"domains": {"api": EU}}},
            {"status": 0, "data": {"ok": True}}]
c = fresh_client({"wt": "w", "ws_id": "ws1", "api_domain": plaud_api.DEFAULT_API})
out = c._biz("POST", "/file/get_upload_presigned_url", json={})

check("6 -302 is retried against the named host",
      len(sent) == 2 and sent[0][1].startswith(plaud_api.DEFAULT_API)
      and sent[1][1] == EU + "/file/get_upload_presigned_url",
      f"({[u for _, u in sent]})")

check("7 the retry's result is what the caller gets",
      out == {"status": 0, "data": {"ok": True}}, f"({out})")

check("8 the adopted host sticks on the client",
      c.api == EU, f"({c.api})")

check("9 the adopted host is persisted for the next launch",
      plaud_api._load_tokens().get("api_domain") == EU,
      f"({plaud_api._load_tokens().get('api_domain')})")

# A backend that answers -302 naming the host you already use would loop.
sent.clear()
QUEUE[:] = [{"status": -302, "data": {"domains": {"api": EU}}},
            {"status": -302, "data": {"domains": {"api": EU}}}]
c2 = fresh_client({"wt": "w", "ws_id": "ws1", "api_domain": EU})
out2 = c2._biz("POST", "/file/confirm_upload", json={})
check("10 a -302 naming the current host does not loop",
      len(sent) == 1, f"({len(sent)} sends)")

# -302 must not be mistaken for a token problem: refreshing spends a WRT
# rotation against a host that will refuse it again.
sent.clear()
QUEUE[:] = [{"status": -302, "data": {"domains": {"api": EU}}},
            {"status": 0, "data": {}}]
c3 = fresh_client({"wt": "w", "ws_id": "ws1", "api_domain": plaud_api.DEFAULT_API})
refreshed = []
c3._refresh_wt = lambda **kw: refreshed.append(kw) or True
c3._biz("POST", "/file/merge_multipart", json={})
check("11 a -302 does not trigger a token refresh",
      not refreshed, f"({refreshed})")

# A non-JSON body on the redirect retry must degrade, not raise.
sent.clear()
QUEUE[:] = [{"status": -302, "data": {"domains": {"api": EU}}}, "<html>502</html>"]
c4 = fresh_client({"wt": "w", "ws_id": "ws1", "api_domain": plaud_api.DEFAULT_API})
try:
    out4 = c4._biz("POST", "/file/confirm_upload", json={})
    ok4 = out4.get("_status") == 200 and "_raw" in out4
except Exception as e:
    ok4 = False
    out4 = f"{type(e).__name__}: {e}"
check("12 a non-JSON body on the redirect retry degrades", ok4, f"({out4})")


# ---------------------------------------------------------------- login flow
# The pre-login case is the one that matters: tokens.enc is empty, so there is
# no stored api_domain, and an EU user meets -302 on the very first call.
sent.clear()
QUEUE[:] = [
    {"status": -302, "data": {"domains": {"api": EU}}},          # login POST
    {"status": 0, "data": {"access_token": "bearer UT1"}},       # login retry
    {"status": 0, "data": {"workspaces": [{"workspace_id": "ws9"}]}},
    {"status": 0, "data": {"workspace_token": "WT1", "refresh_token": "WRT1",
                           "expires_in": 3600}},
]
os.remove(plaud_api.TOKENS_FILE) if plaud_api.TOKENS_FILE.exists() else None
c5 = fresh_client()
c5.login_with_auth_code("single-use-code")

check("13 login follows -302 before any token exists",
      len(sent) == 4 and sent[1][1] == EU + "/auth/access-token-auth-code",
      f"({[u for _, u in sent][:2]})")

# The auth_code is single-use: a retry that re-fetched one would fail.
check("14 the login retry reuses the same auth_code",
      c5.tokens.get("ut") == "UT1", f"({c5.tokens.get('ut')})")

check("15 the region discovered at login is persisted",
      plaud_api._load_tokens().get("api_domain") == EU,
      f"({plaud_api._load_tokens().get('api_domain')})")

check("16 a new client starts on the stored regional host",
      fresh_client(plaud_api._load_tokens()).api == EU)


# ------------------------------------------------- HIGH-1: login lockout
# The auth_code is single-use. If the backend burns it and THEN answers -302,
# the retry fails and login raises -- but the REGION must survive, or every
# later browser re-login restarts on the default host and burns a fresh code
# the same way. Measured before the fix: three re-login cycles, three codes
# burned, tokens.enc empty, user permanently locked out.
def wipe_tokens():
    if plaud_api.TOKENS_FILE.exists():
        os.remove(plaud_api.TOKENS_FILE)


wipe_tokens()
sent.clear()
QUEUE[:] = [
    {"status": -302, "data": {"domains": {"api": EU}}},   # code burned, then redirect
    {"status": -1, "msg": "auth_code already used"},      # retry: code is spent
]
c6 = fresh_client()
try:
    c6.login_with_auth_code("burned-code")
    raised = False
except RuntimeError:
    raised = True

check("17 a burnt-then-redirected login still raises", raised)

check("18 the region survives a login that burned its auth_code",
      plaud_api._load_tokens().get("api_domain") == EU,
      f"({plaud_api._load_tokens().get('api_domain')})")

# The point of persisting it: the NEXT login attempt must start on the
# regional host, not back on the default one.
sent.clear()
QUEUE[:] = [
    {"status": 0, "data": {"access_token": "bearer UT2"}},
    {"status": 0, "data": {"workspaces": [{"workspace_id": "ws9"}]}},
    {"status": 0, "data": {"workspace_token": "WT2", "refresh_token": "WRT2",
                           "expires_in": 3600}},
]
c7 = fresh_client(plaud_api._load_tokens())
try:
    c7.login_with_auth_code("fresh-code")
except Exception:
    pass
check("19 the next login starts on the remembered region, burning no extra code",
      sent and sent[0][1] == EU + "/auth/access-token-auth-code" and len(sent) == 3,
      f"({sent[0][1] if sent else None}, {len(sent)} sends)")

# A tokens.enc holding only api_domain must not look like a session.
wipe_tokens()
c8 = fresh_client()
c8._adopt_region({"status": -302, "data": {"domains": {"api": EU}}})
check("20 a region-only token file still reads as logged out",
      not fresh_client(plaud_api._load_tokens()).is_logged_in())


# ------------------------------------------- HIGH-2: host allowlist
# A -302 names the host that will then receive Authorization: Bearer <WT>.
# Nothing re-validates a stored api_domain, so one hostile response would
# otherwise redirect this client persistently.
HOSTILE = [
    ("userinfo exfiltration", "https://api.plaud.ai@evil.example.com"),
    ("bare evil host", "https://evil.example.com"),
    ("suffix lookalike", "https://plaud.ai.evil.com"),
    ("http downgrade", "http://api.plaud.ai"),
    ("raw IP", "https://203.0.113.9"),
    ("embedded path", "https://evil.example.com/api.plaud.ai"),
    ("path on a real host", "https://api.plaud.ai/../evil"),
    ("javascript scheme", "javascript:alert(1)"),
    ("CRLF header injection", "api.plaud.ai\r\nX-Evil: 1"),
    ("newline", "api.plaud.ai\nevil"),
    ("odd port", "https://api.plaud.ai:8080"),
    ("space", "api.plaud.ai evil.com"),
]
bad = [n for n, h in HOSTILE
       if rh({"status": -302, "data": {"domains": {"api": h}}}) is not None]
check("21 every hostile redirect host is refused", not bad, f"(accepted: {bad})")

# and refusing must also mean not persisting, and not retrying
wipe_tokens()
sent.clear()
QUEUE[:] = [{"status": -302, "data": {"domains": {"api": "https://evil.example.com"}}}]
c9 = fresh_client({"wt": "w", "ws_id": "ws1", "api_domain": plaud_api.DEFAULT_API})
out9 = c9._biz("POST", "/file/confirm_upload", json={})
check("22 a refused host is neither followed nor persisted",
      len(sent) == 1 and c9.api == plaud_api.DEFAULT_API
      and plaud_api._load_tokens().get("api_domain") != "https://evil.example.com",
      f"({len(sent)} sends, api={c9.api})")

# legitimate regional hosts must still pass
good = [h for h in ["https://api-euc1.plaud.ai", "https://api-apse1.plaud.ai",
                    "api.plaud.ai", "https://plaud.ai"]
        if rh({"status": -302, "data": {"domains": {"api": h}}}) is None]
check("23 real regional hosts are still accepted", not good, f"(refused: {good})")


# ------------------------------------------- MED-3: all call sites follow -302
sent.clear()
QUEUE[:] = [
    {"status": -302, "data": {"domains": {"api": EU}}},
    {"status": 0, "data": {"workspace_token": "WT3", "refresh_token": "WRT3",
                           "expires_in": 3600}},
]
c10 = fresh_client({"ut": "UT", "api_domain": plaud_api.DEFAULT_API})
# Raises on a pre-fix tree ("mint WT failed: {...domains...}"), which is the
# defect itself -- catch it so the remaining checks still run and report.
try:
    c10._mint_wt("ws5")
    err10 = None
except Exception as e:
    err10 = f"{type(e).__name__}: {str(e)[:60]}"
check("24 _mint_wt follows -302 instead of aborting a burnt login",
      err10 is None and c10.tokens.get("wt") == "WT3" and len(sent) == 2
      and sent[1][1].startswith(EU),
      f"(wt={c10.tokens.get('wt')}, {len(sent)} sends, {err10 or 'no raise'})")

sent.clear()
QUEUE[:] = [
    {"status": -302, "data": {"domains": {"api": EU}}},
    {"status": 0, "data": {"workspace_token": "WT4", "expires_in": 3600}},
]
c11 = fresh_client({"wt": "old", "wrt": "WRT", "ws_id": "ws5",
                    "api_domain": plaud_api.DEFAULT_API})
try:
    ok11 = c11._refresh_wt_locked()
    err11 = None
except Exception as e:
    ok11, err11 = None, f"{type(e).__name__}: {str(e)[:60]}"
check("25 _refresh_wt_locked follows -302 instead of reporting a dead session",
      ok11 is True and c11.tokens.get("wt") == "WT4" and len(sent) == 2,
      f"(returned {ok11}, wt={c11.tokens.get('wt')}, {len(sent)} sends"
      f"{', ' + err11 if err11 else ''})")


# ------------------------------------------- MED-4: cosmetic host variants
# These are all the host the client is ALREADY on. Adopting one re-writes
# tokens.enc on every call and, for the uppercase case, builds a double-scheme
# dead URL.
CURRENT = "https://api.plaud.ai"
same = []
for variant in ["HTTPS://API.PLAUD.AI", "https://API.plaud.ai", "api.plaud.ai",
                "https://api.plaud.ai.", "https://api.plaud.ai:443",
                "https://api.plaud.ai/", "  https://api.plaud.ai  "]:
    c12 = fresh_client({"wt": "w", "ws_id": "ws1", "api_domain": CURRENT})
    try:
        if c12._adopt_region({"status": -302, "data": {"domains": {"api": variant}}}):
            same.append((variant, c12.api))
    except Exception as e:
        same.append((variant, f"{type(e).__name__}"))
check("26 cosmetic variants of the current host are not re-adopted",
      not same, f"(adopted: {same})")

check("27 normalization never produces a double scheme",
      "://" not in (rh({"status": -302,
                        "data": {"domains": {"api": "HTTPS://API.PLAUD.AI"}}})
                    or "")[8:],
      f"({rh({'status': -302, 'data': {'domains': {'api': 'HTTPS://API.PLAUD.AI'}}})})")

# A genuinely different region, spelled oddly, must still be adopted once.
c13 = fresh_client({"wt": "w", "ws_id": "ws1", "api_domain": CURRENT})
try:
    adopted = c13._adopt_region({"status": -302,
                                 "data": {"domains": {"api": "API-EUC1.PLAUD.AI:443"}}})
except Exception:
    adopted = False
check("28 a real region change is still adopted, and normalized",
      adopted and c13.api == EU, f"({c13.api})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
