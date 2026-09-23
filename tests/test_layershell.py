"""Checks for the Wayland port: the overlay is a real, placed layer surface.

Usage: python3 tests/test_layershell.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-port tree, PASS after.

Why this file exists: the pre-port overlay looked healthy to every other check
in tests/, because none of them ever mapped a window or read a coordinate. It
positioned itself with move(), which under Wayland is a *silent* no-op -- no
exception, no warning, a constructed object and a wrong pixel. So the checks
here deliberately do the two things the rest of the suite does not:

  1. assert the window is actually a layer-shell surface (checks 1-4), which is
     pure API state and runs anywhere GtkLayerShell imports; and
  2. map the real overlay and ask the *compositor* where it ended up
     (check 5), which is the only evidence that survives "the constructor
     returned an object".

Check 5 needs a live Hyprland session and is skipped as a PASS elsewhere -- it
cannot be made to run on X11 or headless, and failing it there would be noise.
"""
import os, sys, json, tempfile, pathlib, subprocess, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="lschk-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, GLib, GtkLayerShell
from plaud_linux import overlay as ov_mod
ov_mod.HYPRLAND = False  # Exercise the fallback contract even on Hyprland.

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


class FakeRec:
    """Stands in for Recorder, so no ffmpeg runs: this file is about geometry."""
    def __init__(self):
        self.state = "recording"
        self.screenshots = []
        self.notes = []
        self.session = "lschk"
        self.final_path = pathlib.Path(home) / "lschk.opus"
    def start(self): pass
    def stop(self):
        self.state = "stopped"
        return self.final_path
    def elapsed(self): return 5
    def add_screenshot(self, p): self.screenshots.append(p)


def make_overlay():
    real = ov_mod.audio.Recorder
    ov_mod.audio.Recorder = lambda **kw: FakeRec()
    try:
        return ov_mod.Overlay(mode="system", on_stop=lambda rec: None)
    finally:
        ov_mod.audio.Recorder = real


o = make_overlay()

# --- CHECK 1: it is a layer surface at all ----------------------------------
# The whole port hinges on this one bit. A plain Gtk.Window still shows up and
# still runs the timer, which is exactly why the old code's breakage was
# invisible: only this predicate separates "floating pill" from "ordinary
# window the compositor parks wherever it likes".
check("1 overlay is a gtk-layer-shell surface",
      GtkLayerShell.is_layer_window(o),
      f"(is_layer_window={GtkLayerShell.is_layer_window(o)})")

# --- CHECK 2: on the overlay layer, above fullscreen ------------------------
# TOP would render below a fullscreen window -- i.e. it would disappear behind
# the fullscreen video call this app exists to record.
layer = GtkLayerShell.get_layer(o)
check("2 sits on the OVERLAY layer, not below fullscreen windows",
      layer == GtkLayerShell.Layer.OVERLAY,
      f"(layer={layer.value_nick if hasattr(layer, 'value_nick') else layer})")

# --- CHECK 3: anchored top-right with the intended margins ------------------
# This is what replaced move(sw - 420, 48). Asserting the anchors *and* the
# margins means a future edit cannot quietly drop one and leave the pill in a
# corner nobody looks at.
anchored = (GtkLayerShell.get_anchor(o, GtkLayerShell.Edge.TOP)
            and GtkLayerShell.get_anchor(o, GtkLayerShell.Edge.RIGHT)
            and not GtkLayerShell.get_anchor(o, GtkLayerShell.Edge.LEFT)
            and not GtkLayerShell.get_anchor(o, GtkLayerShell.Edge.BOTTOM))
m_top = GtkLayerShell.get_margin(o, GtkLayerShell.Edge.TOP)
m_right = GtkLayerShell.get_margin(o, GtkLayerShell.Edge.RIGHT)
check("3 anchored TOP|RIGHT with the intended margins",
      anchored and m_top == ov_mod.Overlay.MARGIN_TOP
      and m_right == ov_mod.Overlay.MARGIN_RIGHT,
      f"(anchored={anchored}, top={m_top}, right={m_right})")

# --- CHECK 4: never takes keyboard focus ------------------------------------
# The layer-shell counterpart of the old set_accept_focus(False). If this
# regresses, the pill starts eating keystrokes from the meeting being recorded.
#
# KeyboardMode.NONE is 0, which is also what get_keyboard_mode() reports for a
# window that was never init'd -- so the mode alone would pass on the pre-port
# tree and prove nothing. Pairing it with is_layer_window() makes the assertion
# "a layer surface that declines focus", which only the ported code satisfies.
kb = GtkLayerShell.get_keyboard_mode(o)
check("4 never takes keyboard focus",
      GtkLayerShell.is_layer_window(o) and kb == GtkLayerShell.KeyboardMode.NONE,
      f"(layer_window={GtkLayerShell.is_layer_window(o)}, "
      f"keyboard_mode={kb.value_nick if hasattr(kb, 'value_nick') else kb})")

o.destroy()

# --- CHECK 5: the compositor really places it where we asked ----------------
# The evidence check. Everything above is the client's own opinion of itself;
# this maps the window and reads the geometry back out of Hyprland, which is
# the only thing that would have caught move() silently doing nothing.
def compositor_geometry():
    """Map a real overlay, then ask Hyprland where the surface landed."""
    got = {}
    ov = make_overlay()

    def sample():
        try:
            raw = subprocess.run(["hyprctl", "layers", "-j"],
                                 capture_output=True, text=True, timeout=10).stdout
            for mon, data in json.loads(raw).items():
                for lvl, entries in data.get("levels", {}).items():
                    for e in entries:
                        if "plaud-overlay" in (e.get("namespace") or ""):
                            got.update(monitor=mon, level=lvl, x=e["x"], y=e["y"],
                                       w=e["w"], h=e["h"])
        except Exception as exc:
            got["error"] = repr(exc)
        Gtk.main_quit()
        return False

    # Give the compositor a moment to map and configure the surface; reading
    # immediately after show_all() races the first configure event.
    GLib.timeout_add(1500, sample)
    Gtk.main()
    ov.destroy()
    return got


live = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and os.environ.get("WAYLAND_DISPLAY")
# A tree without the anchor constants is a pre-port tree: that is a FAIL of
# this check, not a crash. Raising here would abort the file, and run.py would
# report "exited with no result" instead of naming the check that went red.
margins = (getattr(ov_mod.Overlay, "MARGIN_TOP", None),
           getattr(ov_mod.Overlay, "MARGIN_RIGHT", None))
if not live:
    check("5 compositor places the pill at the top-right of one monitor",
          True, "(skipped: no live Hyprland session)")
elif None in margins:
    check("5 compositor places the pill at the top-right of one monitor",
          False, "(overlay has no anchor margins: not a layer-shell overlay)")
else:
    g = compositor_geometry()
    mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"],
                                     capture_output=True, text=True).stdout)
    by_name = {m["name"]: m for m in mons}
    ok, detail = False, f"(surface not found in hyprctl layers: {g})"
    if "x" in g and g.get("monitor") in by_name:
        m = by_name[g["monitor"]]
        # reserved = [left, top, right, bottom]; the compositor subtracts any
        # bar's exclusive zone before applying our margin, so the expected
        # position is monitor origin + reserved + our margin.
        res = m.get("reserved", [0, 0, 0, 0])
        # hyprctl reports width/height PRE-transform, but the compositor lays
        # surfaces out in the rotated frame. Transforms 1/3 (90/270 deg) and
        # their flipped forms 5/7 swap the two. Measured on this machine:
        # HDMI-A-1 has transform=1 with width=1920, so an un-swapped formula
        # expects a right edge of 3800 while the correct answer is 2960 -- the
        # app was placing the pill correctly and this check was wrong.
        mon_w = m["width"]
        if m.get("transform", 0) in (1, 3, 5, 7):
            mon_w = m["height"]
        want_right = m["x"] + mon_w - res[2] - ov_mod.Overlay.MARGIN_RIGHT
        want_y = m["y"] + res[1] + ov_mod.Overlay.MARGIN_TOP
        got_right = g["x"] + g["w"]
        # Exact, not approximate: the compositor is doing integer arithmetic
        # from values we control, so a tolerance would only hide a real drift.
        ok = (got_right == want_right and g["y"] == want_y and g["w"] > 0 and g["h"] > 0)
        detail = (f"(on {g['monitor']} right_edge={got_right} want={want_right}, "
                  f"y={g['y']} want={want_y}, size={g['w']}x{g['h']})")
        # A pill wider than its monitor means the anchor maths went wrong.
        if g["w"] > mon_w:
            ok = False
    check("5 compositor places the pill at the top-right of one monitor", ok, detail)

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
