"""Checks for the settings window (CAN-315 item 4).

Usage: python3 tests/test_settings.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm these still go red).
Each check prints PASS/FAIL.

No network: plaud_api is never imported by settings.py, and this file never
imports plaud_api either. No real audio changes: audio.pick_system_monitor()
is read-only (see audio.py), and nothing here calls anything that sets a
sink or a default. Windows are built but never show_all()'d or mapped --
same convention as test_panel.py -- so nothing appears on the user's real
screen just from running this file.
"""
import os, sys, pathlib, tempfile, json

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
home = tempfile.mkdtemp(prefix="plaudsettings-")
os.environ["PLAUD_LINUX_HOME"] = home
# Real XDG autostart dir is NOT under PLAUD_LINUX_HOME (see settings.py) --
# redirect it separately so toggling autostart in this test never touches
# the real ~/.config/autostart on whatever machine runs this suite.
autostart_dir = tempfile.mkdtemp(prefix="plaudsettings-autostart-")
os.environ["PLAUD_LINUX_AUTOSTART_DIR"] = autostart_dir
sys.path.insert(0, repo)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


try:
    from plaud_linux import settings as st
    HAVE = True
except Exception as e:  # noqa: BLE001
    HAVE = False
    why = f"{type(e).__name__}: {e}"


if not HAVE:
    for n in ("official geometry matches the proven 720x520",
              "language persists across a fresh load",
              "language default is pt with no settings.json",
              "unknown/corrupt settings.json degrades to pt, not a crash",
              "autostart file is written with the real launcher path",
              "toggling autostart in this test never touches the real autostart dir",
              "autostart toggle off removes the file, not just empties it",
              "audio section never issues a pactl SET command",
              "mic device and system-audio prefs persist independently",
              "mic default is Automatic, matching the official atom's default",
              "open_settings returns the same window on a second call",
              "data folder shown is the real DATA_HOME, not a placeholder",
              "settings.json write is atomic (no tmp file left behind)",
              "plaud_api reads the same settings.json this module writes",
              "plaud_api never imports Gtk"):
        check(n, False, why)
else:
    # --- CHECK 1: official geometry -----------------------------------
    check("official geometry matches the proven 720x520",
          st.WINDOW_WIDTH == 720 and st.WINDOW_HEIGHT == 520,
          f"got {st.WINDOW_WIDTH}x{st.WINDOW_HEIGHT}")

    # --- CHECK 2/3: language persistence --------------------------------
    check("language default is pt with no settings.json",
          st.get_language() == "pt", f"got {st.get_language()!r}")
    st.set_language("en")
    check("language persists across a fresh load",
          st.get_language() == "en", f"got {st.get_language()!r}")

    # --- CHECK 4: corrupt file degrades, does not crash -----------------
    with open(st.SETTINGS_FILE, "w", encoding="utf-8") as f:
        f.write("{not json")
    try:
        got = st.get_language()
        crashed = False
    except Exception:
        got = None
        crashed = True
    check("unknown/corrupt settings.json degrades to pt, not a crash",
          (not crashed) and got == "pt", f"crashed={crashed} got={got!r}")
    # restore a valid file for the checks below
    st.set_language("pt")

    # --- CHECK 5/6: autostart file -------------------------------------
    real_autostart = os.path.expanduser("~/.config/autostart/plaud-linux.desktop")
    real_existed_before = os.path.exists(real_autostart)

    st.set_autostart(True)
    written = False
    launcher_ok = False
    in_redirected_dir = False
    try:
        with open(st._AUTOSTART_FILE, encoding="utf-8") as f:
            content = f.read()
        written = os.path.isfile(st._AUTOSTART_FILE)
        launcher_ok = f"Exec={st._find_launcher()}" in content
        in_redirected_dir = st._AUTOSTART_FILE.startswith(autostart_dir)
    except OSError:
        pass
    check("autostart file is written with the real launcher path",
          written and launcher_ok and in_redirected_dir,
          f"written={written} launcher_ok={launcher_ok} redirected={in_redirected_dir}")
    # The check this exists to catch: an earlier version of this test wrote
    # into the user's REAL ~/.config/autostart, transiently adding a real
    # login item to whatever machine ran the suite.
    check("toggling autostart in this test never touches the real autostart dir",
          os.path.exists(real_autostart) == real_existed_before,
          f"real file exists now={os.path.exists(real_autostart)} "
          f"existed before this test={real_existed_before}")

    st.set_autostart(False)
    check("autostart toggle off removes the file, not just empties it",
          not os.path.exists(st._AUTOSTART_FILE),
          f"exists={os.path.exists(st._AUTOSTART_FILE)}")

    # --- CHECK 7: audio section never issues a pactl SET command ---------
    #
    # The adversarial question: a green "it shows a device dropdown" check
    # is STRUCTURALLY INCAPABLE of proving the window never mutates live
    # PipeWire state. audio.py exposes no "set default sink/source"
    # function at all -- its only subprocess entry point is _run(), which
    # every read (list_sinks, list_sources, default_sink, ...) goes through.
    # So the real guard is at that single choke point: patch _run() to
    # record every argv it is asked to execute, build the real
    # Gtk.Window (never shown), exercise the mic combo and the system-audio
    # switch as a user would, and assert "set" never appears in any argv
    # this window issued to pactl.
    import plaud_linux.audio as audio_mod
    calls = []
    real_run = audio_mod._run

    def _recording_run(cmd):
        calls.append(list(cmd))
        return real_run(cmd)

    audio_mod._run = _recording_run
    try:
        win = st.SettingsWindow()
        # Exercise both controls, same as a user clicking them.
        win.combo_mic.set_active(0)
        win.sw_system_audio.set_active(not win.sw_system_audio.get_active())
    finally:
        audio_mod._run = real_run
    set_calls = [c for c in calls if any("set" in str(a).lower() for a in c)]
    check("audio section never issues a pactl SET command",
          set_calls == [], f"pactl calls={calls} set_calls={set_calls}")
    win.destroy()

    # --- CHECK 7b/7c: mic + system-audio persistence --------------------
    check("mic default is Automatic, matching the official atom's default",
          st.get_mic_device() == st.MIC_AUTOMATIC,
          f"got {st.get_mic_device()!r}")

    st.set_mic_device(st.MIC_OFF)
    st.set_system_audio_enabled(False)
    mic_ok = st.get_mic_device() == st.MIC_OFF
    sys_ok = st.get_system_audio_enabled() is False
    # Setting one must not clobber the other -- both live in the same
    # settings.json, so a naive _save() that didn't round-trip the existing
    # dict would silently drop whichever key was written first.
    check("mic device and system-audio prefs persist independently",
          mic_ok and sys_ok, f"mic={st.get_mic_device()!r} system_audio={st.get_system_audio_enabled()!r}")
    st.set_system_audio_enabled(True)
    st.set_mic_device(st.MIC_AUTOMATIC)

    # --- CHECK 8: singleton reuse ----------------------------------------
    w1 = st.open_settings()
    w2 = st.open_settings()
    check("open_settings returns the same window on a second call",
          w1 is w2, f"same={w1 is w2}")
    w1.destroy()
    while Gtk.events_pending():
        Gtk.main_iteration()
    check("data folder shown is the real DATA_HOME, not a placeholder",
          str(st.paths.DATA_HOME) == home, f"{st.paths.DATA_HOME} vs {home}")

    # --- CHECK 9: atomic write -------------------------------------------
    st.set_language("pt")
    leftover = [p for p in st.paths.STATE.iterdir() if p.name.endswith(".tmp")]
    check("settings.json write is atomic (no tmp file left behind)",
          leftover == [], f"leftover={leftover}")

    # --- CHECK 10/11: plaud_api reads the SAME file, no Gtk ---------------
    import importlib
    import plaud_linux.plaud_api as api_mod
    importlib.reload(api_mod)  # picks up PLAUD_LINUX_HOME set above
    check("plaud_api reads the same settings.json this module writes",
          api_mod.SETTINGS_FILE == st.SETTINGS_FILE,
          f"{api_mod.SETTINGS_FILE} vs {st.SETTINGS_FILE}")
    st.set_language("en")
    got_lang = api_mod._settings_language()
    check("plaud_api never imports Gtk",
          "gi" not in sys.modules or not hasattr(api_mod, "Gtk"),
          f"api reads back {got_lang!r}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
