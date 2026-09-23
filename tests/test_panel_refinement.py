"""Keyboard and allocated preview regressions with synthetic media only.

Run with a separate D-Bus session. The fake recorder never captures audio and
network calls are blocked before importing the application.
"""
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

MANUAL = "--manual" in sys.argv
if MANUAL:
    sys.argv.remove("--manual")
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp(prefix="plaud-panel-refinement-")
sys.path.insert(0, sys.argv.pop(1) if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
blocked = types.ModuleType("requests")


def no_network(*_args, **_kwargs):
    raise AssertionError("Unexpected network request")


blocked.get = blocked.post = blocked.put = blocked.request = no_network
sys.modules["requests"] = blocked

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf  # noqa: E402
from plaud_linux import panel  # noqa: E402


class FakeRec:
    def __init__(self):
        self.state = "recording"
        self.notes, self.screenshots, self.flags = [], [], []
        self.saves = 0

    def elapsed(self):
        return 42

    def add_note(self, text, elapsed):
        self.notes.append({"text": text, "t": elapsed})
        self._save_meta()

    def _save_meta(self):
        self.saves += 1


def settle():
    for _ in range(40):
        if Gtk.events_pending():
            Gtk.main_iteration_do(False)


def key(widget, keyval, state=0):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    event.state = state
    event.window = widget.get_toplevel().get_window()
    event.send_event = True
    event.device = Gdk.Display.get_default().get_default_seat().get_keyboard()
    return widget.emit("key-press-event", event)


class PanelRefinement(unittest.TestCase):
    def setUp(self):
        self.rec = FakeRec()
        self.overlay = types.SimpleNamespace(rec=self.rec)
        # Avoid external compositor commands in this focused fake-recorder test.
        self.map_patch = patch.object(panel.Panel, "_on_map", return_value=False)
        self.map_patch.start()
        self.panel = panel.Panel(self.overlay)
        settle()

    def tearDown(self):
        self.panel.destroy()
        self.map_patch.stop()
        settle()

    def _add_note(self, text):
        self.panel.note.grab_focus()
        self.panel.note.set_text(text)
        self.panel.note.emit("activate")
        settle()

    def _add_shot(self, width, height):
        path = pathlib.Path(os.environ["PLAUD_LINUX_HOME"]) / f"synthetic-{width}x{height}.png"
        pix = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
        pix.fill(0x4477AAFF)
        pix.savev(str(path), "png", [], [])
        original = path.read_bytes()
        shot = {"path": str(path), "t": 12}
        self.rec.screenshots.append(shot)
        self.panel._sync()
        settle()
        return path, original, shot

    def test_two_enter_notes_and_existing_editor_completion(self):
        self._add_note("Primeira")
        self.assertEqual(self.panel.note.get_text(), "")
        self.assertTrue(self.panel.note.is_focus())
        self._add_note("Segunda")
        self.assertEqual([(n["text"], n["t"]) for n in self.rec.notes],
                         [("Primeira", 42), ("Segunda", 42)])

        first = self.rec.notes[0]
        editor = self.panel._entry_widgets[id(first)][1]
        editor.grab_focus()
        editor.get_buffer().set_text("Primeira editada")
        self.assertTrue(key(editor, Gdk.KEY_Return))
        self.assertEqual(first["text"], "Primeira editada")
        self.assertTrue(self.panel.note.is_focus())
        self.assertEqual(self.panel.note.get_text(), "")
        self.assertEqual(len(self.rec.notes), 2)

        self._add_note("   ")
        self.assertEqual(len(self.rec.notes), 2)

    def test_shift_enter_keeps_multiline_editor(self):
        self._add_note("Rascunho")
        note = self.rec.notes[0]
        editor = self.panel._entry_widgets[id(note)][1]
        editor.grab_focus()
        editor.get_buffer().place_cursor(editor.get_buffer().get_end_iter())
        # The connected handler must let GTK's TextView handle Shift+Enter.
        key(editor, Gdk.KEY_Return, Gdk.ModifierType.SHIFT_MASK)
        self.assertEqual(note["text"], "Rascunho\n")
        self.assertTrue(editor.is_focus())

    def test_next_note_field_remains_visible_in_long_list(self):
        for number in range(12):
            self._add_note(f"Nota {number}")
        scroll = self.panel._entries_scroll
        _, note_y = self.panel.note.translate_coordinates(scroll, 0, 0)
        self.assertGreaterEqual(note_y, 0)
        self.assertLessEqual(note_y + self.panel.note.get_allocation().height,
                             scroll.get_allocation().height)
        self.assertTrue(self.panel.note.is_focus())
        self.assertEqual(len(self.rec.notes), 12)

    def test_portrait_and_landscape_previews_fit_with_visible_delete(self):
        for width, height in ((900, 3000), (3000, 900)):
            with self.subTest(size=(width, height)):
                path, original, shot = self._add_shot(width, height)
                row = self.panel._entry_widgets[id(shot)][0]
                header, picture = row.get_children()
                image, remove = picture.get_children()
                self.assertIsInstance(image, Gtk.Image)
                self.assertIsInstance(remove, Gtk.Button)
                self.assertEqual(len(header.get_children()), 1)
                preview = image.get_pixbuf()
                self.assertLessEqual(preview.get_height(), 90)
                self.assertLessEqual(preview.get_width(), row.get_allocation().width)
                self.assertLessEqual(image.get_allocation().height, 90)
                self.assertEqual(image.get_alignment()[0], 0.0)
                self.assertLessEqual(picture.get_allocation().width, row.get_allocation().width)
                self.assertLessEqual(remove.get_allocation().x + remove.get_allocation().width,
                                     picture.get_allocation().width)
                self.assertGreaterEqual(remove.get_allocation().x - preview.get_width(), 0)
                self.assertLessEqual(remove.get_allocation().x - preview.get_width(), 8)
                self.assertLessEqual(row.get_allocation().width,
                                     self.panel.get_allocation().width)
                horizontal = self.panel._entries_scroll.get_hadjustment()
                self.assertLessEqual(horizontal.get_upper() - horizontal.get_page_size(), 1)
                self.assertAlmostEqual(preview.get_width() / preview.get_height(),
                                       width / height, delta=0.08)
                self.panel.note.grab_focus()
                settle()
                _, header_y = header.translate_coordinates(self.panel._entries_scroll, 0, 0)
                self.assertGreaterEqual(header_y, 0)
                self.assertLessEqual(header_y + header.get_allocation().height,
                                     self.panel._entries_scroll.get_allocation().height)
                self.assertEqual(path.read_bytes(), original)
                remove.clicked()
                settle()
                self.assertNotIn(shot, self.rec.screenshots)
                self.assertTrue(path.exists())

    def test_narrow_preview_does_not_request_horizontal_overflow(self):
        path, original, shot = self._add_shot(5000, 700)
        picture = self.panel._entry_widgets[id(shot)][0].get_children()[1]
        image, remove = picture.get_children()
        # Force the compact image/button group through a narrow allocation.
        rect = Gdk.Rectangle()
        rect.x = rect.y = 0
        rect.width, rect.height = 268, 90
        picture.size_allocate(rect)
        # Inspect this allocation before the mapped parent schedules its own
        # compositor-sized reallocation.
        self.assertLessEqual(image.get_pixbuf().get_width() + remove.get_allocation().width + 4, 268)
        self.assertLessEqual(image.get_pixbuf().get_height(), 90)
        self.assertLessEqual(picture.get_preferred_width()[0], 268)
        self.assertEqual(path.read_bytes(), original)


def manual_preview():
    """Open a normal panel with fake notes/images for physical visual inspection."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size):
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            return ImageFont.load_default()

    home = pathlib.Path(os.environ["PLAUD_LINUX_HOME"])
    rec = FakeRec()
    rec.state = "paused"  # Screenshot/flag buttons stay disabled; no capture exists.
    rec.notes.append({"text": "Nota de teste — edite aqui e pressione Enter", "t": 12})
    for width, height, title in ((1440, 900, "SLIDE HORIZONTAL"),
                                 (900, 1600, "SLIDE VERTICAL")):
        path = home / f"manual-{width}x{height}.png"
        image = Image.new("RGB", (width, height), "#f5f7fb")
        draw = ImageDraw.Draw(image)
        step = max(100, width // 8)
        for x in range(0, width, step):
            draw.line((x, 0, x, height), fill="#d6dfec", width=3)
        for y in range(0, height, step):
            draw.line((0, y, width, y), fill="#d6dfec", width=3)
        draw.rectangle((0, 0, width, height // 4), fill="#163f79")
        draw.text((width // 16, height // 12), title, font=font(max(50, width // 15)), fill="white")
        inset = width // 12
        for index, colour in enumerate(("#f2ad4e", "#42aeb2", "#8e77c7")):
            top = height // 3 + index * (height // 6)
            draw.rounded_rectangle((inset, top, width - inset, top + height // 9),
                                   radius=18, fill=colour)
            draw.text((inset + 24, top + 10), f"{index + 1}  Quadro de exemplo",
                      font=font(max(28, width // 25)), fill="#14243a")
        image.save(path)
        rec.screenshots.append({"path": str(path), "t": 13 if height > width else 14})
    overlay = types.SimpleNamespace(rec=rec, _on_pause=lambda: None,
                                    _on_stop=lambda: None, _on_shot=lambda: None,
                                    _on_kebab=lambda *_: None)
    window = panel.Panel(overlay)
    window._initial_size = (680, 640)  # Scratch home: never changes the user's saved size.
    window.resize(680, 640)
    window.connect("destroy", lambda *_: Gtk.main_quit())
    window.present()
    print("PAINEL SINTÉTICO: editar nota, Enter/Shift+Enter, conferir miniaturas e ×; feche a janela para sair.", flush=True)
    Gtk.main()


if __name__ == "__main__":
    if MANUAL:
        manual_preview()
    else:
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

        result = unittest.TextTestRunner(resultclass=Result).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(PanelRefinement))
        sys.exit(not result.wasSuccessful())
