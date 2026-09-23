"""Native standby and upload cards, matching the user's Desktop references."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

try:
    from . import library
except ImportError:
    import library

CSS = b"""
.plaud-card { background: #fff; color: #111; border-radius: 10px;
              border: 1px solid #ebebeb; font-size: 13px; }
.plaud-card label { color: #111; }
.plaud-card button { background: #fff; color: #111; border: 1px solid #d6d6d6;
                     border-radius: 5px; box-shadow: none; padding: 7px 10px; }
.plaud-card button:hover { background: #eee; }
.plaud-card button.primary { background: #000; color: #fff; border-color: #000; }
.plaud-card button.primary label { color: #fff; }
.plaud-card button.primary:hover { background: #333; }
.plaud-card button:disabled { opacity: 0.5; }
.plaud-card .muted { color: #7a7a7a; font-size: 12px; }
.plaud-card .heading { font-size: 18px; }
.plaud-card .flat { border: none; padding: 4px; }
"""
_screens = set()


def prepare(window, width=280, namespace="plaud-standby"):
    """Use the work area's top-right on Wayland; retain a visible fallback."""
    window.set_title("Plaud Linux")
    window.set_icon_name("plaud-linux")
    window.set_default_size(width, -1)
    window.set_resizable(False)
    screen = window.get_screen()
    if hash(screen) not in _screens:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _screens.add(hash(screen))
    window.get_style_context().add_class("plaud-card")
    try:
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import GtkLayerShell as Layer
        if Layer.is_supported():
            window.set_decorated(False)
            Layer.init_for_window(window)
            Layer.set_namespace(window, namespace)
            Layer.set_layer(window, Layer.Layer.OVERLAY)
            Layer.set_anchor(window, Layer.Edge.TOP, True)
            Layer.set_anchor(window, Layer.Edge.RIGHT, True)
            Layer.set_margin(window, Layer.Edge.TOP, 20)
            Layer.set_margin(window, Layer.Edge.RIGHT, 10)
            Layer.set_keyboard_mode(window, Layer.KeyboardMode.ON_DEMAND)
            Layer.set_exclusive_zone(window, 0)
    except (ImportError, ValueError):
        pass


def button(label, callback, primary=False):
    widget = Gtk.Button(label=label)
    if primary:
        widget.get_style_context().add_class("primary")
    widget.connect("clicked", callback)
    widget.connect("realize", lambda w: w.get_window().set_cursor(
        Gdk.Cursor.new_from_name(w.get_display(), "pointer")))
    return widget


def label(text, muted=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    widget.set_max_width_chars(36)
    if muted:
        widget.get_style_context().add_class("muted")
    return widget


class Standby(Gtk.Window):
    def __init__(self, on_start, on_settings, on_library, on_open, on_close=None):
        super().__init__()
        prepare(self)
        self._on_open = on_open
        close = on_close or self.hide
        self.connect("delete-event", lambda *_: close() or True)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_border_width(16)
        self.add(root)
        header = Gtk.Box(spacing=6)
        logo = Gtk.Image.new_from_icon_name("plaud-linux", Gtk.IconSize.LARGE_TOOLBAR)
        header.pack_start(logo, True, True, 0)
        logo.set_pixel_size(20)
        logo.set_halign(Gtk.Align.START)
        for icon, tip, action in (("folder-symbolic", "Envios recentes", on_library),
                                  ("emblem-system-symbolic", "Preferências", on_settings),
                                  ("window-close-symbolic", "Ocultar painel", close)):
            b = button("", lambda _, cb=action: cb())
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU))
            b.set_always_show_image(True)
            b.set_tooltip_text(tip)
            b.get_style_context().add_class("flat")
            header.pack_start(b, False, False, 0)
        root.pack_start(header, False, False, 0)
        root.pack_start(Gtk.Separator(), False, False, 0)
        self.start = button("Iniciar gravação", lambda _: on_start(), True)
        root.pack_start(self.start, False, False, 0)
        self.upload = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.upload.pack_start(label("Fila de envio", True), False, False, 0)
        self.status = label("")
        self.upload.pack_start(self.status, False, False, 0)
        self.bar = Gtk.ProgressBar()
        self.upload.pack_start(self.bar, False, False, 0)
        root.pack_start(self.upload, False, False, 0)
        self.upload.set_no_show_all(True)
        recent = Gtk.Expander(label="Envios recentes")
        recent.set_expanded(False)
        self.recent = recent
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        recent.add(self.rows)
        root.pack_start(recent, False, False, 0)
        self._pulse_id = None
        self.connect("destroy", lambda *_: self.set_busy(False))

    def refresh(self):
        for child in self.rows.get_children():
            child.destroy()
        entries = library.uploaded()[:5]
        for entry in entries:
            b = button("", lambda _, fid=entry.file_id: self._on_open(fid))
            name = Gtk.Label(label=entry.meta.get("title") or entry.session, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(28)
            b.remove(b.get_child())
            b.add(name)
            self.rows.pack_start(b, False, False, 0)
        if not entries:
            self.rows.pack_start(label("Nenhum envio recente", True), False, False, 0)
        self.rows.show_all()

    def set_busy(self, busy, text="Preparando áudio para envio…"):
        self.start.set_sensitive(not busy)
        if busy:
            self.status.set_text(text)
            self.upload.show()
            for child in self.upload.get_children():
                child.show()
            if self._pulse_id is None:
                self._pulse_id = GLib.timeout_add(120, self._pulse)
        else:
            self.upload.hide()
            if self._pulse_id is not None:
                GLib.source_remove(self._pulse_id)
                self._pulse_id = None

    def _pulse(self):
        self.bar.pulse()
        return True


def generation_dialog():
    dlg = Gtk.Dialog(title="Pronto para gerar — Plaud")
    prepare(dlg, 320, "plaud-generation")
    dlg.set_title("Pronto para gerar — Plaud")
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(16)
    heading = label("Pronto para gerar")
    heading.get_style_context().add_class("heading")
    box.pack_start(heading, False, False, 0)
    box.pack_start(label("Escolha como pretende gerar esta nota.", True), False, False, 0)
    auto = Gtk.Button(label="✦  Gerar automaticamente")
    auto.get_style_context().add_class("primary")
    for widget, response in ((auto, Gtk.ResponseType.YES),
                             (Gtk.Button(label="Gerar personalizada"), Gtk.ResponseType.NO),
                             (Gtk.Button(label="Agora não"), Gtk.ResponseType.CANCEL)):
        dlg.add_action_widget(widget, response)
    actions = dlg.get_action_area()
    actions.set_orientation(Gtk.Orientation.VERTICAL)
    actions.set_layout(Gtk.ButtonBoxStyle.EXPAND)
    auto.set_can_default(True)
    dlg.set_default_response(Gtk.ResponseType.YES)
    return dlg


def completed(on_open):
    window = Gtk.Window()
    prepare(window, 320, "plaud-completed")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_border_width(16)
    window.add(box)
    box.pack_start(label("Sua nota está pronta"), False, False, 0)
    box.pack_start(label("Transcrição e resumo concluídos.", True), False, False, 0)
    box.pack_start(button("Ver as notas", lambda _: (on_open(), window.destroy()), True), False, False, 0)
    box.pack_start(button("Agora não", lambda _: window.destroy()), False, False, 0)
    window.show_all()
    return window


def context_menu(menu):
    """SNI clicks have no GTK pointer event/Wayland serial for Gtk.Menu.popup."""
    window = Gtk.Window()
    prepare(window, 240, "plaud-tray-menu")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_border_width(8)
    window.add(box)
    for item in menu.get_children():
        if isinstance(item, Gtk.MenuItem):
            b = button(item.get_label(), lambda _, item=item: (window.destroy(), item.activate()))
            b.set_sensitive(item.get_sensitive())
            box.pack_start(b, False, False, 0)
    box.pack_start(button("Fechar", lambda _: window.destroy()), False, False, 0)
    window.show_all()
    return window
