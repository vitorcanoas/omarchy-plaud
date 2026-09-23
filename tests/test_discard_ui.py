"""Checks for CAN-315: the discard/cancel UI wiring in overlay.py.

Usage: python3 tests/test_discard_ui.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).

tests/test_discard.py already proves Recorder.discard() itself removes every
artefact. This file proves the UI wiring on top of it: the confirm dialog
actually gates the action (Cancelar must not discard anything), the confirmed
path calls discard() and NOT stop() (a discard must never upload), the correct
callback (on_discard, not on_stop) fires, and the two terminal paths
(_do_stop/_do_discard) share one guard so they cannot both run for the same
session.

Drives a real Overlay against a FakeRec, same technique as test_overlay.py:
audio.Recorder is swapped out so no real ffmpeg is spawned, and Gtk.Dialog.run/
destroy are monkeypatched to inject a response synthetically -- the same
mechanism test_genchoice.py uses, because Wayland refuses synthetic input into
arbitrary windows (xdotool is gone; see that file's own comment for the
detail). No real GTK loop iteration blocks on a human here.
"""
import os, pathlib, sys, tempfile

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="discarduichk-")
os.environ["PLAUD_LINUX_HOME"] = home
sys.path.insert(0, repo)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk

from plaud_linux import overlay as ov_mod

results = []
def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


class FakeRec:
    """Stands in for Recorder: records which terminal method was actually
    called, so a discard that secretly stops-and-uploads would be caught."""
    def __init__(self):
        self.state = "recording"
        self.screenshots = []
        self.notes = []
        self.session = "chk"
        self.final_path = pathlib.Path(home) / "chk.opus"
        self.calls = []

    def start(self):
        pass

    def stop(self):
        self.calls.append("stop")
        self.state = "stopped"
        return self.final_path

    def discard(self):
        self.calls.append("discard")
        self.state = "discarded"

    def elapsed(self):
        return 5

    def add_screenshot(self, p):
        self.screenshots.append(p)


def make_overlay(on_stop=None, on_discard=None):
    real = ov_mod.audio.Recorder
    ov_mod.audio.Recorder = lambda **kw: FakeRec()
    try:
        o = ov_mod.Overlay(mode="system", on_stop=on_stop, on_discard=on_discard)
    finally:
        ov_mod.audio.Recorder = real
    return o


real_run, real_destroy = Gtk.Dialog.run, Gtk.Dialog.destroy


def fake_run_factory(resp):
    def fake_run(self):
        self.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration()
        return resp
    return fake_run


def fake_destroy(self):
    pass


def click_discard_with(o, resp):
    Gtk.Dialog.run = fake_run_factory(resp)
    Gtk.Dialog.destroy = fake_destroy
    try:
        o._on_discard_clicked()
        while Gtk.events_pending():
            Gtk.main_iteration()
    finally:
        Gtk.Dialog.run, Gtk.Dialog.destroy = real_run, real_destroy


# --- CHECK 1: the kebab and its handlers exist and are wired ----------------
o1 = make_overlay()
check("1 the pill exposes a kebab button wired to _on_kebab",
      hasattr(o1, "btn_kebab") and hasattr(o1, "_on_kebab")
      and hasattr(o1, "_on_discard_clicked") and hasattr(o1, "_do_discard"),
      f"kebab={hasattr(o1, 'btn_kebab')}")
real_destroy(o1)


# --- CHECK 2: Cancelar in the confirm dialog discards NOTHING ---------------
seen2 = []
o2 = make_overlay(on_stop=lambda rec: seen2.append(("stop", rec)),
                   on_discard=lambda rec: seen2.append(("discard", rec)))
click_discard_with(o2, Gtk.ResponseType.CANCEL)
check("2 cancelling the confirm dialog calls neither discard() nor stop()",
      o2.rec.calls == [] and seen2 == [],
      f"calls={o2.rec.calls} callbacks={seen2}")
check("2b the overlay is still alive after Cancelar (not silently destroyed)",
      not o2.in_destruction(),
      f"in_destruction={o2.in_destruction()}")
real_destroy(o2)


# --- CHECK 3: confirming calls discard(), never stop() ----------------------
seen3 = []
o3 = make_overlay(on_stop=lambda rec: seen3.append(("stop", rec)),
                   on_discard=lambda rec: seen3.append(("discard", rec)))
click_discard_with(o3, Gtk.ResponseType.YES)
check("3 confirming the dialog calls rec.discard() and never rec.stop()",
      o3.rec.calls == ["discard"],
      f"calls={o3.rec.calls}")
check("3b the on_discard callback fires, NOT on_stop -- a discard must never "
      "reach the upload path",
      seen3 == [("discard", o3.rec)],
      f"callbacks={seen3}")


# --- CHECK 4: the exact proven copy from OFFICIAL-UI-SPEC.md §6.3 -----------
captured = {}
def capture_run(self):
    captured["title"] = self.get_title()
    lbl = None
    area = self.get_content_area()
    for child in area.get_children():
        if isinstance(child, Gtk.Label):
            lbl = child
            break
    captured["message"] = lbl.get_text() if lbl else None
    captured["cancel"] = self.get_widget_for_response(Gtk.ResponseType.CANCEL)
    captured["discard"] = self.get_widget_for_response(Gtk.ResponseType.YES)
    return Gtk.ResponseType.CANCEL

o4 = make_overlay()
Gtk.Dialog.run = capture_run
Gtk.Dialog.destroy = fake_destroy
try:
    o4._on_discard_clicked()
finally:
    Gtk.Dialog.run, Gtk.Dialog.destroy = real_run, real_destroy

check("4 dialog title matches the PROVEN i18n string",
      captured.get("title") == "Descartar gravação?",
      f"title={captured.get('title')!r}")
check("4b dialog message matches the PROVEN i18n string",
      captured.get("message") == "A gravação atual será excluída permanentemente. "
                                  "Essa ação não pode ser desfeita.",
      f"message={captured.get('message')!r}")
cancel_btn, discard_btn = captured.get("cancel"), captured.get("discard")
check("4c both buttons exist with the PROVEN labels",
      cancel_btn is not None and discard_btn is not None
      and cancel_btn.get_label() == "Cancelar"
      and discard_btn.get_label() == "Descartar",
      f"cancel={cancel_btn.get_label() if cancel_btn else None!r} "
      f"discard={discard_btn.get_label() if discard_btn else None!r}")
real_destroy(o4)


# --- CHECK 5: discard and stop share one guard, so they cannot both fire ----
# Mirrors the double-stop protection _do_stop already has -- CAN-321's
# double-upload defect, replayed for the discard path: a stop click racing a
# discard confirmation must resolve to exactly one terminal action, not two.
seen5 = []
o5 = make_overlay(on_stop=lambda rec: seen5.append("stop"),
                   on_discard=lambda rec: seen5.append("discard"))
o5._do_discard()
o5._do_stop()  # arrives second -- must be a no-op, not a second ending
check("5 a stop that arrives after a discard already ran is a no-op",
      o5.rec.calls == ["discard"] and seen5 == ["discard"],
      f"calls={o5.rec.calls} callbacks={seen5}")


passed = sum(results)
total = len(results)
print(f"\n{passed}/{total} passed")
if passed != total:
    sys.exit(1)
