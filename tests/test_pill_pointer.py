"""Movable pill regression and --manual physical input check; no capture/network."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

manual = '--manual' in sys.argv
repo = next((a for a in sys.argv[1:] if a != '--manual'), str(pathlib.Path(__file__).resolve().parent.parent))
os.environ['PLAUD_LINUX_HOME'] = tempfile.mkdtemp(prefix='plaud-pill-check-')
sys.path.insert(0, repo)
requests = types.ModuleType('requests')
def blocked(*args, **kwargs):
    raise AssertionError('Network disabled in pill check')
requests.get = requests.post = requests.put = requests.request = blocked
requests.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules['requests'] = requests
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkLayerShell', '0.1')
from gi.repository import Gtk, GLib, GtkLayerShell
from plaud_linux import overlay as ov

class FakeRec:
    def __init__(self, **kwargs):
        self.state = 'recording'
        self.mode = 'system'
        self.screenshots, self.notes, self.flags = [], [], []
        self.mark_buffer = None
        self.session = 'pill-check'
        self.final_path = pathlib.Path(os.environ['PLAUD_LINUX_HOME']) / 'fake.opus'
        self.stops = 0
    def start(self): pass
    def stop(self):
        self.stops += 1
        self.state = 'stopped'
        return self.final_path
    def pause(self): self.state = 'paused'
    def resume(self): self.state = 'recording'
    def elapsed(self): return 10
    def _save_meta(self): pass
    def add_note(self, text, elapsed): self.notes.append((text, elapsed))
    def set_mode(self, mode): self.mode = mode; return True
    def device_status(self): return (True, True, None, None)

ov.audio.Recorder = FakeRec

def pump():
    loop = GLib.MainLoop()
    GLib.timeout_add(250, lambda: (loop.quit(), False)[1])
    loop.run()

def make(**kwargs):
    return ov.Overlay(on_stop=lambda rec: None, **kwargs)

if manual:
    ov.Overlay._on_shot = lambda *a: print('SCREENSHOT click (capture disabled)', flush=True)
    o = ov.Overlay(on_stop=lambda rec: (print('STOP received', flush=True), Gtk.main_quit()))
    o.connect('button-press-event', lambda *a: print('POINTER press', flush=True))
    o.btn_note.connect('clicked', lambda *a: print('PENCIL received', flush=True))
    o.btn_pause.connect('clicked', lambda *a: print('STATE', o.rec.state, flush=True))
    print('MANUAL: no audio capture, no upload; SUPER+drag, pencil, pause/resume, stop.', flush=True)
    Gtk.main()
    sys.exit(0)

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS' if ok else 'FAIL') + '  ' + name, flush=True)

old_name = GLib.get_prgname()
o = make()
pump()
check('1 Hyprland pill is a movable toplevel', not GtkLayerShell.is_layer_window(o))
clients = json.loads(subprocess.check_output(['hyprctl', 'clients', '-j']))
mine = [c for c in clients if c.get('pid') == os.getpid() and c.get('title') == 'plaud-recording-pill']
check('2 compositor maps pill floating and pinned', len(mine) == 1 and mine[0]['floating'] and mine[0]['pinned'])
check('3 placement leaves global app identity untouched', GLib.get_prgname() == old_name)
from plaud_linux import panel
opened = []
with patch.object(panel, 'open_for', side_effect=lambda source: opened.append(source)):
    o.btn_note.clicked()
o._toggle_expand()
o.btn_pause.clicked()
paused = o.rec.state == 'paused'
o.btn_pause.clicked()
resumed = o.rec.state == 'recording'
o.btn_stop.clicked()
pump()
o._do_stop()
check('4 pencil pause resume and stop preserve callbacks', opened == [o] and paused and resumed and o.rec.stops == 1)

# Failures must choose a visible layer surface BEFORE the recorder starts.
for number, label, effect in [(5, 'missing hyprctl', FileNotFoundError()),
                              (6, 'Lua rejection', types.SimpleNamespace(returncode=0, stdout='error: invalid rule')),
                              (7, 'IPC timeout', subprocess.TimeoutExpired('hyprctl', 2))]:
    kwargs = {'side_effect': effect} if isinstance(effect, Exception) else {'return_value': effect}
    with patch.object(ov.subprocess, 'run', **kwargs):
        o = make()
    check(f'{number} {label} retains layer-shell', GtkLayerShell.is_layer_window(o))
    o.destroy()
with patch.object(ov, 'HYPRLAND', False, create=True):
    o = make()
check('8 other compositors retain layer-shell', GtkLayerShell.is_layer_window(o))
o.destroy()
sys.exit(0 if all(results) else 1)
