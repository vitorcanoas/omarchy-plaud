"""Isolated settings interaction and device-discovery regression checks."""
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ['PLAUD_LINUX_HOME'] = tempfile.mkdtemp(prefix='plaud-settings-ux-')
os.environ['PLAUD_LINUX_AUTOSTART_DIR'] = tempfile.mkdtemp(prefix='plaud-autostart-ux-')
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parents[1]))
def blocked(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.create_connection = blocked
socket.socket.connect = blocked
from plaud_linux import settings as st
from gi.repository import Gtk, Gdk, GLib

results = []
def check(name, fn):
    try:
        assert fn(), name
        print('PASS ' + name)
        results.append(True)
    except Exception as exc:
        print('FAIL ' + name + ': ' + type(exc).__name__ + ' ' + str(exc))
        results.append(False)

def pump(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return True
        time.sleep(.005)
    return False

def window(source='missing-mic'):
    st.set_mic_device(source)
    w = st.SettingsWindow()
    pump(lambda: getattr(w, '_mic_loading', False) is False)
    return w

# SPECSFY: US-001 FR-001 NFR-001 AC-001
with patch.object(st, '_discover_microphones', return_value=['available-mic'], create=True), patch.object(st.audio, 'list_sources', return_value=['available-mic']):
    w = window()
    check('visible preferences title and close', lambda: isinstance(w.get_titlebar(), Gtk.HeaderBar) and w.get_titlebar().get_show_close_button())
    # SPECSFY: US-001 FR-001 NFR-001 AC-001
    before = st._load()
    def escape():
        destroyed = []
        w.connect('destroy', lambda *_: destroyed.append(True))
        result = w._on_key_press(w, SimpleNamespace(keyval=Gdk.KEY_Escape))
        return result and bool(destroyed) and st._load() == before
    check('Escape closes only preferences without writing', escape)
    w.destroy()
    # SPECSFY: US-001 FR-001 NFR-001 AC-001
    w = window()
    def navigation():
        names = ['general', 'recording', 'shortcuts', 'notifications', 'cloud', 'about']
        return all(isinstance(w.stack.get_child_by_name(n), Gtk.ScrolledWindow) for n in names)
    check('all six pages have vertical overflow recovery', navigation)
    w.destroy()
    # SPECSFY: US-001 FR-001 NFR-001 AC-002
    w = window()
    check('unavailable saved microphone remains selected', lambda: w.combo_mic.get_active_id() == 'missing-mic' and 'indisponível' in w.combo_mic.get_active_text().lower())
    # SPECSFY: US-001 FR-001 NFR-001 AC-002
    check('loading never rewrites missing source', lambda: st.get_mic_device() == 'missing-mic')
    w.destroy()
    # SPECSFY: US-001 FR-001 NFR-001 AC-002
    w = window('available-mic')
    check('available saved source survives loading', lambda: w.combo_mic.get_active_id() == 'available-mic' and st.get_mic_device() == 'available-mic')
    w.destroy()

# SPECSFY: US-001 FR-001 NFR-001 AC-003
started, release = threading.Event(), threading.Event()
def slow():
    started.set()
    release.wait(2)
    return ['late-mic']
with patch.object(st, '_discover_microphones', side_effect=slow, create=True), patch.object(st.audio, 'list_sources', return_value=[]):
    w = st.SettingsWindow()
    def lifecycle():
        responsive = started.wait(.5) and getattr(w, '_mic_loading', False) and not w.combo_mic.get_sensitive()
        w.destroy()
        # A completion must return before any destroyed-widget operation.
        with patch.object(w.combo_mic, 'remove_all', side_effect=AssertionError('late widget mutation')):
            if hasattr(w, '_apply_microphones'):
                w._apply_microphones(['late-mic'], False)
            else:
                responsive = False
            release.set()
            pump(lambda: False, .05)
        return responsive
    check('discovery runs off GTK and late completion is ignored', lifecycle)

# SPECSFY: US-001 FR-001 NFR-001 AC-003
with patch.object(st, '_discover_microphones', side_effect=OSError('synthetic'), create=True), patch.object(st.audio, 'list_sources', return_value=[]):
    w = window()
    def retry():
        error_visible = w.mic_status.get_text().startswith('Não foi possível') and w.mic_retry.get_visible()
        with patch.object(st, '_discover_microphones', return_value=['missing-mic']):
            w.mic_retry.clicked()
            loaded = pump(lambda: not w._mic_loading)
        return error_visible and loaded and st.get_mic_device() == 'missing-mic' and w.combo_mic.get_active_text() == 'missing-mic'
    check('discovery error offers retry and preserves selection', retry)
    w.destroy()

# SPECSFY: US-001 FR-001 NFR-001 AC-003
def bounded_query():
    with patch.object(st.subprocess, 'run', return_value=SimpleNamespace(stdout='1\tmic\tx\n2\tspeakers.monitor\tx\n')) as run:
        found = st._discover_microphones()
        return found == ['mic'] and run.call_args.kwargs.get('timeout') == 3 and run.call_args.kwargs.get('check') is True
check('settings discovery bounds pactl and excludes monitors', bounded_query)
sys.exit(0 if all(results) else 1)
