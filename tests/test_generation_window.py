"""Checks for the window side of generation_web.run(): the handlers it wires.

Usage: python3 tests/test_generation_window.py [repo]

test_generation_web.py covers the contract around the picker process (URL
validation, argv, the page adapter, browser fallback). This file drives the
closures run() connects -- script messages, navigation policy, load failures,
Escape, destroy -- without a display or a real WebKit: a stub `gi` tree is
injected into sys.modules before run() imports it, every connect() and timer
is recorded, and the recorded handlers are then called with fake objects.
The hyprctl subprocess is stubbed too, so the float/resize/center commands
the picker sends to Hyprland are asserted rather than executed.

plaud_api is replaced by a fake client with a fake UT/WT/WRT, so nothing
reads the user's session and the refresh token's absence from every
injected script can be asserted verbatim.
"""
import base64, json, os, pathlib, subprocess, sys, tempfile, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp(prefix="genwin-")
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

# ------------------------------------------------------------------ stub gi
# Everything run() touches on Gtk/Gdk/GLib/Gio/WebKit2, recording instead of
# rendering. `env` is reset by boot() so every scenario starts clean.
env = types.SimpleNamespace()

def reset_env():
    env.timers = {}        # id -> {"ms": int, "cb": callable, "removed": bool}
    env.removed = []
    env.next_id = 100
    env.launched = []
    env.hyprctl = []       # argv lists handed to subprocess.run
    env.main_quit = 0
    env.scripts = []
    env.sheets = []
    env.registered = []
    env.load_uri = []
    env.stop_loading = 0
    env.removed_scripts = 0
    env.prgname = None
    env.require = []
    env.expiry = None      # the session-expiry timer: long-lived, kept out of pending()
    env.loop = None        # what Gtk.main() does: a Boot(loop=...) script drives the messages
    env.managers = []
    env.fullscreen = 0     # what the stubbed `hyprctl clients -j` reports for the mapped window
    env.windows = 0        # Gtk.Window instances made so far (Boot counts them)
    env.rule_windows = None  # env.windows when the window rule was registered
reset_env()

def _add_timer(ms, cb):
    env.next_id += 1
    env.timers[env.next_id] = {"ms": ms, "cb": cb, "removed": False}
    return env.next_id

def pending():
    return {i: t for i, t in env.timers.items() if not t["removed"] and i != env.expiry}

def fire(tid):
    t = env.timers[tid]
    t["removed"] = True
    return t["cb"]()

class Signals:
    def __init__(self):
        self.handlers = {}
    def connect(self, name, cb, *a):
        self.handlers.setdefault(name, []).append(cb)
        return len(self.handlers[name])
    def emit(self, name, *args):
        out = None
        for cb in self.handlers.get(name, []):
            out = cb(self, *args)
        return out

class Widget(Signals):
    def __init__(self, **kw):
        super().__init__()
        self.visible = False
        self.no_show_all = False
        self.children = []
        self.text = kw.get("label", "")
        self.realized = False
        self.resizes = []
        self.destroyed = False
    def show(self): self.visible = True
    def hide(self): self.visible = False
    def get_visible(self): return self.visible
    def set_no_show_all(self, v): self.no_show_all = v
    def show_all(self):
        self.visible = True
        self.realized = True
        for c in self.children:
            if not c.no_show_all:
                c.show_all()
    def add(self, c): self.children.append(c)
    def pack_start(self, c, *a): self.children.append(c)
    def pack_end(self, c, *a): self.children.append(c)
    def set_margin_top(self, v): pass
    def set_margin_bottom(self, v): pass
    def set_border_width(self, v): pass
    def set_default_size(self, w, h): self.default = (w, h)
    def set_text(self, t): self.text = t
    def get_text(self): return self.text
    def get_style_context(self): return types.SimpleNamespace(add_provider=lambda *a: None)
    def get_realized(self): return self.realized
    def resize(self, w, h): self.resizes.append((w, h))
    def get_window(self): return object()
    def get_display(self):
        area = types.SimpleNamespace(width=1920, height=1080)
        monitor = types.SimpleNamespace(get_workarea=lambda: area)
        return types.SimpleNamespace(get_monitor_at_window=lambda w: monitor)
    def destroy(self):
        self.destroyed = True
        self.visible = False
        self.emit("destroy")

class CssProvider:
    def load_from_data(self, data): self.data = data

Gtk = types.SimpleNamespace(
    Window=Widget, Box=Widget, Label=Widget, Button=Widget, EventBox=Widget,
    CssProvider=CssProvider, Orientation=types.SimpleNamespace(VERTICAL=1, HORIZONTAL=0),
    STYLE_PROVIDER_PRIORITY_APPLICATION=600,
    main=lambda: env.loop() if env.loop else None,
    main_quit=lambda: setattr(env, "main_quit", env.main_quit + 1))
Gdk = types.SimpleNamespace(KEY_Escape=0xff1b, KEY_a=0x61)

def _source_remove(tid):
    env.removed.append(tid)
    env.timers[tid]["removed"] = True
    return True
GLib = types.SimpleNamespace(
    set_prgname=lambda n: setattr(env, "prgname", n),
    timeout_add=lambda ms, cb, *a: _add_timer(ms, cb),
    timeout_add_seconds=lambda s, cb, *a: _add_timer(s * 1000, cb),
    idle_add=lambda cb, *a: _add_timer(0, cb),
    source_remove=_source_remove)

class AppInfo:
    @staticmethod
    def launch_default_for_uri(uri, ctx):
        env.launched.append(uri)
        return True
Gio = types.SimpleNamespace(AppInfo=AppInfo)

class Manager(Signals):
    def __init__(self):
        super().__init__()
        env.managers.append(self)
    def register_script_message_handler(self, name): env.registered.append(name)
    def add_script(self, s): env.scripts.append(s)
    def add_style_sheet(self, s): env.sheets.append(s)
    def remove_all_scripts(self): env.removed_scripts += 1

class Context(Signals):
    pass

class View(Widget):
    def __init__(self, **kw):
        super().__init__()
        self.kw = kw
        self.settings = types.SimpleNamespace(set_enable_write_console_messages_to_stdout=lambda v: None)
    def get_settings(self): return self.settings
    def load_uri(self, uri): env.load_uri.append(uri)
    def stop_loading(self): env.stop_loading += 1

def _new(*args):
    return types.SimpleNamespace(source=args[0], frames=args[1], when=args[2],
                                 allow=args[3], block=args[4])
WebKit2 = types.SimpleNamespace(
    UserContentManager=Manager, WebContext=types.SimpleNamespace(new_ephemeral=Context),
    WebView=View, UserScript=types.SimpleNamespace(new=_new),
    UserStyleSheet=types.SimpleNamespace(new=_new),
    UserContentInjectedFrames=types.SimpleNamespace(TOP_FRAME=0),
    UserScriptInjectionTime=types.SimpleNamespace(START=0),
    UserStyleLevel=types.SimpleNamespace(USER=0),
    PolicyDecisionType=types.SimpleNamespace(NAVIGATION_ACTION=0, NEW_WINDOW_ACTION=1, RESPONSE=2),
    NavigationType=types.SimpleNamespace(LINK_CLICKED=0, OTHER=5),
    NetworkError=types.SimpleNamespace(CANCELLED=302),
    network_error_quark=lambda: "webkit-network-error-quark")

gi_mod = types.ModuleType("gi")
gi_mod.require_version = lambda ns, ver: env.require.append((ns, ver))
gi_mod.repository = types.ModuleType("gi.repository")
for name, mod in [("Gtk", Gtk), ("Gdk", Gdk), ("GLib", GLib), ("Gio", Gio), ("WebKit2", WebKit2)]:
    setattr(gi_mod.repository, name, mod)
sys.modules["gi"] = gi_mod
sys.modules["gi.repository"] = gi_mod.repository

# ----------------------------------------------------------- fake plaud_api
_seg = base64.urlsafe_b64encode(json.dumps({"sub": "user1"}).encode()).rstrip(b"=").decode()
UT = "hdr." + _seg + ".sig"
WRT = "WRT-SECRET-NEVER-INJECTED"
NOW = 1_700_000_000
FAR = NOW + 3600

class FakeClient:
    tokens = {}
    refreshed = 0
    def __init__(self):
        self.tokens = dict(FakeClient.tokens)
        self.api = "https://api.plaud.ai"
    def _refresh_wt(self):
        FakeClient.refreshed += 1
        self.tokens["wt"] = "WT-RENEWED"
        self.tokens["wt_exp"] = FAR

fake_api = types.ModuleType("plaud_linux.plaud_api")
fake_api.PlaudClient = FakeClient
sys.modules["plaud_linux.plaud_api"] = fake_api

import plaud_linux
plaud_linux.plaud_api = fake_api
from plaud_linux import generation_web

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")

URL = "https://web.plaud.ai/file/file_ABC?transcribeDialog=custom"
ADDRESS = "0x55d0c0ffee00"

# ------------------------------------------------------------- hyprctl stub
def fake_run(argv, **kw):
    env.hyprctl.append(list(argv))
    if argv[:2] == ["hyprctl", "repl"] and "window_rule" in argv[2]:
        env.rule_windows = env.windows
    if argv[:2] == ["hyprctl", "clients"]:
        clients = [{"pid": os.getpid(), "class": "plaud-generation", "mapped": True, "address": ADDRESS,
                    "fullscreen": env.fullscreen},
                   {"pid": os.getpid(), "class": "plaud-generation", "mapped": False, "address": "0x1"},
                   {"pid": 1, "class": "plaud-generation", "mapped": True, "address": "0x2"}]
        return types.SimpleNamespace(stdout=json.dumps(clients), returncode=0)
    return types.SimpleNamespace(stdout="", returncode=0)

real_subprocess = generation_web.subprocess
generation_web.subprocess = types.SimpleNamespace(
    run=fake_run, SubprocessError=subprocess.SubprocessError, Popen=None)
real_time_mod = generation_web.time
generation_web.time = types.SimpleNamespace(time=lambda: NOW)

def lua_calls():
    """Dispatch scripts, without the one-off window rule (see rule_calls)."""
    return [a[2] for a in env.hyprctl if a[:2] == ["hyprctl", "repl"] and "window_rule" not in a[2]]

def rule_calls():
    return [a[2] for a in env.hyprctl if a[:2] == ["hyprctl", "repl"] and "window_rule" in a[2]]

# ----------------------------------------------------------------- driver
class Msg:
    def __init__(self, text): self.text = text
    def get_js_value(self): return types.SimpleNamespace(to_string=lambda: self.text)

class Decision:
    def __init__(self, uri, gesture=False, nav_type=5):
        self.uri, self.gesture, self.nav_type, self.ignored = uri, gesture, nav_type, 0
    def get_navigation_action(self):
        return types.SimpleNamespace(
            get_request=lambda: types.SimpleNamespace(get_uri=lambda: self.uri),
            is_user_gesture=lambda: self.gesture,
            get_navigation_type=lambda: self.nav_type)
    def ignore(self): self.ignored += 1

class Error:
    def __init__(self, domain, code): self.domain, self.code = domain, code
    def matches(self, domain, code): return (self.domain, self.code) == (domain, code)

CANCELLED = Error("webkit-network-error-quark", 302)
OTHER_ERR = Error("webkit-network-error-quark", 1)

class Boot:
    """One run() of the picker, with its widgets and handlers captured."""
    def __init__(self, tokens=None, loop=None):
        reset_env()
        env.loop = loop
        FakeClient.tokens = {"ut": UT, "wt": "WT-VALUE", "wrt": WRT, "ws_id": "ws_TEST123",
                             "wt_exp": FAR} if tokens is None else tokens
        FakeClient.refreshed = 0
        made = []
        real_window = Gtk.Window
        def make_window(**kw):
            env.windows += 1
            made.append(Widget(**kw))
            return made[-1]
        Gtk.Window = make_window
        try:
            self.rc = generation_web.run(URL)
        finally:
            Gtk.Window = real_window
        self.window = made[0]
        # Whatever run() scheduled at boot besides the 60 s load timeout.
        others = [i for i, t in env.timers.items() if t["ms"] != 60_000]
        self.expiry = env.expiry = others[0] if len(others) == 1 else None
        self.layout = self.window.children[0]
        self.status, self.page, self.footer = self.layout.children
        self.browser, self.closer = self.footer.children
        self.view = self.page.children[0]
        self.manager = self.view.kw["user_content_manager"]
        self.context = self.view.kw["web_context"]
    def send(self, text):
        return self.manager.emit("script-message-received::selector", Msg(text))
    def policy(self, decision, kind=0):
        return self.view.emit("decide-policy", decision, kind)
    def fail(self, error):
        return self.view.emit("load-failed", 0, URL, error)
    def key(self, keyval):
        return self.window.emit("key-press-event", types.SimpleNamespace(keyval=keyval))
    def state(self):
        return (self.status.visible, self.page.visible, self.footer.visible, self.window.visible)

# ---------------------------------------------------------------- CHECK 1
b = Boot()
srcs = [s.source for s in env.scripts]
js = (pathlib.Path(repo) / "plaud_linux/web/generation.js").read_text()
css = (pathlib.Path(repo) / "plaud_linux/web/generation.css").read_text()
check("1 bootstrap + generation.js + stylesheet are injected on web.plaud.ai only, without the refresh token",
      b.rc == 4 and len(env.scripts) == 2 and len(env.sheets) == 1
      and 'localStorage.setItem("pld_tokenstr"' in srcs[0] and "pld_user1:workspaceList" in srcs[0]
      and "WT-VALUE" in srcs[0] and srcs[1] == js and env.sheets[0].source == css
      and all(s.allow == ["https://web.plaud.ai/*"] for s in env.scripts + env.sheets)
      and not any(WRT in s or "refreshToken" in s for s in srcs)
      and env.registered == ["selector"] and env.load_uri == [URL]
      and env.prgname == "plaud-generation" and ("WebKit2", "4.1") in env.require
      and FakeClient.refreshed == 0,
      f"scripts={len(env.scripts)} sheets={len(env.sheets)} allow={[s.allow for s in env.scripts]}")

# ---------------------------------------------------------------- CHECK 2
initial = pending()
check("2 the picker starts with a single 60 s timeout, the page shown and the fallback footer hidden",
      len(initial) == 1 and list(initial.values())[0]["ms"] == 60_000
      and b.state() == (True, True, False, True) and b.browser.visible and b.closer.visible,
      f"timers={[(t['ms']) for t in initial.values()]} state={b.state()}")
first_timeout = list(initial)[0]

# ---------------------------------------------------------------- CHECK 3
b.send("page")
after = pending()
check("3 'page' shortens the 60 s timeout to 10 s",
      first_timeout in env.removed and len(after) == 1
      and list(after.values())[0]["ms"] == 10_000 and b.state() == (True, True, False, True),
      f"removed={env.removed} timers={[t['ms'] for t in after.values()]}")
short_timeout = list(after)[0]

# ---------------------------------------------------------------- CHECK 4
b.send("ready:464:477")
lua = lua_calls()
sel = f'window="address:{ADDRESS}"'
check("4 'ready:464:477' shows the page box, hides status/footer and floats 496x509 through hyprctl",
      b.state() == (False, True, False, True) and short_timeout in env.removed and not pending()
      and b.window.resizes == [(496, 509)] and len(lua) == 1
      and f'float({{action="enable", {sel}}})' in lua[0]
      and f'set_prop({{prop="opaque", value="1", {sel}}})' in lua[0]
      and f'resize({{x=496, y=509, {sel}}})' in lua[0] and f'center({{{sel}}})' in lua[0]
      and "0x1" not in lua[0] and "0x2" not in lua[0],
      f"state={b.state()} resizes={b.window.resizes} lua={lua!r}")

# ---------------------------------------------------------------- CHECK 5
b.send("ready:464:477")
check("5 the same size reported again does not float the window a second time",
      b.window.resizes == [(496, 509)] and len(lua_calls()) == 1, repr(b.window.resizes))

# ---------------------------------------------------------------- CHECK 6
b.send("templates")
check("6 'templates' without a size floats to the 925x760 default",
      b.window.resizes == [(496, 509), (925, 760)]
      and "resize({x=925, y=760," in lua_calls()[-1] and b.state() == (False, True, False, True),
      repr(b.window.resizes))

# ---------------------------------------------------------------- CHECK 7
b.send("generating")
close_timers = pending()
check("7 'generating' keeps the process alive 20 s and tells the visible window the request is out",
      len(close_timers) == 1 and list(close_timers.values())[0]["ms"] == 20_000
      and b.state() == (True, False, False, True) and "Geração iniciada" in b.status.text,
      f"timers={[t['ms'] for t in close_timers.values()]} state={b.state()} text={b.status.text!r}")
close20 = list(close_timers)[0]

# ---------------------------------------------------------------- CHECK 8
b.send("closed")
check("8 'closed' after 'generating' changes nothing: the window stays up on its 20 s timer",
      b.state() == (True, False, False, True) and list(pending()) == [close20]
      and close20 not in env.removed, f"state={b.state()} timers={list(pending())}")

# ---------------------------------------------------------------- CHECK 9
b.send("sent")
sent_timers = pending()
check("9 'sent' while visible replaces the 20 s timer with a 1500 ms close",
      close20 in env.removed and len(sent_timers) == 1
      and list(sent_timers.values())[0]["ms"] == 1500,
      f"removed={env.removed} timers={[t['ms'] for t in sent_timers.values()]}")

# ---------------------------------------------------------------- CHECK 10
fire(list(sent_timers)[0])
check("10 the close timer destroys the window, stops the load, drops the scripts and quits the loop",
      b.window.destroyed and env.main_quit == 1 and env.stop_loading == 1
      and env.removed_scripts == 1 and not pending(),
      f"destroyed={b.window.destroyed} quit={env.main_quit} timers={list(pending())}")

# ---------------------------------------------------------------- CHECK 11
b.send("ready:464:477")
b.send("generating")
check("11 messages after destroy are ignored: no timer, no show, no hyprctl",
      not pending() and b.state() == (True, False, False, False) and len(lua_calls()) == 2
      and env.main_quit == 1, f"timers={list(pending())} state={b.state()}")

# ---------------------------------------------------------------- CHECK 12
b = Boot()
b.send("ready:464:477")
b.send("closed")
hidden = pending()
check("12 'closed' hides the window at once and keeps the page alive 6 s",
      b.window.visible is False and len(hidden) == 1
      and list(hidden.values())[0]["ms"] == 6_000 and not b.window.destroyed,
      f"visible={b.window.visible} timers={[t['ms'] for t in hidden.values()]}")
close6 = list(hidden)[0]

# ---------------------------------------------------------------- CHECK 13
b.send("ready:464:477")
check("13 a later 'ready' shows the window again and cancels the 6 s close",
      b.window.visible and close6 in env.removed and not pending()
      and b.state() == (False, True, False, True), f"removed={env.removed} state={b.state()}")

# ---------------------------------------------------------------- CHECK 14
b.send("closed")
b.send("generating")
gen_hidden = pending()
b.send("sent")
sent_hidden = pending()
check("14 'closed' then 'generating' then 'sent' (Generate now with a late request) closes in 100 ms, without showing status",
      not b.window.visible and len(gen_hidden) == 1 and list(gen_hidden.values())[0]["ms"] == 20_000
      and b.status.visible is False and "Geração iniciada" not in b.status.text
      and len(sent_hidden) == 1 and list(sent_hidden.values())[0]["ms"] == 100,
      f"visible={b.window.visible} gen={[t['ms'] for t in gen_hidden.values()]} "
      f"sent={[t['ms'] for t in sent_hidden.values()]} text={b.status.text!r}")

# ---------------------------------------------------------------- CHECK 15
b = Boot()
b.send("ready:464:477")
b.send("login")
check("15 'login' hands the note to the browser and closes the window",
      env.launched == [URL] and b.window.destroyed and env.main_quit == 1,
      f"launched={env.launched} destroyed={b.window.destroyed}")

# ---------------------------------------------------------------- CHECK 16
b = Boot()
stripe = Decision("https://js.stripe.com/v3/controller.html", gesture=False, nav_type=5)
r16 = b.policy(stripe, 0)
check("16 a third-party frame the page loads on its own (js.stripe.com) is ignored, not sent to the browser",
      r16 is True and stripe.ignored == 1 and env.launched == [],
      f"ret={r16} ignored={stripe.ignored} launched={env.launched}")

# ---------------------------------------------------------------- CHECK 17
help_g = Decision("https://help.plaud.ai/x", gesture=True, nav_type=5)
r17a = b.policy(help_g, 0)
help_c = Decision("https://help.plaud.ai/y", gesture=False, nav_type=0)
r17b = b.policy(help_c, 0)
check("17 an external link the user followed (gesture or LINK_CLICKED) opens in the browser and is ignored here",
      r17a is True and r17b is True and help_g.ignored == 1 and help_c.ignored == 1
      and env.launched == ["https://help.plaud.ai/x", "https://help.plaud.ai/y"],
      f"launched={env.launched}")

# ---------------------------------------------------------------- CHECK 18
same = Decision("https://web.plaud.ai/file/other", gesture=True, nav_type=0)
r18 = b.policy(same, 0)
resp = Decision("https://web.plaud.ai/file/other", gesture=True, nav_type=0)
r18b = b.policy(resp, 2)
check("18 navigation inside web.plaud.ai (and a RESPONSE decision) is allowed",
      r18 is False and same.ignored == 0 and r18b is False and resp.ignored == 0
      and len(env.launched) == 2, f"ret={r18},{r18b} ignored={same.ignored}")

# ---------------------------------------------------------------- CHECK 19
popup = Decision("https://web.plaud.ai/file/other", gesture=True, nav_type=0)
r19a = b.policy(popup, 1)
plain = Decision("http://help.plaud.ai/x", gesture=True, nav_type=0)
r19b = b.policy(plain, 0)
check("19 a new window to web.plaud.ai and any http:// link are ignored without opening a browser",
      r19a is True and popup.ignored == 1 and r19b is True and plain.ignored == 1
      and len(env.launched) == 2, f"launched={env.launched}")

# ---------------------------------------------------------------- CHECK 20
before = b.state()
r20 = b.fail(CANCELLED)
check("20 a CANCELLED load (the web app redirecting itself) is not a failure",
      r20 is False and b.state() == before == (True, True, False, True), f"ret={r20} state={b.state()}")

# ---------------------------------------------------------------- CHECK 21
r21 = b.fail(OTHER_ERR)
check("21 any other load error shows the failure status and the browser/close footer",
      r21 is True and b.state() == (True, False, True, True)
      and "Não foi possível carregar o seletor" in b.status.text,
      f"ret={r21} state={b.state()} text={b.status.text!r}")

# ---------------------------------------------------------------- CHECK 22
b.browser.emit("clicked")
b.closer.emit("clicked")
check("22 the footer buttons open the note in the browser and close the window",
      env.launched[-1] == URL and b.window.destroyed and env.main_quit == 1,
      f"launched={env.launched[-1:]} destroyed={b.window.destroyed}")

# ---------------------------------------------------------------- CHECK 23
b = Boot()
b.send("ready:464:477")
b.send("generating")
snap = (b.state(), b.status.text)
r23a = b.fail(OTHER_ERR)
unchanged = (b.state(), b.status.text) == snap
fire(list(pending())[0])
r23b = b.fail(OTHER_ERR)
check("23 a load error while generating, or after destroy, changes nothing",
      r23a is True and unchanged and b.window.destroyed and r23b is True
      and b.footer.visible is False and b.status.text == snap[1],
      f"state={b.state()} text={b.status.text!r}")

# ---------------------------------------------------------------- CHECK 24
b = Boot()
r24a = b.key(Gdk.KEY_a)
r24b = b.key(Gdk.KEY_Escape)
check("24 Escape destroys the window; any other key is left to GTK",
      r24a is False and r24b is True and b.window.destroyed and env.main_quit == 1,
      f"ret={r24a},{r24b} destroyed={b.window.destroyed}")

# ---------------------------------------------------------------- CHECK 25
b = Boot()
b.send("ready:464:477")
b.send("closed")
ids = list(pending())
b.window.destroy()
check("25 destroy cancels every pending timer and quits the loop once",
      len(ids) == 1 and all(i in env.removed for i in ids) and not pending()
      and env.main_quit == 1 and env.stop_loading == 1 and env.removed_scripts == 1,
      f"ids={ids} removed={env.removed} quit={env.main_quit}")

# ---------------------------------------------------------------- CHECK 26
b = Boot()
fire(list(pending())[0])
check("26 the 60 s timeout with no picker reported shows the failure status and the footer",
      b.state() == (True, False, True, True) and "Não foi possível carregar" in b.status.text
      and not pending(), f"state={b.state()}")

# ---------------------------------------------------------------- CHECK 27
b = Boot()
b.window.emit("map-event", None)
mapped = pending()
fire([i for i, t in mapped.items() if t["ms"] == 150][0])
check("27 map-event floats the window at its default size after 150 ms",
      any(t["ms"] == 150 for t in mapped.values()) and b.window.resizes == [(496, 474)]
      and "resize({x=496, y=474," in lua_calls()[-1], f"resizes={b.window.resizes}")

# ---------------------------------------------------------------- CHECK 28
b = Boot(tokens={"ut": UT, "wt": "WT-OLD", "wrt": WRT, "ws_id": "ws_TEST123", "wt_exp": NOW + 120})
src28, refreshed28 = env.scripts[0].source, FakeClient.refreshed
b2 = Boot(tokens={})
check("28 a workspace token about to expire is renewed by the recorder first; without a session only generation.js is injected",
      refreshed28 == 1 and FakeClient.refreshed == 0 and "WT-RENEWED" in src28 and "WT-OLD" not in src28
      and WRT not in src28 and len(env.scripts) == 1 and env.scripts[0].source == js
      and "pld_tokenstr" not in env.scripts[0].source,
      f"refreshed={refreshed28},{FakeClient.refreshed} scripts={len(env.scripts)}")

# ---------------------------------------------------------------- CHECK 29
b = Boot()
b.send("ready:464:477")
check("29 with a session the picker schedules its stop 360 s before wt_exp (3600 s token -> 3240 s), untouched by 'ready'",
      b.expiry is not None and env.timers[b.expiry]["ms"] == 3_240_000
      and not env.timers[b.expiry]["removed"] and not pending(),
      f"expiry={b.expiry and env.timers[b.expiry]['ms']} timers={list(pending())}")

# ---------------------------------------------------------------- CHECK 30
fire(b.expiry)
blank = Decision("about:blank", gesture=False, nav_type=5)
r30 = b.policy(blank, 0)
b.send("ready:464:477")
r30b = b.fail(OTHER_ERR)
check("30 firing it unloads the page (stop, scripts dropped, about:blank allowed), shows the expiry status and the footer, ignores later messages and load errors",
      env.stop_loading == 1 and env.removed_scripts == 1 and env.load_uri == [URL, "about:blank"]
      and r30 is False and blank.ignored == 0 and b.state() == (True, False, True, True)
      and "A sessão expirou" in b.status.text and r30b is True and b.page.visible is False
      and not b.window.destroyed and env.main_quit == 0 and not pending() and env.launched == [],
      f"state={b.state()} text={b.status.text!r} load_uri={env.load_uri} ret={r30},{r30b}")

# ---------------------------------------------------------------- CHECK 31
b = Boot()
b.send("ready:464:477")
b.send("generating")
snap31 = (b.state(), b.status.text, list(pending()))
fire(b.expiry)
b.send("sent")
check("31 firing it after 'generating' changes nothing: the request finishes through the sent/close path",
      (b.state(), b.status.text) == snap31[:2] and env.stop_loading == 0 and env.removed_scripts == 0
      and env.load_uri == [URL] and snap31[2][0] in env.removed
      and [t["ms"] for t in pending().values()] == [1500],
      f"state={b.state()} text={b.status.text!r} timers={[t['ms'] for t in pending().values()]}")

# ---------------------------------------------------------------- CHECK 32
b = Boot()
b.window.destroy()
check("32 destroy removes the expiry timer too",
      b.expiry in env.removed and env.timers[b.expiry]["removed"] and env.main_quit == 1,
      f"removed={env.removed}")

# ---------------------------------------------------------------- CHECK 33
b = Boot(tokens={})
none33 = (b.expiry, [t["ms"] for t in env.timers.values()])
real_refresh = FakeClient._refresh_wt
FakeClient._refresh_wt = lambda self: (_ for _ in ()).throw(OSError("offline"))
try:
    b = Boot(tokens={"ut": UT, "wt": "WT-SHORT", "wrt": WRT, "ws_id": "ws_TEST123", "wt_exp": NOW + 320})
finally:
    FakeClient._refresh_wt = real_refresh
check("33 no session, no timer; a 320 s token the recorder failed to renew is stopped after the 1 s floor",
      none33 == (None, [60_000]) and b.expiry is not None and env.timers[b.expiry]["ms"] == 1_000
      and "WT-SHORT" in env.scripts[0].source,
      f"none={none33} floor={b.expiry and env.timers[b.expiry]['ms']}")

# ---------------------------------------------------------------- CHECK 34
# The exit status is the parent's only view of what happened in the window:
# 0 says the official page issued its generation request (main.py then
# monitors the note and shows the completion notice), 4 says the picker
# closed without one. Driven inside Gtk.main() because the status is decided
# when the loop returns.
def send_in_loop(*texts):
    for text in texts:
        env.managers[-1].emit("script-message-received::selector", Msg(text))
def fire_close(ms):
    # Inside the loop env.expiry is not known yet, so pick the close by its delay.
    fire(next(i for i, t in pending().items() if t["ms"] == ms))
def loop_sent():
    send_in_loop("ready:464:477", "generating", "sent")
    fire_close(1500)
def loop_cancel():
    send_in_loop("ready:464:477", "closed")
    fire_close(6000)
def loop_refused():
    # Generate now, refused by the service, the dialog back, then Cancel.
    send_in_loop("ready:464:477", "generating", "ready:464:477", "closed")
    fire_close(6000)
def loop_login():
    send_in_loop("login")
rc_sent = Boot(loop=loop_sent).rc
quit_sent = env.main_quit
rc_cancel = Boot(loop=loop_cancel).rc
rc_refused = Boot(loop=loop_refused).rc
b_login = Boot(loop=loop_login)
check("34 the exit status is 0 once the page's request was observed, 4 when the picker closed without one (Cancel, refused then Cancel, login)",
      rc_sent == 0 and quit_sent == 1 and rc_cancel == 4 and rc_refused == 4
      and b_login.rc == 4 and env.launched == [URL] and env.main_quit == 1,
      f"sent={rc_sent} cancel={rc_cancel} refused={rc_refused} login={b_login.rc} launched={env.launched}")

# ---------------------------------------------------------------- CHECK 35
b = Boot()
rules = rule_calls()
check("35 a Hyprland rule floating, centring and sizing the exact class ^plaud-generation$ is registered once, before the window exists (a tiled map inherits a fullscreen workspace)",
      len(rules) == 1 and env.rule_windows == 0 and env.hyprctl[0][:2] == ["hyprctl", "repl"]
      and rules[0].startswith("if not PLAUD_GENERATION_RULE then")
      and 'match={class="^plaud-generation$"}' in rules[0] and "title=" not in rules[0]
      and "float=true" in rules[0] and "center=true" in rules[0] and 'size="496 474"' in rules[0]
      and "plaud-overlay" not in rules[0] and lua_calls() == [],
      f"rules={rules!r} rule_windows={env.rule_windows}")

# ---------------------------------------------------------------- CHECK 36
env.fullscreen = 2
b.send("ready:464:477")
lua = lua_calls()
check("36 a picker the compositor reports fullscreen is taken out of it (fullscreen_state 0 0) before float, opaque, resize and center, in one script",
      len(lua) == 1 and f'fullscreen_state({{internal=0, client=0, {sel}}})' in lua[0]
      and lua[0].index("fullscreen_state(") < lua[0].index("float(") < lua[0].index("resize(")
      < lua[0].index("center(") and b.window.resizes == [(496, 509)], repr(lua))

# ---------------------------------------------------------------- CHECK 37
env.fullscreen = 0
b.send("templates")
check("37 a picker that is not fullscreen gets no fullscreen_state dispatch",
      len(lua_calls()) == 2 and "fullscreen_state" not in lua_calls()[1]
      and "resize({x=925, y=760," in lua_calls()[1], repr(lua_calls()))

generation_web.subprocess = real_subprocess
generation_web.time = real_time_mod
sys.exit(0 if all(results) else 1)
