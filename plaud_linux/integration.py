"""User-local desktop integration; no GTK, account access or capture startup."""
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import selectors
import signal
import stat
import subprocess
import sys
import time

try:
    from .safe_io import Directory, UnsafePath, data_home
except ImportError:
    from safe_io import Directory, UnsafePath, data_home

BEGIN = '-- >>> plaud-linux shortcuts >>>'
END = '-- <<< plaud-linux shortcuts <<<'
MANAGED = 'X-Plaud-Linux-Managed=true\n'
LIMIT = 64 * 1024


def child_environment(source=None):
    source = os.environ if source is None else source
    allowed = set(('HOME USER LOGNAME LANG DISPLAY WAYLAND_DISPLAY XAUTHORITY '
                   'XDG_RUNTIME_DIR XDG_SESSION_TYPE XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP '
                   'XDG_DATA_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_DIRS XDG_CONFIG_DIRS '
                   'DBUS_SESSION_BUS_ADDRESS HYPRLAND_INSTANCE_SIGNATURE '
                   'PULSE_SERVER PULSE_COOKIE PULSE_RUNTIME_PATH PULSE_SINK PULSE_SOURCE PIPEWIRE_REMOTE '
                   'PIPEWIRE_RUNTIME_DIR PLAUD_LINUX_HOME PLAUD_LINUX_AUTOSTART_DIR '
                   'HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy '
                   'all_proxy no_proxy REQUESTS_CA_BUNDLE CURL_CA_BUNDLE SSL_CERT_FILE '
                   'SSL_CERT_DIR GTK_THEME GDK_BACKEND DESKTOP_SESSION XDG_SESSION_ID').split())
    env = {k: v for k, v in source.items() if k in allowed or k.startswith('LC_')}
    env['PATH'] = '/usr/bin:/bin'
    if source.get('SNAP'):
        for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
            env.pop(key, None)
    return env


def run_tool(argv, *, timeout=3, limit=LIMIT):
    """Run a fixed system helper with bounded output and group cleanup."""
    executable = Path(argv[0])
    st = executable.stat()
    if (not executable.is_absolute() or not stat.S_ISREG(st.st_mode)
            or st.st_uid != 0 or st.st_mode & 0o022):
        raise UnsafePath('Executável do sistema inválido.')
    proc = subprocess.Popen(argv, env=child_environment(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('O comando excedeu o tempo permitido.')
            for key, _ in selector.select(min(left, .1)):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    output.extend(chunk)
                    if len(output) > limit:
                        raise UnsafePath('O comando excedeu o limite de saída.')
        rc = proc.wait(timeout=max(.01, deadline - time.monotonic()))
        return rc, bytes(output)
    finally:
        selector.close()
        proc.stdout.close()
        # Includes grandchildren that inherited neither output nor parent life.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def supported_launcher(launcher):
    value = str(Path(launcher).absolute())
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
        raise UnsafePath('Use uma pasta sem espaços ou caracteres especiais para instalar o Plaud Linux.')
    return value


def desktop_entry(launcher, *, background=False, protocol=False):
    launcher = supported_launcher(launcher)
    command = launcher + (' --background' if background else ' %u' if protocol else '')
    return ('[Desktop Entry]\nType=Application\nVersion=1.0\n'
            'Name=Plaud Linux' + (' (login handler)' if protocol else '') + '\n'
            'Comment=Grava e envia áudio para sua conta Plaud\n'
            f'Exec={command}\nIcon=plaud-linux\nTerminal=false\n' + MANAGED +
            ('X-GNOME-Autostart-enabled=true\n' if background else
             'MimeType=x-scheme-handler/plaud;\nNoDisplay=true\n' if protocol else
             'Categories=AudioVideo;Utility;\nStartupNotify=true\n')).encode()


def _managed(data):
    text = data.decode('utf-8')
    return (text.startswith('[Desktop Entry]\n')
            and re.search(r'^Name=Plaud Linux(?: \(login handler\))?$', text, re.M)
            and re.search(r'^Exec=[^\n]*/bin/plaud-linux(?: [^\n]*)?$', text, re.M))


def _write_desktop(directory, name, content):
    try:
        previous = directory.read(name, LIMIT)
    except FileNotFoundError:
        previous = None
    if previous is not None and not _managed(previous):
        raise UnsafePath('Já existe uma entrada de outro aplicativo com esse nome.')
    if previous != content:
        directory.write(name, content, backup=previous is not None)


def set_autostart(enabled, launcher, directory):
    try:
        parent = Directory.open(directory, create=enabled)
    except FileNotFoundError:
        if not enabled:
            return
        raise
    with parent as dest:
        if enabled:
            _write_desktop(dest, 'plaud-linux.desktop', desktop_entry(launcher, background=True))
        else:
            try:
                old = dest.snapshot('plaud-linux.desktop')
                if old is None:
                    return
                if not _managed(dest.read('plaud-linux.desktop', LIMIT)):
                    raise UnsafePath('A entrada de inicialização pertence a outro aplicativo.')
                dest.remove('plaud-linux.desktop', old)
            except FileNotFoundError:
                pass


def autostart_enabled(directory):
    try:
        with Directory.open(directory) as dest:
            return bool(_managed(dest.read('plaud-linux.desktop', LIMIT)))
    except (OSError, ValueError):
        return False


def read_settings(path):
    try:
        path = Path(path)
        with Directory.open(path.parent.parent, resolve_anchor=True) as root, root.child(path.parent.name) as parent:
            value = json.loads(parent.read(path.name, LIMIT))
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RuntimeError):
        return {}


def write_settings(path, values):
    content = json.dumps(values, ensure_ascii=False, indent=2).encode('utf-8')
    if len(content) > LIMIT:
        raise UnsafePath('As preferências excedem o limite permitido.')
    path = Path(path)
    with Directory.open(path.parent.parent, create=True, resolve_anchor=True) as root, root.child(path.parent.name, create=True) as parent:
        parent.write(path.name, content)


def shortcut_content(old, launcher, *, remove=False):
    text = old.decode('utf-8')
    begins, ends = text.count(BEGIN), text.count(END)
    if begins != ends or begins > 1:
        raise UnsafePath('Bloco de atalhos inválido; o arquivo foi preservado.')
    if begins:
        pattern = r'(?m)^' + re.escape(BEGIN) + r'\n[\s\S]*?^' + re.escape(END) + r'(?:\n|$)'
        match = re.search(pattern, text)
        if not match:
            raise UnsafePath('Bloco de atalhos inválido; o arquivo foi preservado.')
        if not remove:
            return old
        start = match.start()
        if start and text[start - 1] == '\n':
            start -= 1
        return (text[:start] + text[match.end():]).encode('utf-8')
    if remove:
        return old
    launcher = supported_launcher(launcher)
    bindings = [('SUPER + ALT + R', 'iniciar/parar gravação', 'record'),
                ('SUPER + ALT + E', 'encerrar gravação', 'stop'),
                ('SUPER + ALT + P', 'pausar/retomar gravação', 'pause'),
                ('ALT + SHIFT + H', 'marcar destaque', 'mark'),
                ('ALT + SHIFT + C', 'capturar região', 'shot')]
    block = '\n' + BEGIN + '\n' + ''.join(
        f'o.bind("{keys}", "Plaud: {label}", "{launcher} --shortcut {action}")\n'
        for keys, label, action in bindings) + END + '\n'
    return old + block.encode('utf-8')


def change_shortcuts(home, launcher, *, remove=False, commands=True):
    try:
        with Directory.open(Path(home) / '.config', resolve_anchor=True) as config, config.child('hypr') as hypr:
            old = hypr.read('bindings.lua', 1024 * 1024)
            new = shortcut_content(old, launcher, remove=remove)
            if old == new:
                return
            hypr.write('bindings.lua', new, backup=True)
            if commands and Path('/usr/bin/hyprctl').exists():
                try:
                    if run_tool(['/usr/bin/hyprctl', 'reload'])[0]:
                        raise UnsafePath('Não foi possível recarregar o Hyprland.')
                    rc, errors = run_tool(['/usr/bin/hyprctl', 'configerrors'])
                    if rc or errors.strip() not in (b'', b'no errors'):
                        raise UnsafePath('O Hyprland informou erros; os atalhos foram restaurados.')
                except (OSError, TimeoutError, subprocess.SubprocessError):
                    # Do not overwrite a concurrent user edit during validation.
                    if hypr.read('bindings.lua', 1024 * 1024) == new:
                        hypr.write('bindings.lua', old)
                        run_tool(['/usr/bin/hyprctl', 'reload'])
                    raise
    except FileNotFoundError:
        if not remove:
            raise UnsafePath('bindings.lua não encontrado; instale os atalhos em uma sessão Omarchy.')


def install(root, argv, *, home=None, commands=True):
    parser = argparse.ArgumentParser(description='Instalação local do Plaud Linux')
    parser.add_argument('--autostart', action='store_true')
    parser.add_argument('--hypr-shortcuts', action='store_true')
    parser.add_argument('--remove-hypr-shortcuts', action='store_true')
    args = parser.parse_args(argv)
    if args.remove_hypr_shortcuts and (args.autostart or args.hypr_shortcuts):
        parser.error('--remove-hypr-shortcuts deve ser usado sozinho')
    root = Path(root).resolve()
    home = Path.home() if home is None else Path(home)
    launcher = supported_launcher(root / 'bin/plaud-linux')
    if args.remove_hypr_shortcuts:
        change_shortcuts(home, launcher, remove=True, commands=commands)
        return
    if os.getuid() == 0:
        raise UnsafePath('Execute como usuário normal, sem sudo.')
    with Directory.open(root / 'assets') as assets:
        icon = assets.read('plaud-linux.png', 8 * 1024 * 1024)
    with Directory.open(home, resolve_anchor=True) as base:
        with base.descend('.local/bin', create=True) as dest:
            dest.symlink('plaud-linux', launcher)
        with base.descend('.local/share/icons/hicolor/256x256/apps', create=True) as dest:
            try:
                prior_icon = dest.read('plaud-linux.png', 8 * 1024 * 1024)
            except FileNotFoundError:
                prior_icon = None
            if prior_icon != icon:
                dest.write('plaud-linux.png', icon, backup=prior_icon is not None)
        with base.descend('.local/share/applications', create=True) as dest:
            _write_desktop(dest, 'plaud-linux.desktop', desktop_entry(launcher))
            _write_desktop(dest, 'plaud-note.desktop', desktop_entry(launcher, protocol=True))
    if args.autostart:
        set_autostart(True, launcher, home / '.config/autostart')
    if args.hypr_shortcuts:
        change_shortcuts(home, launcher, commands=commands)
    if commands:
        for argv in (['/usr/bin/update-desktop-database', str(home / '.local/share/applications')],
                     ['/usr/bin/xdg-mime', 'default', 'plaud-note.desktop', 'x-scheme-handler/plaud']):
            try:
                run_tool(argv)
            except (OSError, TimeoutError, subprocess.SubprocessError):
                print('Aviso: não foi possível atualizar o registro do ambiente gráfico.', file=sys.stderr)


def open_log():
    """Return independent checked append fd; caller owns it."""
    with Directory.open(data_home(), create=True, resolve_anchor=True) as root, root.child('logs', create=True) as logs:
        with logs.file('app.log', os.O_WRONLY | os.O_APPEND, create=True) as fd:
            return os.dup(fd)


def launch(root, argv):
    env = child_environment()
    os.environ.clear()
    os.environ.update(env)
    os.chdir(root)
    try:
        fd = open_log()
    except (OSError, RuntimeError):
        fd = os.open('/dev/null', os.O_WRONLY)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
    sys.argv = [str(Path(root) / 'bin/plaud-linux'), *argv]
    runpy.run_module('plaud_linux', run_name='__main__')


def entry(mode, source, argv):
    source = Path(source).resolve()
    root = source.parent if mode == 'install' else source.parent.parent
    if mode == 'launch':
        launch(root, argv)
    else:
        try:
            install(root, argv)
        except (OSError, ValueError, RuntimeError) as error:
            print(f'Não foi possível concluir a integração: {error}', file=sys.stderr)
            raise SystemExit(1)
        print('Integração do Plaud Linux concluída.')
