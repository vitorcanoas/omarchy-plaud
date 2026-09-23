"""Checks for the dedicated generation-picker window (custom generation).

Usage: python3 tests/test_generation_web.py [repo]

Windows opens the official picker from Plaud Web; the Linux port used to open
the whole note page in a browser tab, with the user's account behind the
dialog. generation_web.py shows that same official page in its own WebKit
process and hides everything but the picker. These checks cover the contract
that keeps that safe: only the exact note URL is accepted, no token travels
through argv, the presentation layer never issues requests or clicks
"Generate now" on its own, and every failure falls back to the browser tab.
"""
import os, pathlib, subprocess, sys, tempfile, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp(prefix="genweb-")
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gio

from plaud_linux import generation_web
import plaud_linux.main as main_mod

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")

GOOD = main_mod._web_note_url("file_ABC", "ws_TEST123", "enc-uuid")

# ---------------------------------------------------------------- CHECK 1
check("1 the URL main.py builds for 'Gerar personalizada' is accepted",
      generation_web.valid_url(GOOD), GOOD)

# ---------------------------------------------------------------- CHECK 2
bad = ["http://web.plaud.ai/file/x?transcribeDialog=custom",
       "https://evil.example/file/x?transcribeDialog=custom",
       "https://web.plaud.ai/file/x", "https://web.plaud.ai/file/?transcribeDialog=custom",
       "https://web.plaud.ai/login?transcribeDialog=custom", "", None, 42]
check("2 anything but the note URL with the custom dialog is rejected",
      not any(generation_web.valid_url(u) for u in bad), repr(bad))

# ---------------------------------------------------------------- CHECK 3
spawned = []
class FakeProc:
    pass
real_popen = subprocess.Popen
subprocess.Popen = lambda args, **kw: spawned.append((args, kw)) or FakeProc()
try:
    generation_web.launch(GOOD)
finally:
    subprocess.Popen = real_popen
args, kw = spawned[0]
check("3 launch() starts a separate python process with only the URL as argument",
      len(spawned) == 1 and args[0] == sys.executable
      and args[1].endswith("generation_web.py") and args[2] == GOOD and len(args) == 3
      and kw.get("start_new_session") and kw.get("stdin") == subprocess.DEVNULL,
      repr(spawned))

# ---------------------------------------------------------------- CHECK 4
try:
    generation_web.launch("https://web.plaud.ai/")
    raised = False
except ValueError:
    raised = True
check("4 launch() refuses an invalid link instead of spawning anything", raised)

# ---------------------------------------------------------------- CHECK 5
script = generation_web.bootstrap_script("Bearer abc.def")
check("5 the web session receives the bearer token once, on web.plaud.ai only",
      'localStorage.setItem("pld_tokenstr"' in script and '"Bearer abc.def"' in script
      and "Bearer Bearer" not in script and 'https://web.plaud.ai' in script
      and 'pathname.startsWith("/file/")' in script,
      script)

# ---------------------------------------------------------------- CHECK 6
script2 = generation_web.bootstrap_script("raw-token")
check("6 a raw token is prefixed with Bearer and no refresh token is mentioned",
      '"Bearer raw-token"' in script2 and "wrt" not in script2 and "wt" not in script2.replace("wt_", ""),
      script2)

# ---------------------------------------------------------------- CHECK 6b
# The page reuses a stored, unexpired workspace token instead of minting one
# (which revokes the recorder's). The injected keys follow the web bundle's
# "pld_<userId>:" user scope with JSON values and millisecond expiry; the
# refresh token never travels, and an almost-expired or incomplete session
# injects nothing so the page behaves as before.
import base64, json as _json
_seg = base64.urlsafe_b64encode(_json.dumps({"sub": "user1"}).encode()).rstrip(b"=").decode()
UT = "hdr." + _seg + ".sig"
TOK = {"ut": UT, "wt": "WT-VALUE", "wrt": "WRT-SECRET", "ws_id": "ws_TEST123", "wt_exp": 100_000}
session = generation_web.web_session(TOK, "https://api.plaud.ai/", now=10_000)
script3 = generation_web.bootstrap_script(UT, session)
stored = {}
for key, val in __import__("re").findall(r'localStorage\.setItem\(("[^"]*"),(.*?)\);', script3):
    stored[_json.loads(key)] = val
wsl = _json.loads(_json.loads(stored.get("pld_user1:workspaceList", '"[]"')))
check("6b the recorder's workspace token is stored where the web app looks for it, without the refresh token",
      session == {"user": "user1", "ws_id": "ws_TEST123", "wt": "WT-VALUE",
                  "expires_at_ms": 100_000_000, "domain": "https://api.plaud.ai"}
      and _json.loads(_json.loads(stored["pld_user1:currentWorkspaceId"])) == "ws_TEST123"
      and wsl == [{"workspaceId": "ws_TEST123", "domain": "https://api.plaud.ai",
                   "workspaceToken": "WT-VALUE", "expiresAt": 100_000_000}]
      and "WRT-SECRET" not in script3 and "refreshToken" not in script3
      and script3.startswith('if(location.origin==="https://web.plaud.ai"&&location.pathname.startsWith("/file/")){')
      and generation_web.web_session(TOK, "https://api.plaud.ai", now=99_800) is None
      and generation_web.web_session({**TOK, "ut": "not-a-jwt"}, "https://api.plaud.ai", now=10_000) is None
      and generation_web.web_session({**TOK, "wt": None}, "https://api.plaud.ai", now=10_000) is None
      and generation_web.web_session(TOK, "http://api.plaud.ai", now=10_000) is None
      and generation_web.bootstrap_script(UT, None) == generation_web.bootstrap_script(UT),
      script3)

# ---------------------------------------------------------------- CHECK 7
js = (pathlib.Path(repo) / "plaud_linux/web/generation.js").read_text()
check("7 the page adapter only observes: no request of its own, no synthetic click",
      ".click(" not in js and "dispatchEvent" not in js and "new XMLHttpRequest" not in js
      and "nativeFetch.apply(this, arguments)" in js and "open.apply(this, arguments)" in js
      and "'/ai/transsumm/'" in js and "send('generating')" in js and "send('sent')" in js
      and "loadend" in js and "result.finally" in js
      and "postMessage" in js and "closed" in js and "send('page')" in js, "")

# ---------------------------------------------------------------- CHECK 7b
src = (pathlib.Path(repo) / "plaud_linux/generation_web.py").read_text()
check("7b closed hides the window but keeps the page alive for a late request; "
      "external links open only on user gestures",
      "window.hide()" in src and "timeout_add_seconds(6, close)" in src
      and "elif name == 'sent':" in src
      and "action.is_user_gesture()" in src and "NavigationType.LINK_CLICKED" in src, "")

# ---------------------------------------------------------------- CHECK 8
css = (pathlib.Path(repo) / "plaud_linux/web/generation.css").read_text()
check("8 the stylesheet hides the note page and keeps every picker layer visible",
      "body * { visibility: hidden !important; }" in css
      and ".generate-options-dialog" in css and ".templates-selector-dialog" in css
      and ".el-popper" in css and "inset: auto !important" in css, "")

# ---------------------------------------------------------------- CHECK 8a
check("8a the audio-language popover is pinned inside the window (it opens "
      "right-end of its row and popper leaves it off-screen in a card-wide window)",
      ".language-selector-popover.el-popper" in css
      and "left: auto !important; right: 16px !important;" in css
      and ".language-selector-popover .el-popper__arrow { display: none !important; }" in css, "")

# ---------------------------------------------------------------- CHECK 8b
check("8b messages carry the card size and unknown states are dropped",
      generation_web.parse_message("ready:464:477") == ("ready", 464, 477)
      and generation_web.parse_message("templates:893:900") == ("templates", 893, 900)
      and generation_web.parse_message("generating") == ("generating", 0, 0)
      and generation_web.parse_message("sent") == ("sent", 0, 0)
      and generation_web.parse_message("closed") == ("closed", 0, 0)
      and generation_web.parse_message("ready:x:y") == (None, 0, 0)
      and generation_web.parse_message("evil") == (None, 0, 0)
      and generation_web.parse_message(None) == (None, 0, 0))

# ---------------------------------------------------------------- CHECK 8c
check("8c the window is the card plus margins, bounded by the work area",
      generation_web.fit_size(464, 477, (1920, 1080), (496, 474)) == (496, 509)
      and generation_web.fit_size(893, 1400, (1920, 1080), (925, 760)) == (925, 1032)
      and generation_web.fit_size(0, 0, (1920, 1080), (496, 474)) == (496, 474))

# ---------------------------------------------------------------- CHECK 9
# A missing webkit2gtk must not strand the recording: the child opens the
# browser tab the previous release used and reports that it did so.
launched = []
real_require = gi.require_version
def fake_require(ns, ver):
    if ns == "WebKit2":
        raise ValueError("Namespace WebKit2 not available")
    return real_require(ns, ver)
gi.require_version = fake_require
real_launch = Gio.AppInfo.launch_default_for_uri
Gio.AppInfo.launch_default_for_uri = staticmethod(lambda uri, ctx: launched.append(uri) or True)
try:
    rc = generation_web.run(GOOD)
finally:
    gi.require_version = real_require
    Gio.AppInfo.launch_default_for_uri = staticmethod(real_launch)
check("9 without WebKit the child falls back to the browser tab and exits 1",
      rc == 1 and launched == [GOOD], f"rc={rc} launched={launched!r}")

# ---------------------------------------------------------------- CHECK 10
check("10 run() with an invalid link exits 2 without opening anything",
      generation_web.run("https://web.plaud.ai/") == 2 and launched == [GOOD])

# ---------------------------------------------------------------- CHECK 11
class Child:
    def __init__(self, code):
        self.code = code
    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("child", timeout)
        return self.code

opened, launches = [], []
orig = (main_mod._open_url, main_mod.generation_web.launch)
main_mod._open_url = lambda url: opened.append(url)
main_mod.generation_web.launch = lambda url: launches.append(url) or Child(None)
try:
    main_mod._open_generation(GOOD)
    ok11 = launches == [GOOD] and opened == []
    main_mod.generation_web.launch = lambda url: Child(1)
    main_mod._open_generation(GOOD)
    ok11b = opened == []
    main_mod.generation_web.launch = lambda url: (_ for _ in ()).throw(OSError("no python"))
    main_mod._open_generation(GOOD)
    ok12 = opened == [GOOD]
    main_mod.generation_web.launch = lambda url: Child(3)
    main_mod._open_generation(GOOD)
    ok12b = opened == [GOOD, GOOD]
    main_mod.generation_web.launch = lambda url: Child(4)
    main_mod._open_generation(GOOD)
    ok12c = opened == [GOOD, GOOD]
finally:
    main_mod._open_url, main_mod.generation_web.launch = orig
check("11 'Gerar personalizada' opens the dedicated window, not a browser tab", ok11 and ok11b,
      f"launches={launches!r} opened={opened!r}")

# ---------------------------------------------------------------- CHECK 12
check("12 a child that cannot start, or exits without opening anything, falls back to the browser tab; "
      "one closed without a request (4) does not", ok12 and ok12b and ok12c, repr(opened))

# ---------------------------------------------------------------- CHECK 13
src = (pathlib.Path(repo) / "plaud_linux/main.py").read_text()
check("13 both custom hand-offs (upload and resend) go through _open_generation, with the client and file id",
      src.count("                    _open_generation(url, client, fid)\n") == 2
      and "                    _open_url(url)\n" not in src)

# ---------------------------------------------------------------- CHECK 14
# A picker still up after 2 s is waited for on its own worker, never on the
# upload worker (which returns at once) and never on GTK; its exit status 0
# means the official page issued the generation request, and the monitor is
# started from the GTK thread through GLib.idle_add with the pending answer
# the automatic path polls on.
import threading, time
class LateChild:
    """wait(timeout=2) times out; wait() blocks until exit() is called."""
    def __init__(self):
        self.done = threading.Event()
        self.code = None
        self.waits = []
    def exit(self, code):
        self.code = code
        self.done.set()
    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self.done.wait(timeout if timeout is not None else 30):
            raise subprocess.TimeoutExpired("child", timeout)
        return self.code

hops, monitored, opened = [], [], []
class FakeGLib:
    @staticmethod
    def idle_add(cb, *a):
        hops.append(threading.current_thread().name)
        return cb(*a)
orig = (main_mod._open_url, main_mod.generation_web.launch, main_mod.GLib,
        main_mod._generation_requested)
main_mod._open_url = lambda url: opened.append(url)
main_mod.GLib = FakeGLib
main_mod._generation_requested = lambda c, f, r: monitored.append((c, f, r))
try:
    late = LateChild()
    main_mod.generation_web.launch = lambda url: late
    t0 = time.monotonic()
    main_mod._open_generation(GOOD, "CLIENT", "file_ABC")
    returned_in = time.monotonic() - t0
    waiting = [t for t in threading.enumerate() if t.daemon and t.name.startswith("Thread")]
    before = list(monitored)
    late.exit(0)
    for t in waiting:
        t.join(timeout=10)
    ok14 = (late.waits == [2, None] and returned_in < 5 and before == []
            and monitored == [("CLIENT", "file_ABC", {"status": 0})]
            and hops and all(h != "MainThread" for h in hops) and opened == [])
    detail14 = f"waits={late.waits} returned_in={returned_in:.1f}s monitored={monitored} hops={hops}"

    # ------------------------------------------------------------ CHECK 15
    monitored.clear(); hops.clear()
    late = LateChild()
    main_mod.generation_web.launch = lambda url: late
    main_mod._open_generation(GOOD, "CLIENT", "file_ABC")
    late.exit(4)
    for t in [t for t in threading.enumerate() if t.daemon and t.name.startswith("Thread")]:
        t.join(timeout=10)
    none_on_cancel = monitored == [] and opened == []
    main_mod.generation_web.launch = lambda url: Child(0)
    main_mod._open_generation(GOOD, "CLIENT", "file_ABC")
    early_zero = monitored == [("CLIENT", "file_ABC", {"status": 0})]
    monitored.clear()
    main_mod._open_generation(GOOD)                     # no client / file id
    main_mod._open_generation(GOOD, "CLIENT", None)
    main_mod.generation_web.launch = lambda url: Child(4)
    main_mod._open_generation(GOOD, "CLIENT", "file_ABC")
    main_mod.generation_web.launch = lambda url: Child(1)
    main_mod._open_generation(GOOD, "CLIENT", "file_ABC")
    nothing_else = monitored == [] and opened == []
finally:
    (main_mod._open_url, main_mod.generation_web.launch, main_mod.GLib,
     main_mod._generation_requested) = orig
check("14 a picker still up after 2 s is awaited on a worker; exit 0 starts the monitor via idle_add with the file id",
      ok14, detail14)
check("15 exit 4 (closed without a request), no file id, or the browser fallback (1) start no monitor and open no tab",
      none_on_cancel and early_zero and nothing_else, f"monitored={monitored} opened={opened}")

# ---------------------------------------------------------------- CHECK 16
# The path after that hop is exactly the automatic one: _generation_requested
# polls wait_for_generation(file_id, {"status": 0}) on a worker and, on
# status 1, reaches _generation_complete on the GTK thread (the "Sua nota
# está pronta" / "Ver as notas" notice); a failure is reported as
# "Áudio enviado. …" and nothing else.
from gi.repository import Gtk
class PollClient:
    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result, error
    def wait_for_generation(self, file_id, initial):
        self.calls.append((file_id, initial))
        if self.error:
            raise self.error
        return self.result
def drain():
    for t in [t for t in threading.enumerate() if t.daemon and t.name.startswith("Thread")]:
        t.join(timeout=10)
    for _ in range(200):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)
completed, said = [], []
orig = (main_mod._generation_complete, main_mod.notify, main_mod._desktop_progress)
main_mod._generation_complete = lambda fid: completed.append(fid) or False
main_mod.notify = lambda t, b, **k: said.append(b)
main_mod._desktop_progress = lambda body: None
try:
    good = PollClient(result={"status": 1})
    main_mod._custom_generation_requested(good, "file_ABC")
    drain()
    ok16 = good.calls == [("file_ABC", {"status": 0})] and completed == ["file_ABC"] and said == []
    bad = PollClient(error=TimeoutError("O Plaud ainda está processando."))
    main_mod._custom_generation_requested(bad, "file_ABC")
    drain()
    ok16b = completed == ["file_ABC"] and said == ["Áudio enviado. O Plaud ainda está processando."]
    main_mod._custom_generation_requested(PollClient(result={"status": 1}), None)
    main_mod._custom_generation_requested(None, "file_ABC")
    drain()
    ok16c = completed == ["file_ABC"] and len(said) == 1
finally:
    main_mod._generation_complete, main_mod.notify, main_mod._desktop_progress = orig
check("16 the monitor polls the pending answer until status 1 and reaches the completion notice; a failure is only reported",
      ok16 and ok16b and ok16c, f"calls={good.calls} completed={completed} said={said}")

sys.exit(0 if all(results) else 1)
