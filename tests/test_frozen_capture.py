"""Frozen screenshot lifecycle; no capture, network or real account."""
import io
import json
import os
import pathlib
import sys
import tempfile
import types
from unittest.mock import patch

os.environ['PLAUD_LINUX_HOME'] = tempfile.mkdtemp(prefix='plaud-freeze-')
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
blocked = types.ModuleType('requests')
def no_network(*a, **kw):
    raise AssertionError('Network forbidden')
blocked.get = blocked.post = blocked.put = blocked.request = no_network
sys.modules['requests'] = blocked
from plaud_linux import overlay as ov


class Child:
    pid = 12345
    def __init__(self):
        self.dead = False
        self.terminated = False
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
    def poll(self): return 0 if self.dead else None
    def terminate(self): self.terminated = self.dead = True
    def kill(self): self.terminated = self.dead = True
    def wait(self, timeout=None): return 0


class Rec:
    state = 'recording'
    def __init__(self): self.screenshots = []
    def elapsed(self): return 12
    def add_screenshot(self, path): self.screenshots.append(path)


class Harness:
    # Execute actual Overlay methods without constructing a recording/window.
    pass

for name in ('_on_shot', '_shot_fail', '_shot_done', '_grim', '_remove_tick',
             '_release_freeze', '_begin_region', '_capture_timeout', '_cancel_selector'):
    if hasattr(ov.Overlay, name):
        setattr(Harness, name, getattr(ov.Overlay, name))


def make():
    h = Harness()
    h.rec = Rec()
    h.shots_dir = pathlib.Path(os.environ['PLAUD_LINUX_HOME'])
    h._shot_pending = False
    h._shot_watch = h._shot_proc = h._tick_id = None
    h._freeze_proc = h._freeze_start_id = h._freeze_deadline_id = None
    h._shot_out = (None, 0)
    h._shots_taken = False
    h._DECLINED = ov.Overlay._DECLINED
    h.notes, h.selections = [], []
    h._notify = lambda *a: h.notes.append(a)
    h._shot_next = lambda *a: h.selections.append(a)
    h._freeze_ready = lambda: True
    return h


results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)


timers = {}
def timer(_ms, fn, *args):
    ident = len(timers) + 1
    timers[ident] = (fn, args)
    return ident


with patch.object(ov.GLib, 'timeout_add', timer), patch.object(ov.GLib, 'source_remove', lambda n: None):
    h = make()
    child = Child()
    calls = []
    def spawn(cmd, **kw):
        calls.append(cmd)
        return child
    with patch.object(ov.subprocess, 'Popen', spawn):
        h._on_shot()
        h._on_shot()
    check('freeze starts before selector, duplicate request ignored',
          len(calls) == 1 and calls[0][0] == 'hyprpicker' and not h.selections)
    if hasattr(h, '_begin_region'):
        h._begin_region()
        h._begin_region()
    check('selector begins after freeze preparation', len(h.selections) == 1 and not child.dead)
    check('screenshot selection keeps recording active', h.rec.state == 'recording')

    proc = Child()
    proc.stderr = io.BytesIO(b'selection cancelled')
    out = h.shots_dir / 'cancel.png'
    h._shot_done(proc, [['slurp'], []], out, 12)
    check('cancel releases freezer and saves nothing', child.terminated and not out.exists()
          and not h.rec.screenshots and not h._shot_pending)

    h = make()
    child = Child()
    h._freeze_proc = child
    h._shot_pending = True
    out = h.shots_dir / 'success.png'
    def capture(cmd, **kw): pathlib.Path(cmd[-1]).write_bytes(b'synthetic image')
    with patch.object(ov.subprocess, 'run', capture):
        h._grim('1,2 30x40', out, 12, [['slurp'], []])
    check('success saves selected image and releases freezer',
          child.terminated and h.rec.screenshots == [out] and not h._shot_pending)

    h = make()
    child = Child()
    h._freeze_proc = child
    h._shot_pending = True
    h._shot_fail()
    check('failure releases freezer and permits another capture', child.terminated and not h._shot_pending)

    h = make()
    child = Child()
    with patch.object(ov.subprocess, 'Popen', return_value=child): h._on_shot()
    h._remove_tick()
    check('destroy during freeze preparation cancels pending selector',
          child.terminated and not h._shot_pending and h._freeze_start_id is None)

    h = make()
    child = Child()
    h._freeze_proc = child
    h._shot_pending = True
    h.rec.state = 'stopped'
    out = h.shots_dir / 'stopped.png'
    with patch.object(ov.subprocess, 'run', capture):
        h._grim('1,2 30x40', out, 12, [['slurp'], []])
    check('stopped recording receives no late screenshot and releases freezer',
          not h.rec.screenshots and child.terminated)

    h = make()
    with patch.object(ov.subprocess, 'Popen', side_effect=FileNotFoundError): h._on_shot()
    check('missing freezer keeps explicit legacy capture with notice',
          len(h.selections) == 1 and bool(h.notes))

    h = make()
    child = Child()
    child.dead = True
    h._freeze_proc = child
    h._shot_pending = True
    out = h.shots_dir / 'dead-freezer.png'
    with patch.object(ov.subprocess, 'run', capture):
        h._grim('1,2 30x40', out, 12, [['slurp'], []])
    check('dead freezer cannot silently capture a later live frame', not out.exists() and not h.rec.screenshots)

    h = make()
    child, selector = Child(), Child()
    h._freeze_proc, h._shot_proc = child, selector
    h._shot_pending = True
    if hasattr(h, '_capture_timeout'): h._capture_timeout()
    check('selection deadline releases owned helpers without stopping audio',
          child.terminated and selector.terminated and not h._shot_pending and h.rec.state == 'recording')

    h = make()
    child = Child()
    child.wait = lambda timeout: (_ for _ in ()).throw(ov.subprocess.TimeoutExpired('freezer', timeout))
    h._freeze_proc = child
    h._shot_pending = True
    h._shot_fail()
    check('unresponsive freezer cannot interrupt failure cleanup', not h._shot_pending and h._freeze_proc is None)

    h = make()
    child = Child()
    child.dead = True
    with patch.object(ov.subprocess, 'Popen', return_value=child): h._on_shot()
    if hasattr(h, '_begin_region'): h._begin_region()
    check('freezer failure before selection fails safely without taking a different frame',
          not h.selections and not h._shot_pending and bool(h.notes))

    h = make()
    h.rec.state = 'stopped'
    with patch.object(ov.subprocess, 'Popen', spawn): h._on_shot()
    check('stopped recording cannot start another capture', not h._shot_pending and not h.selections)

    h = make()
    child = Child()
    with patch.object(ov.subprocess, 'Popen', return_value=child): h._on_shot()
    h._freeze_ready = lambda: False
    waiting = h._begin_region() if hasattr(h, '_begin_region') else False
    check('selector waits while a monitor freeze surface is still fading', waiting and not h.selections)
    for _ in range(10):
        if hasattr(h, '_begin_region'): h._begin_region()
    check('readiness has a bounded deadline and releases the desktop', child.terminated and not h._shot_pending)

    h = make()
    h._freeze_proc = Child()
    layer = {'pid': Child.pid, 'namespace': 'hyprpicker', 'alpha': 1, 'w': 1920, 'h': 1080}
    outputs = {'DP-2': {'levels': {'3': [dict(layer)]}}, 'HDMI-A-1': {'levels': {'3': [dict(layer)]}}}
    def readiness():
        if not hasattr(ov.Overlay, '_freeze_ready'): return False
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': 'synthetic'}), patch.object(
                ov.subprocess, 'run', return_value=types.SimpleNamespace(stdout=json.dumps(outputs))):
            return ov.Overlay._freeze_ready(h)
    ready = readiness()
    outputs['HDMI-A-1']['levels']['3'][0]['alpha'] = 0.8
    fading = readiness()
    outputs['HDMI-A-1']['levels']['3'][0].update(alpha=1, pid=999)
    foreign = readiness()
    check('both monitors must have fully visible surfaces owned by this freezer', ready and not fading and not foreign)

sys.exit(0 if all(results) else 1)
