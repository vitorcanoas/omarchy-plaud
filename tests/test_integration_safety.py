"""Isolated filesystem/child-process checks; no GUI or account access."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
from plaud_linux import integration as api


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='plaud-integration-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / 'source'
        (self.root / 'assets').mkdir(parents=True)
        (self.root / 'assets/plaud-linux.png').write_bytes(b'icon fixture')
        (self.root / 'bin').mkdir()
        self.launcher = self.root / 'bin/plaud-linux'
        self.launcher.write_text('fixture')
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'PLAUD_LINUX_HOME': str(self.home / 'data')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.net = mock.patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden'))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.auto = self.home / '.config/autostart'
        self.auto.mkdir(parents=True)

    def install(self, args=()):
        api.install(self.root, args, home=self.home, commands=False)

    def test_install_and_autostart_roundtrip(self):
        self.install(['--autostart'])
        self.assertEqual((self.home / '.local/bin/plaud-linux').resolve(), self.launcher)
        self.assertTrue(api.autostart_enabled(self.auto))
        api.set_autostart(False, self.launcher, self.auto)
        self.assertFalse((self.auto / 'plaud-linux.desktop').exists())

    def test_autostart_symlink_preserves_target(self):
        sentinel = self.home / 'recording.opus'
        sentinel.write_bytes(b'recorded audio')
        (self.auto / 'plaud-linux.desktop').symlink_to(sentinel)
        with self.assertRaises(OSError):
            api.set_autostart(True, self.launcher, self.auto)
        self.assertEqual(sentinel.read_bytes(), b'recorded audio')

    def test_autostart_fifo_refused(self):
        os.mkfifo(self.auto / 'plaud-linux.desktop')
        with self.assertRaises(OSError):
            api.set_autostart(True, self.launcher, self.auto)

    def test_autostart_hardlink_preserves_target(self):
        sentinel = self.home / 'recording.opus'
        sentinel.write_bytes(b'audio')
        os.link(sentinel, self.auto / 'plaud-linux.desktop')
        with self.assertRaises(OSError):
            api.set_autostart(True, self.launcher, self.auto)
        self.assertEqual(sentinel.read_bytes(), b'audio')

    def test_unknown_desktop_preserved(self):
        target = self.auto / 'plaud-linux.desktop'
        target.write_bytes(b'[Desktop Entry]\nName=Other\nExec=/bin/true\n')
        with self.assertRaises(OSError):
            api.set_autostart(True, self.launcher, self.auto)
        self.assertIn(b'Name=Other', target.read_bytes())

    def test_shortcuts_roundtrip_exact_bytes(self):
        folder = self.home / '.config/hypr'
        folder.mkdir()
        target = folder / 'bindings.lua'
        original = b'-- user bindings\n\n\n-- keep these\n\n'
        target.write_bytes(original)
        api.change_shortcuts(self.home, self.launcher, commands=False)
        self.assertIn(api.BEGIN.encode(), target.read_bytes())
        api.change_shortcuts(self.home, self.launcher, remove=True, commands=False)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(list(folder.glob('bindings.lua.bak.*')))

    def test_remove_does_not_install(self):
        self.install(['--remove-hypr-shortcuts'])
        self.assertFalse((self.home / '.local').exists())

    def test_malformed_markers_preserved(self):
        original = (api.BEGIN + '\nuser content\n').encode()
        with self.assertRaises(OSError):
            api.shortcut_content(original, self.launcher, remove=True)

    def test_unsupported_checkout_fails_before_writes(self):
        with self.assertRaises(OSError):
            api.install(self.home / 'bad name', [], home=self.home, commands=False)
        self.assertFalse((self.home / '.local').exists())

    def test_settings_special_and_invalid_files(self):
        state = self.home / 'data/state'
        state.mkdir(parents=True)
        path = state / 'settings.json'
        path.write_text('[]')
        self.assertEqual(api.read_settings(path), {})
        path.write_bytes(b'x' * (api.LIMIT + 1))
        self.assertEqual(api.read_settings(path), {})
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(api.read_settings(path), {})

    def test_settings_atomic_roundtrip(self):
        path = self.home / 'data/state/settings.json'
        api.write_settings(path, {'language': 'pt', 'cloud_sync': False})
        self.assertEqual(api.read_settings(path), {'language': 'pt', 'cloud_sync': False})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_environment_keeps_session_drops_startup_hooks(self):
        env = api.child_environment({'HOME': '/home/a', 'WAYLAND_DISPLAY': 'wayland-1',
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=fixture', 'PLAUD_LINUX_HOME': '/tmp/data',
            'PYTHONPATH': '/evil', 'BASH_ENV': '/evil', 'LD_PRELOAD': '/evil',
            'HTTPS_PROXY': 'http://proxy.invalid'})
        self.assertEqual(env['WAYLAND_DISPLAY'], 'wayland-1')
        self.assertEqual(env['PLAUD_LINUX_HOME'], '/tmp/data')
        self.assertIn('HTTPS_PROXY', env)
        self.assertFalse({'PYTHONPATH', 'BASH_ENV', 'LD_PRELOAD'} & env.keys())

    def test_log_respects_data_override(self):
        fd = api.open_log()
        os.write(fd, b'diagnostic')
        os.close(fd)
        self.assertEqual((self.home / 'data/logs/app.log').read_bytes(), b'diagnostic')
        self.assertFalse((self.home / '.local/share/plaud-linux').exists())

    def test_log_symlink_does_not_append_audio(self):
        logs = self.home / 'data/logs'
        logs.mkdir(parents=True)
        audio = self.home / 'recording.opus'
        audio.write_bytes(b'audio')
        (logs / 'app.log').symlink_to(audio)
        with self.assertRaises(OSError):
            api.open_log()
        self.assertEqual(audio.read_bytes(), b'audio')

    def test_disabling_missing_autostart_is_noop(self):
        api.set_autostart(False, self.launcher, self.home / 'absent/autostart')
        self.assertFalse((self.home / 'absent').exists())

    def test_settings_state_link_is_refused(self):
        data = self.home / 'data'
        data.mkdir()
        other = self.home / 'other'
        other.mkdir()
        target = other / 'settings.json'
        target.write_bytes(b'{}')
        (data / 'state').symlink_to(other)
        with self.assertRaises(OSError):
            api.write_settings(data / 'state/settings.json', {'language': 'en'})
        self.assertEqual(target.read_bytes(), b'{}')

    def bootstrap(self, unsafe_log=False):
        real_root = Path(api.__file__).parent.parent
        self.launcher.write_bytes((real_root / 'bin/plaud-linux').read_bytes())
        self.launcher.chmod(0o755)
        package = self.root / 'plaud_linux'
        package.mkdir()
        (package / '__init__.py').write_text('')
        for name in ('integration.py', 'safe_io.py'):
            (package / name).write_bytes((real_root / 'plaud_linux' / name).read_bytes())
        result = self.home / 'result.json'
        (package / '__main__.py').write_text(
            'import os, sys, json, pathlib\n'
            + 'pathlib.Path(' + repr(str(result)) + ').write_text(json.dumps({"args":sys.argv[1:], '
            + '"display":os.environ.get("WAYLAND_DISPLAY"), "pythonpath":os.environ.get("PYTHONPATH")}))\n'
            + 'print("fixture launched")\n')
        env = dict(os.environ, WAYLAND_DISPLAY='fixture-display')
        hook = self.home / 'hook'
        forbidden = self.home / 'hook-executed'
        hook.write_text('touch ' + str(forbidden))
        env.update(BASH_ENV=str(hook), PYTHONPATH=str(self.home / 'evil'))
        (self.home / 'evil').mkdir()
        (self.home / 'evil/sitecustomize.py').write_text(
            'import pathlib; pathlib.Path(' + repr(str(forbidden)) + ').touch()')
        sentinel = self.home / 'audio.opus'
        sentinel.write_bytes(b'preserve')
        if unsafe_log:
            logs = self.home / 'data/logs'
            logs.mkdir(parents=True)
            (logs / 'app.log').symlink_to(sentinel)
        proc = subprocess.run([str(self.launcher), '--background', 'argument with spaces'],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(forbidden.exists())
        import json
        payload = json.loads(result.read_text())
        self.assertEqual(payload['args'], ['--background', 'argument with spaces'])
        self.assertEqual(payload['display'], 'fixture-display')
        self.assertIsNone(payload['pythonpath'])
        self.assertEqual(sentinel.read_bytes(), b'preserve')

    def test_real_bootstrap_preserves_argv_and_closes_environment(self):
        self.bootstrap()
        self.assertIn(b'fixture launched', (self.home / 'data/logs/app.log').read_bytes())

    def test_real_bootstrap_unsafe_log_still_launches(self):
        self.bootstrap(unsafe_log=True)

    def test_tool_output_is_bounded(self):
        with self.assertRaises(OSError):
            api.run_tool(['/usr/bin/python3', '-I', '-c', 'print("x" * 100000)'], limit=1024)

    def test_tool_deadline(self):
        with self.assertRaises(TimeoutError):
            api.run_tool(['/usr/bin/python3', '-I', '-c', 'import time; time.sleep(30)'], timeout=.1)


class Result(unittest.TextTestResult):
    def addSuccess(self, test):
        super().addSuccess(test)
        print('PASS ' + test.id().rsplit('.', 1)[-1])
    def addFailure(self, test, err):
        super().addFailure(test, err)
        print('FAIL ' + test.id().rsplit('.', 1)[-1])
    def addError(self, test, err):
        super().addError(test, err)
        print('FAIL ' + test.id().rsplit('.', 1)[-1])


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IntegrationTests)
    result = unittest.TextTestRunner(resultclass=Result).run(suite)
    sys.exit(not result.wasSuccessful())
