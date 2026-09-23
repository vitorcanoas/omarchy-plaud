# Desktop compatibility and private review

Target: Omarchy / Arch Linux, Hyprland / Wayland, GTK3 and PipeWire.
This repository remains private; no marketplace submission or upstream approval
is implied. The optional bar widget requires the desktop application to be
installed separately. Prefer the application's native tray to avoid duplicate
launcher icons.

## Verify a candidate

Use an awake graphical session and isolated data:

```bash
dbus-run-session -- python3 tests/run.py
python3 -m compileall -q plaud_linux tests
bash -n install.sh bin/plaud-linux
```

Confirm that opening the launcher/tray does not record, that recording is visible,
that stopped audio remains on disk after cloud failures, and that annotation
controls remain usable on both normal and rotated monitors. Test Enter and
Shift+Enter, screenshot selection and Escape, compact thumbnails and removal.
A mocked transport does not prove a current real-account cloud operation.

Installation is per-user and uses an existing checkout. Dependencies are managed
through Arch packages. The optional shortcut installation backs up only the user
bindings file, validates Hyprland, and restores it when validation fails; it does
not change packaged Omarchy configuration. Review unusual filesystem layouts
before using the installer. Unsupported checkout path characters are rejected.

File and process hardening protects these integration operations from common
pathname substitutions and unbounded helpers; the desktop application is not a
sandbox against a compromised process running under the same user.

Public distribution still requires a separate owner decision and resolution of
artwork rights in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
Marketplace review is tied to an exact revision; previous acceptance of
another project does not transfer here.
