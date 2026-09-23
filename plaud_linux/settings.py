#!/usr/bin/env python3
"""Preferences for recording defaults, account access and desktop integration.

Preferences apply to the next recording. Autostart opens the resident tray
without capture; data placement is selected through PLAUD_LINUX_HOME at startup.
"""
import os
import subprocess

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango  # noqa: E402

try:
    from . import audio
    from . import paths
    from . import integration
except ImportError:
    import audio
    import paths
    import integration


WINDOW_WIDTH = 720
WINDOW_HEIGHT = 520
WMCLASS = "plaud-settings"

SETTINGS_FILE = paths.STATE / "settings.json"

# Autostart file, exactly what install.sh --autostart already writes -- the
# toggle below reuses that mechanism rather than inventing a second one.
#
# NOT under paths.DATA_HOME: XDG autostart is a desktop-session location, not
# app data, so PLAUD_LINUX_HOME must not move it. Redirectable for tests via
# PLAUD_LINUX_AUTOSTART_DIR instead -- without this a test that toggles
# autostart on writes into the user's REAL ~/.config/autostart (measured: the
# first version of this module's own test did exactly that, transiently
# adding a real login item to this machine's session).
_AUTOSTART_DIR = os.environ.get(
    "PLAUD_LINUX_AUTOSTART_DIR", os.path.expanduser("~/.config/autostart"))
_AUTOSTART_FILE = os.path.join(_AUTOSTART_DIR, "plaud-linux.desktop")

# Kept deliberately small: this app's own UI strings are hardcoded Portuguese
# throughout, and this dropdown does NOT retranslate
# them -- see the note in _build_general(). "pt" is plaud_api.py's existing
# hardcoded default (_headers(..., lang="pt")); "en" is the only other value
# this codebase has ever had reason to send. Not the official app's full
# 10-language list -- that would be speculative for a header nothing here
# reads back.
LANGUAGES = [("pt", "Português"), ("en", "English")]

# Colour tokens, same source as panel.py/overlay.py --
# renderer/assets/index-CYM9K6Ws.css :root (light).
CSS = b"""
window.plaud-settings, .plaud-settings { background: #fff; color: #111; font-size: 13px; }
.plaud-settings label, .plaud-settings button { color: #111; }
.plaud-settings button, .plaud-settings combobox { background: #fff; box-shadow: none; }
.plaud-settings switch { background: #ddd; }
.plaud-settings switch:checked { background: #000; }
.plaud-settings switch slider { background: #fff; }

.plaud-settings-sidebar {
  background-color: #f5f5f5;
  border-right: 1px solid #ebebeb;
}
.plaud-settings button.plaud-settings-tab {
  border: none;
  background: transparent;
  border-radius: 5px;
  padding: 8px 12px;
}
.plaud-settings button.plaud-settings-tab:checked { background-color: #e5e5e5; }
.plaud-settings-section-title { font-weight: 600; }
.plaud-settings .plaud-settings-hint { color: #7a7a7a; }
.plaud-settings-sep { background: #ebebeb; min-height: 1px; }
"""

_css_screens = set()


def _load():
    return integration.read_settings(SETTINGS_FILE)


def _save(values):
    integration.write_settings(SETTINGS_FILE, values)


def get_option(key):
    return bool(_load().get(key, True))


def set_option(key, enabled):
    values = _load()
    values[key] = bool(enabled)
    _save(values)


def get_language():
    """The persisted app-language, or plaud_api.py's own default."""
    return _load().get("language", "pt")


def set_language(lang):
    values = _load()
    values["language"] = lang
    _save(values)


# "smart" matches the official micDeviceStateAtom's own default value name
# (OFFICIAL-UI-SPEC.md §0.4) -- kept as the literal string rather than
# renamed, so a future reader diffing this against the official atom sees
# the same value, not an equivalent-but-different one.
MIC_AUTOMATIC = "smart"
MIC_OFF = "off"


def get_mic_device():
    """Persisted microphone choice: MIC_AUTOMATIC, MIC_OFF, or a device name
    from audio.list_sources(). Default matches the official app's own
    default (Automatic)."""
    return _load().get("mic_device", MIC_AUTOMATIC)


def set_mic_device(value):
    values = _load()
    values["mic_device"] = value
    _save(values)


def get_system_audio_enabled():
    """Persisted system-audio-capture preference. Default True, matching the
    official enableSystemAudioRecordAtom's own default."""
    return _load().get("system_audio", True)


def set_system_audio_enabled(enabled):
    values = _load()
    values["system_audio"] = bool(enabled)
    _save(values)


def autostart_enabled():
    return integration.autostart_enabled(_AUTOSTART_DIR)


def set_autostart(enabled):
    integration.set_autostart(enabled, _find_launcher(), _AUTOSTART_DIR)


def _find_launcher():
    """Same launcher path install.sh resolves, for a rebuilt autostart entry.

    install.sh knows the checkout root at install time; this module only
    knows it at runtime, from this file's own location.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "bin", "plaud-linux")


class SettingsWindow(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Preferências")
        self.set_icon_name("plaud-linux")
        self.get_style_context().add_class("plaud-settings")
        self.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.set_resizable(True)
        # Same reasoning as panel.py: a normal toplevel needs the compositor
        # told what it is, or Hyprland tiles it instead of floating it.
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        GLib.set_prgname(WMCLASS)
        self.connect("map-event", self._on_map)

        self._apply_css()
        self._build_ui()

    def _on_map(self, *_):
        GLib.timeout_add(150, self._float)
        return False

    def _float(self):
        """Ask Hyprland to float this window at its own size. See panel.py._float
        for why this runs on a timer rather than from map-event directly, and
        why it is `hyprctl repl` rather than a dispatch string."""
        sel = f'window="class:^({WMCLASS})$"'
        lua = (f'hl.dispatch(hl.dsp.window.float({{action="enable", {sel}}}))\n'
               f'hl.dispatch(hl.dsp.window.resize('
               f'{{x={WINDOW_WIDTH}, y={WINDOW_HEIGHT}, {sel}}}))')
        try:
            subprocess.run(["hyprctl", "repl", lua], capture_output=True, timeout=2)
        except Exception:
            pass
        return False

    def _apply_css(self):
        screen = self.get_screen()
        key = hash(screen)
        if key in _css_screens:
            return
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _css_screens.add(key)

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(root)

        # --- sidebar ---------------------------------------------------
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sidebar.get_style_context().add_class("plaud-settings-sidebar")
        sidebar.set_size_request(180, -1)
        sidebar.set_border_width(12)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)

        general_page = self._build_general()
        recording_page = self._build_recording()
        self.stack.add_named(general_page, "general")
        self.stack.add_named(recording_page, "recording")

        pages = [("Geral", "general"), ("Gravação", "recording")]
        for title, name, page in (
                ("Atalhos", "shortcuts", self._build_shortcuts()),
                ("Notificações", "notifications", self._build_notifications()),
                ("Sincronização em nuvem", "cloud", self._build_cloud()),
                ("Sobre", "about", self._build_about())):
            self.stack.add_named(page, name)
            pages.append((title, name))
        group = None
        for title, name in pages:
            btn = Gtk.RadioButton.new_with_label_from_widget(group, title)
            if group is None:
                group = btn
            btn.set_mode(False)
            btn.get_child().set_xalign(0)
            btn.get_child().set_line_wrap(True)
            btn.get_child().set_max_width_chars(20)
            btn.get_style_context().add_class("plaud-settings-tab")
            btn.connect("toggled", self._on_tab_toggled, name)
            sidebar.pack_start(btn, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_border_width(20)
        content.pack_start(self.stack, True, True, 0)

        root.pack_start(sidebar, False, False, 0)
        root.pack_start(content, True, True, 0)
        root.show_all()

    def _on_tab_toggled(self, btn, name):
        if btn.get_active():
            self.stack.set_visible_child_name(name)

    # --- Geral: "Abrir o Plaud ao iniciar sessão" + "Idioma de exibição" ---
    def _build_general(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        try:
            from . import login
        except ImportError:
            import login
        connected = login.plaud_api.PlaudClient().is_logged_in()
        box.pack_start(self._section_title("Conta Plaud"), False, False, 0)
        box.pack_start(Gtk.Label(label="Conta conectada neste aplicativo" if connected else "Entre pelo navegador para conectar sua conta", xalign=0), False, False, 0)
        account_button = Gtk.Button(label="Reconectar conta" if connected else "Entrar no Plaud")
        account_button.set_halign(Gtk.Align.START)
        account_button.connect("clicked", lambda _: login.begin_login(force=connected))
        box.pack_start(account_button, False, False, 0)

        box.pack_start(self._section_title("Sistema"), False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)

        # Start with system -- reuses install.sh --autostart's own mechanism
        # (see set_autostart above), not a second one.
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.pack_start(Gtk.Label(label="Abrir o Plaud ao iniciar sessão", xalign=0),
                        True, True, 0)
        self.sw_autostart = Gtk.Switch()
        self.sw_autostart.set_active(autostart_enabled())
        self.sw_autostart.set_valign(Gtk.Align.CENTER)
        self.sw_autostart.connect("notify::active", self._on_autostart_toggled)
        row.pack_start(self.sw_autostart, False, False, 0)
        box.pack_start(row, False, False, 0)

        # Display language -- edits app-language on API calls (plaud_api.py's
        # _headers()), NOT this app's own GTK strings. Said explicitly here
        # because a "Idioma de exibição" label that did not retranslate the
        # window it lives in would otherwise look broken rather than scoped.
        lang_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        lang_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lang_col.pack_start(Gtk.Label(label="Idioma enviado ao Plaud", xalign=0), False, False, 0)
        hint = Gtk.Label(
            label="Só o cabeçalho de idioma das chamadas deste app à API. Não muda "
                  "a interface nem o idioma do áudio no seletor da Plaud Web.", xalign=0)
        hint.get_style_context().add_class("plaud-settings-hint")
        hint.set_line_wrap(True)
        # set_line_wrap alone does not cap the label's natural (unwrapped)
        # width request -- without this the window's own natural size
        # request stretched to fit every hint on one line, forcing the whole
        # SettingsWindow well past its 720px official width (measured: 1352
        # wide, and hyprctl resize back to 720 was silently refused because
        # GTK's own size negotiation won).
        hint.set_max_width_chars(48)
        lang_col.pack_start(hint, False, False, 0)
        lang_row.pack_start(lang_col, True, True, 0)

        self.combo_lang = Gtk.ComboBoxText()
        current = get_language()
        active_idx = 0
        for i, (code, label) in enumerate(LANGUAGES):
            self.combo_lang.append(code, label)
            if code == current:
                active_idx = i
        self.combo_lang.set_active(active_idx)
        self.combo_lang.set_valign(Gtk.Align.CENTER)
        self.combo_lang.connect("changed", self._on_language_changed)
        lang_row.pack_start(self.combo_lang, False, False, 0)
        box.pack_start(lang_row, False, False, 0)

        box.pack_start(Gtk.Box(), False, False, 6)  # spacer

        # Data folder -- see module docstring for why this is display + open
        # + restart notice, not a picker.
        box.pack_start(self._section_title("Dados"), False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)

        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        folder_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        folder_col.pack_start(Gtk.Label(label="Pasta de dados", xalign=0), False, False, 0)
        path_label = Gtk.Label(label=str(paths.DATA_HOME), xalign=0)
        path_label.set_selectable(True)
        path_label.get_style_context().add_class("plaud-settings-hint")
        # A path (unlike prose) reads worse wrapped mid-string -- ellipsize
        # the start instead, same fix as the hint labels above (cap the
        # natural width request) but appropriate to what this label holds:
        # the full path is still reachable by selecting the text or via
        # "Abrir pasta", so nothing is actually hidden.
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_label.set_max_width_chars(56)
        folder_col.pack_start(path_label, False, False, 0)
        restart_hint = Gtk.Label(
            label="Para mudar, defina PLAUD_LINUX_HOME e reinicie o Plaud "
                  "Linux -- não pode ser alterado nesta janela.",
            xalign=0)
        restart_hint.get_style_context().add_class("plaud-settings-hint")
        restart_hint.set_line_wrap(True)
        restart_hint.set_max_width_chars(56)  # see the language hint's own comment above
        folder_col.pack_start(restart_hint, False, False, 0)
        folder_row.pack_start(folder_col, True, True, 0)

        btn_open = Gtk.Button(label="Abrir pasta")
        btn_open.set_valign(Gtk.Align.CENTER)
        btn_open.connect("clicked", self._on_open_folder)
        folder_row.pack_start(btn_open, False, False, 0)
        box.pack_start(folder_row, False, False, 0)

        return box

    # --- Gravação: "Microfone" (read-only) + "Áudio do sistema" (read-only) --
    def _build_recording(self):
        """Áudio pane. Shape PROVEN, OFFICIAL-UI-SPEC.md §0.4 (`Audio$1()` in
        renderer/assets/index-CrfkTyBe.js): a microphone DEVICE SELECTOR
        (not a toggle -- "Off" is one of its options) and a separate
        "System audio" Switch. NOT symmetric on/off pairs.

        Both write to this module's settings.json (mic_device/system_audio),
        read when the next capture starts. Neither control here
        calls anything that sets a live PipeWire sink or source; the
        microphone list comes from audio.list_sources(), a read-only
        enumeration.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.pack_start(self._section_title("Áudio"), False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)

        # --- Microphone: device selector, default "Automatic" -------------
        mic_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        mic_row.pack_start(Gtk.Label(label="Microfone", xalign=0), True, True, 0)

        self.combo_mic = Gtk.ComboBoxText()
        for cell in self.combo_mic.get_cells():
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            cell.set_property("max-width-chars", 24)
            cell.set_property("width-chars", 24)
        self.combo_mic.append(MIC_AUTOMATIC, "Automático")
        self.combo_mic.append(MIC_OFF, "Desativado")
        try:
            sources = audio.list_sources()
        except Exception:
            sources = []
        # .monitor sources are sinks-as-inputs (system audio loopback), not
        # microphones -- listing them here would offer to "record from" a
        # speaker output as if it were a mic, which is a different control
        # (the System audio switch below already covers that signal).
        for name in sources:
            if not name.endswith(".monitor"):
                self.combo_mic.append(name, name)

        current_mic = get_mic_device()
        ids = [MIC_AUTOMATIC, MIC_OFF] + [n for n in sources if not n.endswith(".monitor")]
        self.combo_mic.set_active(ids.index(current_mic) if current_mic in ids else 0)
        self.combo_mic.set_valign(Gtk.Align.CENTER)
        self.combo_mic.connect("changed", self._on_mic_changed)
        mic_row.pack_start(self.combo_mic, False, False, 0)
        box.pack_start(mic_row, False, False, 0)

        mic_hint = Gtk.Label(
            label="O Plaud seleciona automaticamente o melhor microfone "
                  "para gravações claras e sem interrupções.",
            xalign=0)
        mic_hint.get_style_context().add_class("plaud-settings-hint")
        mic_hint.set_line_wrap(True)
        mic_hint.set_max_width_chars(56)  # see the language hint's own comment in _build_general
        box.pack_start(mic_hint, False, False, 0)

        # --- System audio: plain Switch, default on ------------------------
        sys_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        sys_row.pack_start(Gtk.Label(label="Áudio do sistema", xalign=0), True, True, 0)
        self.sw_system_audio = Gtk.Switch()
        self.sw_system_audio.set_active(get_system_audio_enabled())
        self.sw_system_audio.set_valign(Gtk.Align.CENTER)
        self.sw_system_audio.connect("notify::active", self._on_system_audio_toggled)
        sys_row.pack_start(self.sw_system_audio, False, False, 0)
        box.pack_start(sys_row, False, False, 0)
        hint = Gtk.Label(label="Estas preferências são aplicadas na próxima gravação. Durante uma gravação, use os controles do widget.", xalign=0)
        hint.set_line_wrap(True)
        hint.set_max_width_chars(48)
        hint.get_style_context().add_class("plaud-settings-hint")
        box.pack_start(hint, False, False, 0)

        return box

    def _preference_page(self, title, description):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.pack_start(self._section_title(title), False, False, 0)
        hint = Gtk.Label(label=description, xalign=0)
        hint.set_line_wrap(True)
        hint.set_max_width_chars(48)
        hint.get_style_context().add_class("plaud-settings-hint")
        box.pack_start(hint, False, False, 0)
        return box

    def _switch_row(self, box, title, key):
        row = Gtk.Box(spacing=12)
        row.pack_start(Gtk.Label(label=title, xalign=0), True, True, 0)
        switch = Gtk.Switch()
        switch.set_active(get_option(key))
        switch.connect("notify::active", lambda w, _: set_option(key, w.get_active()))
        row.pack_start(switch, False, False, 0)
        box.pack_start(row, False, False, 0)

    def _build_shortcuts(self):
        box = self._preference_page("Atalhos", "Disponíveis com o Plaud aberto e os atalhos instalados no Hyprland.")
        for title, keys in (("Iniciar / parar", "Super + Alt + R"),
                            ("Encerrar gravação", "Super + Alt + E"),
                            ("Pausar / retomar", "Super + Alt + P"),
                            ("Abrir destaques", "Alt + Shift + H"),
                            ("Capturar região", "Alt + Shift + C")):
            box.pack_start(Gtk.Label(label=f"{title}    {keys}", xalign=0), False, False, 0)
        return box

    def _build_notifications(self):
        box = self._preference_page("Notificações", "Erros de gravação e envio continuam sendo exibidos para ajudar a recuperar seu áudio.")
        self._switch_row(box, "Avisar quando a nota estiver pronta", "notify_complete")
        return box

    def _build_cloud(self):
        box = self._preference_page("Sincronização em nuvem privada", "Envie as gravações para sua conta Plaud ao encerrar. Com a sincronização desativada, o áudio permanece neste computador e pode ser enviado em Envios recentes.")
        self._switch_row(box, "Enviar automaticamente ao encerrar", "cloud_sync")
        return box

    def _build_about(self):
        return self._preference_page("Plaud Linux", "Cliente independente para Omarchy / Arch Linux. Usa sua conta Plaud para transcrição e resumo. Áudio e anotações ficam preservados neste computador.")

    def _section_title(self, text):
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.get_style_context().add_class("plaud-settings-section-title")
        return lbl

    # --- handlers ------------------------------------------------------
    def _on_autostart_toggled(self, switch, _pspec):
        if getattr(self, "_resetting_autostart", False):
            return
        try:
            set_autostart(switch.get_active())
        except (OSError, ValueError, RuntimeError):
            self._resetting_autostart = True
            try:
                switch.set_active(autostart_enabled())
            finally:
                self._resetting_autostart = False
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="Não foi possível alterar a inicialização automática.")
            dialog.format_secondary_text(
                "Verifique as permissões e a entrada do Plaud na pasta de inicialização. "
                "O arquivo existente foi preservado.")
            dialog.connect("response", lambda window, _: window.destroy())
            dialog.show()

    def _on_language_changed(self, combo):
        code = combo.get_active_id()
        if code:
            set_language(code)

    def _on_open_folder(self, _btn):
        try:
            subprocess.Popen(["xdg-open", str(paths.DATA_HOME)])
        except Exception:
            pass

    def _on_mic_changed(self, combo):
        device = combo.get_active_id()
        if device:
            set_mic_device(device)

    def _on_system_audio_toggled(self, switch, _pspec):
        set_system_audio_enabled(switch.get_active())


_window = None


def open_settings():
    """Open the settings window, or present the one already open.

    Same singleton pattern as panel.open_for(): does NOT run a GTK loop,
    tray.run_tray() owns the one Gtk.main().
    """
    global _window
    if _window is not None:
        _window.present()
        return _window
    _window = SettingsWindow()
    _window.connect("destroy", _on_destroy)
    _window.show_all()
    return _window


def _on_destroy(*_):
    global _window
    _window = None


if __name__ == "__main__":
    win = open_settings()
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()
