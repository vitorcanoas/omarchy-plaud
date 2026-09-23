#!/usr/bin/env python3
"""The workspace record's api_domain must go through the same allowlist as -302.

login_with_auth_code() adopted `ws0["api_domain"]` straight into self.api with
no validation, and _mint_wt() then sent the UT as a bearer token to whatever
host that named. The value arrives in a response body, so it is exactly as
attacker-controlled as the -302 payload the allowlist was written for -- and
this path had no allowlist at all.

Every scenario runs in a CHILD PROCESS. plaud_api caches module state and the
stubbed requests handlers bind to one module instance, so reloading in-process
silently leaks stubs between scenarios and makes these checks pass whether the
defect is present or not. That mistake was made once while writing this file.

Run standalone:  python3 tests/test_wshost.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}")
        if detail:
            print(f"  {detail}")


PROBE = '''
import os, sys, tempfile, json
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, {root!r})
from plaud_linux import plaud_api as api

DOMAIN = {domain!r}
sent = []

def post(url, **kw):
    sent.append(url)
    class R:
        status_code = 200
        @staticmethod
        def json():
            if "auth" in url and "workspace" not in url:
                return {{"status": 0, "data": {{
                    "access_token": "bearer UT_SECRET",
                    "ut_expire_at": 9999999999}}}}
            if "workspace/token" in url:
                return {{"status": 0, "data": {{
                    "access_token": "WT", "refresh_token": "WRT",
                    "expire_at": 9999999999,
                    "refresh_expire_at": 9999999999}}}}
            return {{"status": 0, "data": {{}}}}
        text = ""
    return R()

def get(url, **kw):
    sent.append(url)
    class R:
        status_code = 200
        @staticmethod
        def json():
            return {{"status": 0, "data": {{"workspaces": [
                {{"workspace_id": "ws9", "status": "active",
                  "api_domain": DOMAIN}}]}}}}
        text = ""
    return R()

api.requests.post = post
api.requests.get = get
c = api.PlaudClient()
try:
    c.login_with_auth_code("code123")
except Exception:
    pass
print(json.dumps({{
    "api": c.api,
    "sent": sent,
    "saved": api._load_tokens().get("api_domain"),
    "is_plaud": api._is_plaud_host(c.api),
}}))
'''


def login_with(domain):
    """Run a full login whose workspace record names `domain`."""
    out = subprocess.run(
        [sys.executable, "-c", PROBE.format(root=ROOT, domain=domain)],
        capture_output=True, text=True).stdout.strip().splitlines()
    import json
    return json.loads(out[-1])


def run():
    EVIL = "https://evil.example.com"
    r = login_with(EVIL)

    check("1 a hostile api_domain is not adopted",
          r["api"] != EVIL,
          f"self.api={r['api']!r} -- the client is now talking to the attacker")

    check("2 the api host stays on a Plaud domain",
          r["is_plaud"],
          f"self.api={r['api']!r}")

    leaked = [u for u in r["sent"] if "evil.example.com" in u]
    check("3 the UT is not sent to the attacker's host",
          not leaked,
          f"leaked {len(leaked)} request(s): {leaked[:3]}")

    check("4 the attacker's host is not persisted to tokens.enc",
          r["saved"] != EVIL,
          f"api_domain={r['saved']!r} -- it would survive a restart")

    # A legitimate regional host on the same field must still be honored, or
    # the fix would break real multi-region accounts.
    g = login_with("https://eu.api.plaud.ai")
    check("5 a legitimate regional host is still adopted",
          g["api"] == "https://eu.api.plaud.ai",
          f"self.api={g['api']!r} -- real multi-region accounts would break")

    # Cyrillic a: folds to xn--pi-6kc.plaud.ai, which really is under
    # plaud.ai and so passes a suffix test, while reading as api.plaud.ai.
    h = login_with("https://аpi.plaud.ai")
    check("6 an IDN homograph under plaud.ai is rejected",
          h["api"] == "https://api.plaud.ai",
          f"self.api={h['api']!r}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
