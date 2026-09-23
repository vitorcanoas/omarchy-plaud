"""Check for CAN-314 item 10: the per-screen CSS provider leak.

Usage: python3 tests/test_css.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

_apply_css() attaches a provider to the *screen*, which outlives the Overlay.
Today the app is single-shot so it happens once, but the tray (CAN-310) builds
an overlay per session in one process -- and each would stack another identical
provider that nothing ever removes.

Counts real calls to add_provider_for_screen across several Overlay builds.
"""
import os, sys, tempfile, pathlib, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="csschk-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
from plaud_linux import overlay as ov_mod

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


class FakeRec:
    def __init__(self):
        self.state = "recording"; self.screenshots = []; self.notes = []
        self.session = "chk"; self.final_path = pathlib.Path(home) / "chk.opus"
    def start(self): pass
    def stop(self): self.state = "stopped"; return self.final_path
    def elapsed(self): return 1
    def add_screenshot(self, p): self.screenshots.append(p)


added = []
real_add = Gtk.StyleContext.add_provider_for_screen
def counting_add(screen, prov, prio):
    added.append(prov)
    return real_add(screen, prov, prio)
Gtk.StyleContext.add_provider_for_screen = counting_add

real_rec = ov_mod.audio.Recorder
ov_mod.audio.Recorder = lambda **kw: FakeRec()
overlays = []
try:
    for _ in range(4):
        o = ov_mod.Overlay(mode="system", on_stop=lambda rec: None)
        overlays.append(o)
finally:
    ov_mod.audio.Recorder = real_rec
    Gtk.StyleContext.add_provider_for_screen = real_add

check("1 four overlays attach exactly one CSS provider",
      len(added) == 1, f"(providers attached = {len(added)}, expected 1)")

for o in overlays:
    o.destroy()

# The registry must stay bounded: one entry per screen, not per Overlay.
# (A WeakSet was tried and is useless here -- nothing holds a strong ref to the
# Gdk.Screen wrapper, so entries vanish immediately and every overlay re-adds.)
reg = getattr(ov_mod, "_css_screens", None)
check("2 the screen registry holds one entry per screen",
      isinstance(reg, set) and len(reg) == 1,
      f"(registry = {reg!r})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
