"""Live highlights: isolated recording data, blocked network and no capture."""
import os
import pathlib
import sys
import tempfile
import types
import unittest
import threading
from concurrent.futures import Future
from unittest.mock import patch

os.environ['PLAUD_LINUX_HOME'] = tempfile.mkdtemp(prefix='plaud-highlights-')
sys.path.insert(0, sys.argv.pop(1) if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
blocked = types.ModuleType('requests')
def no_network(*args, **kwargs):
    raise AssertionError('Unexpected network request')
blocked.get = blocked.post = blocked.put = blocked.request = no_network
sys.modules['requests'] = blocked
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GdkPixbuf, GLib, Gdk
from plaud_linux import panel, highlights, plaud_api

class Rec:
    def __init__(self):
        self.state = 'recording'
        self.notes, self.screenshots, self.flags = [], [], []
        self.saves = 0
    def elapsed(self): return 42
    def _save_meta(self): self.saves += 1
    def add_note(self, text, t):
        self.notes.append({'text': text, 't': t})
        self._save_meta()

class Highlights(unittest.TestCase):
    def setUp(self):
        (panel.paths.STATE / 'highlights-window.json').unlink(missing_ok=True)
        self.rec = Rec()
        self.menu = []
        self.overlay = types.SimpleNamespace(rec=self.rec, _on_kebab=self.menu.append)
        self.panel = panel.Panel(self.overlay)
    def tearDown(self):
        self.panel.destroy()
    def test_notes_appear_and_edits_persist_when_reopened(self):
        self.panel.note.set_text('Anotação de teste')
        self.panel._on_note_activate()
        note = self.rec.notes[0]
        editor = self.panel._entry_widgets[id(note)][1]
        editor.get_buffer().set_text('Texto editado\nOutra linha')
        self.panel.destroy()
        self.panel = panel.Panel(self.overlay)
        editor = self.panel._entry_widgets[id(note)][1]
        self.assertEqual(editor.get_buffer().get_text(*editor.get_buffer().get_bounds(), True), 'Texto editado\nOutra linha')
        self.assertGreater(self.rec.saves, 1)
    def test_capture_appears_and_delete_keeps_local_image(self):
        path = pathlib.Path(os.environ['PLAUD_LINUX_HOME']) / 'capture.png'
        pix = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 32, 32)
        pix.fill(0x4477AAFF)
        pix.savev(str(path), 'png', [], [])
        shot = {'path': str(path), 't': 10}
        self.rec.screenshots.append(shot)
        self.panel._sync()
        row = self.panel._entry_widgets[id(shot)][0]
        def images(widget):
            for child in widget.get_children() if isinstance(widget, Gtk.Container) else ():
                if isinstance(child, Gtk.Image):
                    yield child
                yield from images(child)
        previews = list(images(row))
        self.assertEqual(len(previews), 1)
        self.assertIsNotNone(previews[0].get_pixbuf())
        self.panel._delete_entry(None, 'shot', shot)
        self.assertFalse(self.rec.screenshots)
        self.assertTrue(path.exists())
        self.assertNotIn(id(shot), self.panel._entry_widgets)
    def test_flag_result_updates_inline_without_overwriting_user_edit(self):
        flag = {'t': 20, 'preview_status': 'Marcado. Analisando…'}
        self.rec.flags.append(flag)
        self.panel._sync()
        flag['text'] = 'Trecho reconhecido'
        self.panel._sync()
        editor = self.panel._entry_widgets[id(flag)][1]
        buffer = editor.get_buffer()
        self.assertEqual(buffer.get_text(*buffer.get_bounds(), True), 'Trecho reconhecido')
        self.assertNotIn('edited', flag)
        buffer.set_text('Minha correção')
        job = Future()
        job.set_result({'mark_content': 'Resultado tardio', 'mark_id': 'm'})
        highlights._apply(self.rec, flag, job)
        self.assertEqual(flag['text'], 'Minha correção')
        self.assertEqual(flag['cloud_mark']['mark_id'], 'm')
    def test_deleted_flag_is_not_restored_by_late_result(self):
        flag = {'t': 20}
        job = Future()
        job.set_result({'mark_content': 'Resultado tardio'})
        highlights._apply(self.rec, flag, job)
        self.assertEqual(self.rec.saves, 0)
        self.assertNotIn('cloud_mark', flag)
    def test_preview_worker_survives_panel_close_and_stop_joins_once(self):
        flag = {'t': 20, 'path': 'fake.ogg'}
        self.rec.flags.append(flag)
        release = threading.Event()
        entered = threading.Event()
        calls = []
        def prepare(_client, item):
            calls.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return {'mark_content': 'Trecho pronto', 'mark_id': 'm'}
        with patch.object(highlights.settings, 'get_option', return_value=True), patch.object(plaud_api.PlaudClient, 'is_logged_in', return_value=True), patch.object(plaud_api.PlaudClient, 'prepare_audio_mark', prepare):
            highlights.start(self.rec, flag)
            self.assertTrue(entered.wait(2))
            self.panel.destroy()
            self.rec.state = 'stopped'
            self.rec._flag_upload_owned = True
            release.set()
            highlights.finish_pending(self.rec)
            self.assertEqual(flag['text'], 'Trecho pronto')
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0], threading.get_ident())
            self.assertFalse(self.rec._flag_jobs)
    def test_cloud_disabled_does_not_start_preview(self):
        flag = {'t': 20}
        self.rec.flags.append(flag)
        with patch.object(highlights.settings, 'get_option', return_value=False), patch.object(plaud_api, 'PlaudClient', side_effect=AssertionError('Cloud disabled')):
            highlights.start(self.rec, flag)
        self.assertIn('enviar', flag['preview_status'])
        self.assertFalse(getattr(self.rec, '_flag_jobs', {}))
    def test_upload_reuses_mark_and_manual_text_without_another_request(self):
        mark = {'mark_type': 1, 'mark_id': 'm', 'mark_content': 'Original', 'picture_link': 'storage', 'timestamp': 20000}
        flag = {'cloud_mark': mark, 'edited': True, 'text': 'Correção'}
        calls = []
        client = plaud_api.PlaudClient()
        client._biz = lambda method, path, **kw: calls.append((path, kw['json'])) or {'status': 0}
        self.assertEqual(client.attach_screenshots('file', [], flags=[flag]), 1)
        self.assertEqual([path for path, _ in calls], ['/ai/update_source_info'])
        import json
        self.assertEqual(json.loads(calls[0][1]['source_content'])[0]['mark_content'], 'Correção')
        self.assertEqual(mark['mark_content'], 'Original')
    def test_failed_preview_preserves_flag_for_recovery(self):
        flag = {'path': 'local.ogg', 't': 20}
        self.rec.flags.append(flag)
        job = Future()
        job.set_exception(RuntimeError('Failed'))
        highlights._apply(self.rec, flag, job)
        self.assertEqual(flag['path'], 'local.ogg')
        self.assertIn('salvo', flag['preview_status'])
        self.assertEqual(self.rec.flags, [flag])
        with patch.object(highlights.settings, 'get_option', return_value=True), patch.object(highlights.threading.Thread, 'start', side_effect=RuntimeError('Cannot start')):
            highlights.start(self.rec, flag)
        self.assertFalse(self.rec._flag_jobs)
        self.assertIn('salvo', flag['preview_status'])
    def test_edge_handles_request_native_resize(self):
        self.assertEqual(len(self.panel._resize_handles), 8)
        event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        event.button, event.x_root, event.y_root, event.time = 1, 120, 240, 123
        with patch.object(self.panel, 'begin_resize_drag') as resize:
            for edge, handle in self.panel._resize_handles.items():
                handle.emit('button-press-event', event)
                resize.assert_called_with(edge, 1, 120, 240, 123)
            event.button = 3
            self.panel._resize_from_edge(None, event, Gdk.WindowEdge.EAST)
            self.assertEqual(resize.call_count, 8)

    def test_resized_dimensions_survive_panel_reopen(self):
        self.panel._geometry_ready = True
        allocation = Gdk.Rectangle()
        allocation.x = allocation.y = 0
        allocation.width, allocation.height = 720, 640
        # Hyprland can retain GTK maximized state after floating the window.
        with patch.object(self.panel, "is_maximized", return_value=True):
            self.panel.size_allocate(allocation)
        self.panel.destroy()
        self.panel = panel.Panel(self.overlay)
        self.assertEqual(self.panel._initial_size, (720, 640))
        self.assertEqual(self.panel.get_default_size(), (720, 640))

    def test_initial_tiled_size_does_not_overwrite_saved_dimensions(self):
        self.panel._on_size_allocate(None, types.SimpleNamespace(width=1900, height=1000))
        self.panel.destroy()
        self.assertEqual(panel.Panel._load_window_size(), (420, 360))
        self.assertIsNone(self.panel._float_id)
        self.assertIsNone(self.panel._restore_id)

    def test_compositor_placement_does_not_resize_another_process(self):
        import json
        clients = [
            {'pid': os.getpid() + 1, 'class': panel.WMCLASS, 'title': self.panel.get_title(), 'mapped': True, 'address': '0x111'},
            {'pid': os.getpid(), 'class': panel.WMCLASS, 'title': self.panel.get_title(), 'mapped': True, 'address': '0x222'}]
        if self.panel._float_id is not None:
            GLib.source_remove(self.panel._float_id)
            self.panel._float_id = None
        with patch.object(panel.subprocess, 'run', return_value=types.SimpleNamespace(stdout=json.dumps(clients))) as run:
            self.panel._float()
            lua = run.call_args.args[0][2]
            self.assertIn('address:0x222', lua)
            self.assertNotIn('0x111', lua)
            self.assertNotIn('class:', lua)

    def test_invalid_saved_dimensions_use_defaults(self):
        import json
        path = panel.paths.STATE / 'highlights-window.json'
        for value in ({'width': -1, 'height': 400}, {'width': '800', 'height': 600}, [], None):
            path.write_text(json.dumps(value))
            self.assertEqual(panel.Panel._load_window_size(), (420, 360))

    def test_screenshot_grows_and_shrinks_with_available_width(self):
        path = pathlib.Path(os.environ['PLAUD_LINUX_HOME']) / 'wide.png'
        pix = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 1000, 500)
        pix.fill(0x4477AAFF)
        pix.savev(str(path), 'png', [], [])
        preview = panel._Screenshot(str(path))
        preview._rescale(preview, types.SimpleNamespace(width=800))
        self.assertEqual((preview.get_pixbuf().get_width(), preview.get_pixbuf().get_height()), (180, 90))
        preview._rescale(preview, types.SimpleNamespace(width=100))
        self.assertEqual((preview.get_pixbuf().get_width(), preview.get_pixbuf().get_height()), (100, 50))
        preview._rescale(preview, types.SimpleNamespace(width=800))
        self.assertEqual((preview.get_pixbuf().get_width(), preview.get_pixbuf().get_height()), (180, 90))
        self.assertEqual(preview.do_get_preferred_width()[0], 1)
        preview.destroy()

    def test_menu_uses_existing_discard_confirmation(self):
        self.panel._on_menu()
        self.assertEqual(self.menu, [self.panel.btn_menu])

if __name__ == '__main__':
    class Result(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            print(f'PASS {test._testMethodName}')
        def addFailure(self, test, err):
            super().addFailure(test, err)
            print(f'FAIL {test._testMethodName}')
        def addError(self, test, err):
            super().addError(test, err)
            print(f'FAIL {test._testMethodName}')
    result = unittest.TextTestRunner(resultclass=Result).run(unittest.defaultTestLoader.loadTestsFromTestCase(Highlights))
    sys.exit(not result.wasSuccessful())
