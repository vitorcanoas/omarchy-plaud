# Tests

The private review runner expects **477 checks across 44 files**. It verifies
GTK behavior, capture and stop lifecycles, desktop integration, local storage,
service payload construction and generation flow. `tests/run.py` is the source
of truth for the included files and per-file counts.

Run the suite in an awake Hyprland / Wayland graphical session:

```bash
dbus-run-session -- python3 tests/run.py
```

The runner prints results per file, checks the expected count and exits
nonzero on failure. The separate D-Bus session prevents login test callbacks
from reaching a running Plaud client. Some tests create real GTK windows or
invoke local screenshot tools; monitors must be awake. Keep sensitive desktop
content off screen during graphical screenshot tests. The suite uses scratch
`PLAUD_LINUX_HOME` directories and blocks network requests; no real account is
needed. Do not run two graphical suites against the same session at once.

## Focused checks

| Area | Files | What they prove |
|---|---|---|
| Recording and recovery | `test_overlay.py`, `test_hang.py`, `test_double_stop.py`, `test_sidecar.py`, `test_growth.py` | Capture/stop behavior and preservation of recorded audio and local data. |
| Highlights and screenshots | `test_panel.py`, `test_panel_refinement.py`, `test_live_highlights.py`, `test_frozen_capture.py`, `test_markring.py`, `test_audiomark.py` | Note editing, compact previews, region selection lifecycle and bounded flag work. |
| Desktop flow | `test_desktop_flow.py`, `test_tray.py`, `test_pill_pointer.py`, `test_shortcuts.py`, `test_settings.py` | Explicit start, status controls, optional shortcuts and preferences. |
| Login and cloud contract | `test_auth_flow.py`, `test_login.py`, `test_region.py`, `test_wt_reexchange.py`, `test_markpath.py`, `test_genchoice.py` | Local OAuth handoff, bounded session recovery, payloads and generation choice with blocked network. |
| Integration boundaries | `test_integration_safety.py`, `test_path_safety.py` | Temporary-home installer/autostart behavior, link and special-file refusal, safe logs and recording retention. |
| Generation picker | `test_generation_web.py`, `test_generation_window.py` | Window fallback, message handling, timers and cleanup without requiring a real Plaud account. |

Each file can be run under the same isolated D-Bus command, for example:

```bash
dbus-run-session -- python3 tests/test_panel_refinement.py
dbus-run-session -- python3 tests/test_integration_safety.py
dbus-run-session -- python3 tests/test_path_safety.py
```

`test_panel_refinement.py --manual` opens a normal panel with a fake recorder
and synthetic slides for visual inspection; closing it exits. The manual
`test_pill_pointer.py --manual` path uses a fake recorder for physical pointer
checks. Programmatic button or compositor commands do not prove physical
pointer delivery. Real Plaud account upload and cloud generation are separate
authorized checks, not properties established by this suite.

When adding a check, set `PLAUD_LINUX_HOME` to a fresh temporary directory
before importing application modules, block network transport, and preserve
the separate D-Bus session. Update the expected per-file count in
`tests/run.py` when adding or removing checks.

Additional focused checks: `test_failure_boundaries.py` covers stop/source/error handling; `test_settings_usability.py` covers close/Escape, source-state preservation, background discovery, timeout and explicit retry. These are synthetic checks, not real account verification.
