"""Native desktop lifecycle regressions; isolated state, no audio or network."""
import os
import pathlib
import sys
import tempfile
import types
import unittest

os.environ['PLAUD_LINUX_HOME'] = tempfile.mkdtemp(prefix='plaud-flow-')
os.environ['PLAUD_LINUX_AUTOSTART_DIR'] = tempfile.mkdtemp(prefix='plaud-autostart-')
sys.path.insert(0, sys.argv.pop(1) if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
blocked = types.ModuleType('requests')
def no_network(*args, **kwargs):
    raise AssertionError('A desktop regression check must not call the network')
blocked.get = blocked.post = blocked.put = blocked.request = no_network
blocked.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules['requests'] = blocked

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
from plaud_linux import tray, main, library, paths, plaud_api

class DesktopFlow(unittest.TestCase):
    def setUp(self):
        self.original_start = main.start_session
        self.starts = []
        main.start_session = lambda **kw: self.starts.append(kw) or False
        tray._busy = tray._started = False
        tray._overlay = tray._standby = None

    def tearDown(self):
        main.start_session = self.original_start
        if tray._standby is not None:
            tray._standby.destroy()
        tray._standby = None
        for window in Gtk.Window.list_toplevels():
            window.destroy()

    def test_open_and_reopen_never_capture(self):
        tray._show()
        first = tray._standby
        tray._show()
        self.assertIs(tray._standby, first)
        self.assertFalse(self.starts)
        self.assertTrue(first.get_visible())

    def test_reopening_closes_context_menu_without_recording(self):
        original = tray._indicator
        try:
            window = Gtk.Window()
            window.show_all()
            tray._indicator = types.SimpleNamespace(_context_window=window)
            tray._show()
            self.assertFalse(window.get_visible())
            self.assertFalse(self.starts)
        finally:
            tray._indicator = original

    def test_only_start_button_requests_recording(self):
        tray._show()
        tray._standby.start.clicked()
        self.assertEqual(len(self.starts), 1)
        self.assertFalse(tray._busy)
        self.assertTrue(tray._standby.get_visible())

    def test_busy_state_blocks_duplicate_recording(self):
        tray._show()
        tray._standby.set_busy(True, 'Enviando áudio…')
        self.assertFalse(tray._standby.start.get_sensitive())
        self.assertTrue(tray._standby.upload.get_visible())
        tray._standby.set_busy(False)
        self.assertIsNone(tray._standby._pulse_id)
        self.assertFalse(tray._standby.upload.get_visible())

    def test_recent_button_has_visible_recording_name(self):
        p = paths.RECORDINGS / 'flow-note.json'
        p.write_text('{"session":"Aula de teste","upload":{"ok":true,"file_id":"test"}}')
        try:
            tray._show()
            self.assertFalse(tray._standby.recent.get_expanded())
            row = tray._standby.rows.get_children()[0]
            self.assertEqual(row.get_child().get_text(), 'Aula de teste')
            tray._standby.recent.activate()
            self.assertTrue(tray._standby.recent.get_expanded())
            tray._standby.refresh()
            self.assertTrue(tray._standby.recent.get_expanded())
            self.assertEqual(tray._standby.rows.get_children()[0].get_child().get_text(),
                             'Aula de teste')
        finally:
            p.unlink()

    def test_header_actions_are_visible(self):
        tray._show()
        header = tray._standby.get_child().get_children()[0]
        buttons = [w for w in header.get_children() if isinstance(w, Gtk.Button)]
        self.assertEqual(len(buttons), 3)
        self.assertTrue(all(w.get_always_show_image() for w in buttons))

    def test_generation_dialog_uses_native_responses(self):
        from plaud_linux import desktop_ui
        dlg = desktop_ui.generation_dialog()
        self.assertIn('automaticamente', dlg.get_widget_for_response(Gtk.ResponseType.YES).get_label())
        self.assertIn('personalizada', dlg.get_widget_for_response(Gtk.ResponseType.NO).get_label())
        self.assertIs(dlg.get_default_widget(), dlg.get_widget_for_response(Gtk.ResponseType.YES))

    def test_generation_completion_and_rejection_are_distinct(self):
        client = plaud_api.PlaudClient()
        self.assertEqual(client.wait_for_generation('test', {'status': 1}), {'status': 1})
        with self.assertRaises(RuntimeError):
            client.wait_for_generation('test', {'status': -7, 'msg': 'transcript empty'})
        with self.assertRaises(TimeoutError):
            client.wait_for_generation('test', {'status': 0}, timeout=0)

    def test_autostart_is_background_only(self):
        from plaud_linux import settings
        settings.set_autostart(True)
        self.assertIn('--background', pathlib.Path(settings._AUTOSTART_FILE).read_text())
        settings.set_autostart(False)
        self.assertFalse(settings.autostart_enabled())

    def test_recording_sources_follow_preferences(self):
        from plaud_linux import settings
        try:
            settings.set_mic_device(settings.MIC_OFF)
            settings.set_system_audio_enabled(True)
            self.assertEqual(main.pick_sources(), (True, False))
            settings.set_mic_device(settings.MIC_AUTOMATIC)
            settings.set_system_audio_enabled(False)
            self.assertEqual(main.pick_sources(), (False, True))
        finally:
            settings.set_mic_device(settings.MIC_AUTOMATIC)
            settings.set_system_audio_enabled(True)

    def test_selected_microphone_is_used(self):
        from plaud_linux import audio
        rec = audio.Recorder()
        rec.mic_device = 'selected-input'
        original = audio.default_source
        audio.default_source = lambda: 'wrong-default'
        try:
            self.assertEqual(rec._resolve_src()[2], 'selected-input')
        finally:
            audio.default_source = original

    def test_settings_fit_reference_width(self):
        from plaud_linux import settings
        window = settings.SettingsWindow()
        window.show_all()
        self.assertLessEqual(window.get_preferred_width()[0], settings.WINDOW_WIDTH)

    def test_pending_generation_is_polled_until_complete(self):
        client = plaud_api.PlaudClient()
        calls = []
        client.generate = lambda fid: calls.append(fid) or {"status": 1}
        result = client.wait_for_generation('test', {"status": 0}, timeout=5)
        self.assertEqual(result["status"], 1)
        self.assertEqual(calls, ['test'])

    def test_cloud_sync_off_preserves_local_audio(self):
        from plaud_linux import settings
        p = paths.RECORDINGS / 'preserved.opus'
        p.write_bytes(b'original audio')
        settings.set_option('cloud_sync', False)
        done = []
        original = main._do_upload
        main._do_upload = no_network
        original_notify = main.notify
        main.notify = lambda *args: None
        try:
            main.on_stop(types.SimpleNamespace(final_path=p), lambda: done.append(True))
            self.assertEqual(done, [True])
            self.assertEqual(p.read_bytes(), b'original audio')
        finally:
            main._do_upload = original
            main.notify = original_notify
            settings.set_option('cloud_sync', True)
            p.unlink()

if __name__ == '__main__':
    class Result(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            print(f"PASS {test._testMethodName}")
        def addFailure(self, test, err):
            super().addFailure(test, err)
            print(f"FAIL {test._testMethodName}")
        def addError(self, test, err):
            super().addError(test, err)
            print(f"FAIL {test._testMethodName}")
    result = unittest.TextTestRunner(resultclass=Result).run(unittest.defaultTestLoader.loadTestsFromTestCase(DesktopFlow))
    sys.exit(not result.wasSuccessful())
