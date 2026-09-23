"""Dedicated, ephemeral view of the official generation selector.

Runs in a separate process so WebKit lifecycle/crashes cannot stop a recorder.
Only presentation is adapted; the official page owns all generation requests.
"""
import base64
import binascii
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit, parse_qs

ORIGIN = 'https://web.plaud.ai'
ASSETS = Path(__file__).with_name('web')
WMCLASS = 'plaud-generation'
TITLE = 'Gerar nota personalizada'
SIZES = {'ready': (496, 474), 'templates': (925, 760)}


def valid_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.netloc == 'web.plaud.ai'
                and parsed.path.startswith('/file/') and len(parsed.path[6:]) > 0
                and parse_qs(parsed.query).get('transcribeDialog') == ['custom'])
    except (ValueError, TypeError):
        return False


def launch(url):
    if not valid_url(url):
        raise ValueError('Link de geração inválido')
    # No token in arguments, environment or diagnostics. The child reads the
    # existing app session locally and uses a separate web workspace session.
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), url],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


# Web storage contract, observed in web-static.plaud.ai/web3 (chunk
# app-initial-common): keys are "pld_" + name for the global scope and
# "pld_<userId>:" + name for the user scope, values JSON-encoded. The user id
# is the `sub` claim of the UT. `workspaceList` is an array of
# {workspaceId, domain, workspaceToken, expiresAt, refreshToken,
# refreshExpiresAt, ...} with millisecond expiries; `currentWorkspaceId` is
# the selected workspace. With a stored, unexpired workspaceToken the page
# reuses it; otherwise it POSTs /user-app/auth/workspace/token/<ws>, and the
# backend then revokes the recorder's own workspace token (measured: -419 on
# the app 15 s after the picker loaded) and its refresh token (-420).
STORAGE_PREFIX = 'pld_'
# The page mints a replacement when its token has under 5 min left; the
# recorder renews before that so the page never has a reason to.
RENEW_MARGIN = 600
# A picker left open is stopped this long before the token expires: one
# minute ahead of the page's own 300 s rule (`Jw=300*1e3` in the bundle),
# since a page-side mint revokes the recorder's token (-419, then -420).
STOP_MARGIN = 360
_IDENT = re.compile(r'[A-Za-z0-9_-]{1,128}')


def _jwt_subject(token):
    """`sub` of an unverified JWT payload: it only names a storage key."""
    try:
        segment = token.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
        subject = payload.get('sub')
    except (AttributeError, IndexError, ValueError, TypeError, binascii.Error):
        return None
    return subject if isinstance(subject, str) and _IDENT.fullmatch(subject) else None


def web_session(tokens, api, now=None):
    """The recorder's workspace session as the web app stores it, or None.

    Only the workspace token travels, never the refresh token: when this one
    expires the page mints its own, as it always did, instead of holding a
    credential that could rotate the recorder's. Nothing is returned for a
    token the page would refresh anyway (under 5 min left) or an incomplete
    session; the page then behaves as before the injection existed.
    """
    now = time.time() if now is None else now
    user = _jwt_subject(tokens.get('ut') or '')
    wt, ws_id, exp = tokens.get('wt'), tokens.get('ws_id'), tokens.get('wt_exp')
    if not (user and isinstance(wt, str) and wt and isinstance(ws_id, str)
            and _IDENT.fullmatch(ws_id) and isinstance(exp, (int, float))
            and exp - now > RENEW_MARGIN / 2
            and isinstance(api, str) and api.startswith('https://')):
        return None
    return {'user': user, 'ws_id': ws_id, 'wt': wt,
            'expires_at_ms': int(exp) * 1000, 'domain': api.rstrip('/')}


def bootstrap_script(token, session=None):
    # Observed web storage contract: tokenstr is a JSON-encoded Bearer UT.
    # Never copy the recorder's workspace refresh token into the web session.
    bearer = 'Bearer ' + token.removeprefix('Bearer ').removeprefix('bearer ')
    body = ('localStorage.setItem("pld_tokenstr",JSON.stringify('
            + json.dumps(bearer) + '));')
    if session:
        scope = STORAGE_PREFIX + session['user'] + ':'
        entry = [{'workspaceId': session['ws_id'], 'domain': session['domain'],
                  'workspaceToken': session['wt'], 'expiresAt': session['expires_at_ms']}]
        body += ('localStorage.setItem(' + json.dumps(scope + 'currentWorkspaceId') + ','
                 + json.dumps(json.dumps(session['ws_id'])) + ');'
                 + 'localStorage.setItem(' + json.dumps(scope + 'workspaceList') + ','
                 + json.dumps(json.dumps(entry)) + ');')
    return ('if(location.origin===' + json.dumps(ORIGIN)
            + '&&location.pathname.startsWith("/file/")){' + body + '}')


MARGIN = 16          # matches the dialog margin in generation.css
FRAME = 2 * MARGIN   # window = card + margins, so the picker IS the window


def parse_message(value):
    """'ready:464:442' -> ('ready', 464, 442); bare states carry no size."""
    if not isinstance(value, str):
        return None, 0, 0
    parts = value.split(':')
    if parts[0] not in ('ready', 'templates', 'generating', 'sent', 'closed', 'login', 'page'):
        return None, 0, 0
    try:
        width = int(parts[1]) if len(parts) > 1 else 0
        height = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None, 0, 0
    return parts[0], max(width, 0), max(height, 0)


def fit_size(width, height, area, fallback):
    """Window size for a card of width x height inside the monitor workarea.

    The card (plus its margins) becomes the window, like the official dialog;
    a card taller than the monitor keeps its own scrollbar (max-height in
    generation.css). Sizes below the reported card fall back to defaults.
    """
    if width <= 0 or height <= 0:
        width, height = fallback[0] - FRAME, fallback[1] - FRAME
    return (min(width + FRAME, area[0] - 48), min(height + FRAME, area[1] - 48))


# Registered through `hyprctl repl` BEFORE the window maps, once per
# compositor lifetime (the Lua state persists between repl calls; the global
# guards against a rule per picker -- overlay.py's pattern). Matched on the
# exact class only: this process has one toplevel. Without it the picker maps
# TILED and is floated 150 ms later by _float(); Hyprland 0.56 hands a new
# tiled window the fullscreen state of the workspace it lands on, so with a
# fullscreen window there the picker mapped as `fullscreen 2` at monitor size
# and resize/center were no-ops (measured 2026-09-08: floating=true,
# fullscreen=2, size [1080,1920]). A window that is floating at its map keeps
# its own size and, created over a fullscreen window, renders above it
# (measured: the card visible over a red fullscreen probe on ws 5).
_RULE_LUA = (
    'if not PLAUD_GENERATION_RULE then\n'
    '  PLAUD_GENERATION_RULE = hl.window_rule({name="%(cls)s",\n'
    '    match={class="^%(cls)s$"},\n'
    '    float=true, center=true, size="%(w)d %(h)d"})\n'
    'end'
)


def _register_rule(size):
    """Best effort, 2 s, no raise: without Hyprland the window is still a
    window and _float() is the second chance after the map."""
    try:
        subprocess.run(['hyprctl', 'repl', _RULE_LUA % dict(cls=WMCLASS, w=size[0], h=size[1])],
                       capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass


def _float(window, size):
    """Float the picker at `size` under Hyprland, like the highlights panel.

    Same best-effort mechanism as panel.py: `hyprctl repl` after the map, on
    this process's window only, so the official picker is a dialog rather
    than a tile. Omarchy also tags every window with a default opacity, which
    let the desktop show through the white picker; the window is made opaque
    the same way. Any other compositor simply keeps its own placement.

    A window the compositor reports fullscreen is taken out of it first:
    that is what a tiled map onto a workspace with a fullscreen window left
    behind when the rule above was not in effect (see _RULE_LUA), and float,
    resize and center do nothing to a fullscreen window.
    """
    if not window.get_realized():
        return False
    width, height = size
    window.resize(width, height)
    try:
        result = subprocess.run(['hyprctl', 'clients', '-j'], capture_output=True,
                                text=True, timeout=2, check=True)
        candidates = [c for c in json.loads(result.stdout)
                      if c.get('pid') == os.getpid() and c.get('class') == WMCLASS
                      and c.get('mapped')]
        if len(candidates) == 1:
            address = candidates[0]['address']
            if not isinstance(address, str) or not address.startswith('0x'):
                raise ValueError('Invalid compositor address')
            int(address[2:], 16)
            sel = f'window="address:{address}"'
            lines = []
            if candidates[0].get('fullscreen'):
                lines.append(f'hl.dispatch(hl.dsp.window.fullscreen_state({{internal=0, client=0, {sel}}}))')
            lines += [f'hl.dispatch(hl.dsp.window.float({{action="enable", {sel}}}))',
                      f'hl.dispatch(hl.dsp.window.set_prop({{prop="opaque", value="1", {sel}}}))',
                      f'hl.dispatch(hl.dsp.window.resize({{x={width}, y={height}, {sel}}}))',
                      f'hl.dispatch(hl.dsp.window.center({{{sel}}}))']
            subprocess.run(['hyprctl', 'repl', '\n'.join(lines)], capture_output=True, timeout=2)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    return False


def run(url):
    if not valid_url(url):
        return 2
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, Gdk, GLib, Gio
    try:
        gi.require_version('WebKit2', '4.1')
        from gi.repository import WebKit2
    except (ValueError, ImportError):
        # webkit2gtk-4.1 is optional (README). Without it the custom flow
        # degrades to the browser tab the previous release used, so the
        # recording is never left without a way to generate its note.
        return 1 if Gio.AppInfo.launch_default_for_uri(url, None) else 3
    try:
        from . import plaud_api
    except ImportError:
        import plaud_api
    client = plaud_api.PlaudClient()
    token = client.tokens.get('ut')
    exp = client.tokens.get('wt_exp')
    if token and exp and exp - time.time() < RENEW_MARGIN:
        # Renew with the recorder's own refresh token before the page can
        # mint a replacement (see web_session). Best effort, before any GTK
        # loop exists: on failure the page mints, exactly as before.
        try:
            client._refresh_wt()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            pass
    session = web_session(client.tokens, client.api)
    # GTK3 on Wayland takes the app_id from g_prgname, not set_wmclass().
    GLib.set_prgname(WMCLASS)
    _register_rule(SIZES['ready'])
    window = Gtk.Window(title=TITLE)
    window.set_default_size(*SIZES['ready'])
    layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    status = Gtk.Label(label='Carregando opções do Plaud…')
    status.set_margin_top(12)
    status.set_margin_bottom(12)
    layout.pack_start(status, False, False, 0)
    manager = WebKit2.UserContentManager()
    context = WebKit2.WebContext.new_ephemeral()
    view = WebKit2.WebView(web_context=context, user_content_manager=manager)
    view.get_settings().set_enable_write_console_messages_to_stdout(False)
    view.connect('permission-request', lambda _view, request: (request.deny(), True)[1])
    context.connect('download-started', lambda _context, download: download.cancel())
    # Shown only when the picker cannot be used here: Hyprland gives this
    # window no title bar, so a visible way out is part of the fallback.
    footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    footer.set_border_width(8)
    browser = Gtk.Button(label='Abrir nota no navegador')
    browser.connect('clicked', lambda *_: Gio.AppInfo.launch_default_for_uri(url, None))
    closer = Gtk.Button(label='Fechar')
    closer.connect('clicked', lambda *_: window.destroy())
    footer.pack_start(browser, True, True, 0)
    footer.pack_start(closer, False, False, 0)
    footer.set_no_show_all(True)
    browser.show()
    closer.show()
    # After a resize the view composites its previous frame at the old size
    # for a few hundred ms and paints nothing else, so the toplevel's theme
    # background showed through: a dark grey flash whenever the picker grew
    # (measured). The page is white (generation.css), so the view sits on a
    # white backdrop; only this box is recoloured, the status label and the
    # fallback footer keep the theme.
    page = Gtk.EventBox()
    backdrop = Gtk.CssProvider()
    backdrop.load_from_data(b'* { background-color: #ffffff; }')
    page.get_style_context().add_provider(backdrop, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    page.add(view)
    layout.pack_start(page, True, True, 0)
    layout.pack_end(footer, False, False, 0)
    window.add(layout)
    window.connect('key-press-event', lambda _w, event: (
        window.destroy(), True)[1] if event.keyval == Gdk.KEY_Escape else False)
    state = {'timeout': None, 'close': None, 'expiry': None, 'destroyed': False,
             'generating': False, 'stopped': False, 'size': SIZES['ready'], 'name': None}

    def workarea():
        monitor = window.get_display().get_monitor_at_window(window.get_window())
        if monitor is None:
            return (1920, 1080)
        area = monitor.get_workarea()
        return (area.width, area.height)

    def fit(width, height, name):
        size = fit_size(width, height, workarea(), SIZES[name])
        # Re-apply on every state change: hiding the status label lets GTK
        # shrink the window to its natural size (measured 474 -> 431 px).
        if name != state['name'] or max(abs(size[0] - state['size'][0]),
                                        abs(size[1] - state['size'][1])) > 4:
            state['size'] = size
            state['name'] = name
            _float(window, size)

    def cancel(key):
        if state[key] is not None:
            GLib.source_remove(state[key])
            state[key] = None

    def close():
        state['close'] = None
        if not state['destroyed']:
            window.destroy()
        return False

    def message(_manager, result):
        name, width, height = parse_message(result.get_js_value().to_string())
        if name is None or state['destroyed'] or state['stopped']:
            return
        if name == 'page':
            # The page is up but no picker has been reported. Give the app a
            # few seconds to open it, then offer the browser instead of
            # leaving "Carregando" on screen for the full minute.
            if state['name'] is None and state['timeout'] is not None:
                cancel('timeout')
                state['timeout'] = GLib.timeout_add_seconds(10, timeout)
            return
        cancel('timeout')
        if name in ('ready', 'templates'):
            # Also reached again if the generation request was refused and
            # the official dialog reappears with its own error message.
            cancel('close')
            state['generating'] = False
            status.hide()
            footer.hide()
            page.show()
            if not window.get_visible():
                window.show()
            fit(width, height, name)
        elif name == 'generating':
            # The request is out. Keep the web process alive until it is
            # answered ('sent'); a destroyed page cancels an in-flight XHR,
            # which lost a generation from the templates dialog (measured:
            # the page posts /ai/transsumm only after both dialogs are gone).
            state['generating'] = True
            cancel('close')
            state['close'] = GLib.timeout_add_seconds(20, close)
            if window.get_visible():
                page.hide()
                footer.hide()
                status.set_text('Geração iniciada. A nota ficará pronta em web.plaud.ai.')
                status.show()
        elif name == 'sent':
            if state['generating']:
                cancel('close')
                state['close'] = GLib.timeout_add(1500 if window.get_visible() else 100, close)
        elif name == 'closed':
            # Both dialogs are gone: Cancel, or Generate now with its request
            # still to come. The window disappears at once, like the official
            # dialog, but the page lives on for a few seconds so a late
            # request is neither lost nor claimed as a generation.
            if not state['generating']:
                window.hide()
                if state['close'] is None:
                    state['close'] = GLib.timeout_add_seconds(6, close)
        elif name == 'login':
            # The saved session was not accepted by the web app. Fall back to
            # the browser tab rather than showing a login form in this window.
            Gio.AppInfo.launch_default_for_uri(url, None)
            close()

    manager.register_script_message_handler('selector')
    manager.connect('script-message-received::selector', message)
    for source in ([bootstrap_script(token, session)] if token else []) + [(ASSETS / 'generation.js').read_text()]:
        manager.add_script(WebKit2.UserScript.new(source,
            WebKit2.UserContentInjectedFrames.TOP_FRAME, WebKit2.UserScriptInjectionTime.START,
            [ORIGIN + '/*'], None))
    manager.add_style_sheet(WebKit2.UserStyleSheet.new((ASSETS / 'generation.css').read_text(),
        WebKit2.UserContentInjectedFrames.TOP_FRAME, WebKit2.UserStyleLevel.USER,
        [ORIGIN + '/*'], None))

    def failed(_view=None, _event=None, _uri=None, error=None, *_):
        # A cancelled provisional load is the web app redirecting itself
        # (or stop_loading() from destroy), not a failure.
        if state['destroyed'] or state['generating'] or state['stopped']:
            return True
        if error is not None and error.matches(WebKit2.network_error_quark(),
                                               WebKit2.NetworkError.CANCELLED):
            return False
        status.set_text('Não foi possível carregar o seletor. Sua gravação está salva.')
        status.show()
        page.hide()
        footer.show()
        return True

    def policy(_view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri()
            if uri == 'about:blank' and state['stopped']:
                return False    # the unload expired() asked for
            target = urlsplit(uri)
            if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION or target.scheme != 'https' or target.netloc != 'web.plaud.ai':
                # Only links the user followed leave this window (as the
                # official client's openExternal does). Third-party frames
                # the page embeds on load (measured: js.stripe.com) are
                # navigations too, and must not open a browser tab.
                clicked = (action.is_user_gesture()
                           or action.get_navigation_type() == WebKit2.NavigationType.LINK_CLICKED)
                if clicked and target.scheme == 'https' and target.netloc and target.netloc != 'web.plaud.ai':
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                decision.ignore()
                return True
        return False

    def timeout():
        state['timeout'] = None
        failed()
        return False

    def expired():
        # The page mints its own workspace token once the stored one has
        # under 300 s left, which revokes the recorder's with no recovery
        # path. Unload the page before it can; a request already out is
        # left to the sent/close path, which ends the process anyway.
        state['expiry'] = None
        if state['destroyed'] or state['generating']:
            return False
        state['stopped'] = True
        cancel('timeout')
        view.stop_loading()
        manager.remove_all_scripts()
        view.load_uri('about:blank')
        page.hide()
        status.set_text('A sessão expirou. Abra a nota no navegador para gerar.')
        status.show()
        footer.show()
        return False

    def destroy(*_):
        state['destroyed'] = True
        cancel('timeout')
        cancel('close')
        cancel('expiry')
        view.stop_loading()
        manager.remove_all_scripts()
        Gtk.main_quit()

    view.connect('decide-policy', policy)
    view.connect('load-failed', failed)
    view.connect('web-process-terminated', failed)
    window.connect('destroy', destroy)
    window.connect('map-event', lambda *_: GLib.timeout_add(150, lambda: _float(window, state['size'])))
    window.show_all()
    state['timeout'] = GLib.timeout_add_seconds(60, timeout)
    if session:
        # Only with a session: without one the page mints as it always did.
        delay = session['expires_at_ms'] / 1000 - time.time() - STOP_MARGIN
        state['expiry'] = GLib.timeout_add_seconds(max(int(delay), 1), expired)
    view.load_uri(url)
    Gtk.main()
    # The exit status carries the one fact the parent cannot observe: whether
    # the official page issued its generation request while this window was
    # up (`generating`). 0 means it did, so the recorder monitors that note
    # and shows the completion notice, as it does for automatic generation;
    # 4 means the picker closed without any request (Cancel, Escape, the
    # login or failure fallbacks), so nothing is monitored or claimed.
    return 0 if state['generating'] else 4


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1]) if len(sys.argv) == 2 else 2)
