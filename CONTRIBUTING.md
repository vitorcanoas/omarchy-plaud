# Contributing

This is a private review copy of an independent Plaud desktop client for
Omarchy / Arch Linux, GTK3, Wayland and PipeWire.

Runtime modules are in `plaud_linux/`; `audio.py` owns capture and graceful stop,
`main.py` and `tray.py` own the session, `panel.py` owns live annotations,
`plaud_api.py` implements the observed service contract, and `generation_web.py`
loads the official picker. `safe_io.py` and `integration.py` handle user-local
installation, startup and bounded housekeeping. The optional `omarchy/` widget
only launches the installed app after a click; the native tray is preferred.

Use Arch system dependencies from [installation](docs/INSTALL.md). With an awake
Wayland desktop, run:

```bash
dbus-run-session -- python3 tests/run.py
python3 -m compileall -q plaud_linux tests
bash -n install.sh bin/plaud-linux
git diff --check
```

Tests use temporary data and blocked network. CI checks syntax; it does not prove
real audio capture or compatibility with the current Plaud service. Real account
verification is separate. Code and documentation use English; UI strings use
Portuguese. Changes should include focused regression evidence and retain local
audio on failures. Opening the app must never start capture by itself.

The application is not a sandbox and machine-derived token encryption does not
protect against other processes running as the same user. Report sensitive
findings privately as described in [SECURITY.md](SECURITY.md). Do not attach
credentials, account data or private recordings to review artifacts.
