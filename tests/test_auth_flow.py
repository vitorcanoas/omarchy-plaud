"""OAuth handoff regressions. Private files, blocked network, real GTK loop."""
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import time
import types
from unittest.mock import patch

os.environ['PLAUD_LINUX_HOME'] = tempfile.mkdtemp(prefix='plaud-auth-flow-')
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
blocked = types.ModuleType('requests')
def forbid(*args, **kw):
    raise AssertionError('NETWORK NOT ALLOWED')
blocked.request = blocked.get = blocked.post = blocked.put = forbid
blocked.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules['requests'] = blocked
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gio
from plaud_linux import login, main, plaud_api

results = []
def check(name, ok):
    results.append(ok)
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)

class Client:
    connected = False
    calls = []
    fail = False
    device = 'test-device'
    def is_logged_in(self): return self.connected
    def login_with_auth_code(self, code):
        self.calls.append(code)
        time.sleep(0.08)
        if self.fail: raise RuntimeError('access_token=SECRET-MUST-NOT-LEAK')
        Client.connected = True

def reset():
    login._LOGIN_STATE.unlink(missing_ok=True)
    Client.connected = Client.fail = False
    Client.calls = []

with patch.object(plaud_api, 'PlaudClient', Client), patch.object(login, '_notify') as notify:
    reset()
    Client.connected = True
    check('connected callbacks do not exchange another code', login.handle_url('plaud://login?auth_code=late') and not Client.calls)
    reset()
    login.handle_url('plaud://login?auth_code=once')
    login.handle_url('plaud://login?auth_code=once')
    check('a successful callback is consumed only once', Client.calls == ['once'])
    reset()
    Client.fail = True
    log = io.StringIO()
    with contextlib.redirect_stderr(log):
        login.handle_url('plaud://login?auth_code=expired')
        login.handle_url('plaud://login?auth_code=expired')
    check('failed duplicate callbacks are not retried', Client.calls == ['expired'])
    check('login errors do not expose credentials', 'SECRET-MUST-NOT-LEAK' not in log.getvalue() and 'SECRET-MUST-NOT-LEAK' not in str(notify.call_args_list))
    reset()
    check('foreign schemes are rejected before token exchange', not login.handle_url('https://other.example/?auth_code=code') and not Client.calls)
    reset()
    Client.connected = True
    with patch.object(login.subprocess, 'Popen') as browser:
        login.begin_login(force=True)
        login.handle_url('plaud://login?auth_code=new-account')
        check('explicit reconnection permits a fresh exchange', Client.calls == ['new-account'] and browser.call_count == 1)
    reset()
    ticks, finished = [], []
    login.handle_url_async('plaud://login?auth_code=slow', lambda ok: (finished.append(ok), Gtk.main_quit()))
    timer = GLib.timeout_add(10, lambda: ticks.append(1) or True)
    timeout = GLib.timeout_add(2000, lambda: Gtk.main_quit() or False)
    Gtk.main()
    GLib.source_remove(timer)
    GLib.source_remove(timeout)
    check('login does not block the GTK loop', finished == [True] and len(ticks) >= 3 and not login.is_authenticating())

app = Gio.Application(application_id='ai.plaud.AuthRegression', flags=Gio.ApplicationFlags.HANDLES_OPEN)
with patch.object(login, 'handle_url_async') as handle:
    main.wire_login_handler(app)
    first = app._plaud_login_handler
    main.wire_login_handler(app)
    app.register(None)
    app.open([Gio.File.new_for_uri('plaud://login?auth_code=one')], '')
    check('repeated session wiring installs one callback', first == app._plaud_login_handler and handle.call_count == 1)

client = plaud_api.PlaudClient()
client.tokens = {'ut':'old-user', 'wt':'old-workspace-token', 'wrt':'old-refresh', 'ws_id':'old-workspace'}
response = types.SimpleNamespace(json=lambda: {'access_token':'new-user'})
with patch.object(plaud_api.requests, 'post', return_value=response), patch.object(plaud_api.requests, 'get', side_effect=RuntimeError('offline')):
    try: client.login_with_auth_code('new-code')
    except RuntimeError: pass
saved = plaud_api._load_tokens()
check('partial new login never retains old workspace credentials', saved.get('ut') == 'new-user' and not saved.get('wt') and not saved.get('wrt'))

with patch.object(main, '_open_url') as web:
    main.open_web_home()
    url = web.call_args.args[0]
    check('folder opens Plaud Web without triggering OAuth', url.startswith('https://web.plaud.ai/?') and 'launch-desktop' not in url and 'from=desktop' in url)

sys.exit(not all(results))
