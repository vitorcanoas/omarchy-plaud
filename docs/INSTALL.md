# Install, update and remove

Plaud Linux targets Arch Linux / Omarchy with Hyprland, Wayland, GTK3 and
PipeWire. Installation is per user from a source checkout. It is not a signed
binary release or standalone wheel. Keep the checkout at its installed path.

## Get the private repository

Repository access is required. Authenticate with your own GitHub account; do
not put access tokens in the clone URL.

```bash
git clone https://github.com/vitorcanoas/omarchy-plaud-linux.git
cd omarchy-plaud-linux
```

The installer accepts checkout paths made of letters, digits, `_`, `-`, `.`,
and `/`. It rejects spaces and other unsupported characters before changing
integration files. A GitHub permission error means you need repository access.

## System packages

On an up-to-date Arch system, review and install:

```bash
sudo pacman -S --needed python python-gobject gtk3 gtk-layer-shell python-cairo \
  python-requests python-cryptography python-pillow ffmpeg libpulse \
  pipewire-pulse grim slurp hyprpicker xdg-utils desktop-file-utils
```

For the custom-generation picker in its own window:

```bash
sudo pacman -S --needed webkit2gtk-4.1
```

Without WebKit, custom generation opens in the browser. The installer does not
install packages. Use system packages for GTK; do not use `sudo pip`.

## Install for your user

Run as the desktop user, without `sudo`:

```bash
./install.sh
plaud-linux --login
plaud-linux
```

Opening the app does not record; click **Iniciar gravação** explicitly. To add
background login startup and global shortcuts:

```bash
./install.sh --autostart --hypr-shortcuts
hyprctl configerrors
```

The installer creates a launcher symlink in `~/.local/bin`, user desktop and
`plaud://` protocol entries, and a user icon. Autostart and Hyprland shortcuts
are opt-in. It changes only user configuration, never `/usr/share/omarchy`.
Recognized entries are replaced through checked file operations, with backups
when existing regular files change. Unsafe links, special files and entries
belonging to another application are refused. The shortcut operation backs up
`~/.config/hypr/bindings.lua`, reloads Hyprland, checks configuration errors
and restores its prior content if validation fails. Keep any generated backup
until you are satisfied with the result.

Use Omarchy's native tray and pin `plaud-linux` if desired. The optional
`community.plaud-linux` bar widget is a second launcher and does not install
the application. Keep Omarchy's system microphone indicator.

## Update

Finish active recording, upload and generation, then exit through the tray:

```bash
git status --short
git pull --ff-only
./install.sh
plaud-linux --background
```

Resolve local changes or a divergent branch before pulling; do not reset away
unfinished work. Existing opt-in shortcuts are left in place. Reinstall after
moving the checkout so launcher and desktop paths remain valid.

## Remove shortcuts or the application

To remove only the marked Plaud shortcut block:

```bash
./install.sh --remove-hypr-shortcuts
hyprctl configerrors
```

This operation does not run installation or enable autostart. It backs up the
user bindings file when it changes and preserves all text outside the marked
block. A malformed block is refused without replacing the file. Use
**Preferências** to switch off autostart; the app removes only a recognized
Plaud entry.

There is no full uninstall command. To remove the app, first finish active
work and exit. Then remove only its launcher symlink in `~/.local/bin`, the
`plaud-linux.desktop` and `plaud-note.desktop` entries in
`~/.local/share/applications/`, and its icon in
`~/.local/share/icons/hicolor/256x256/apps/`. If another application should
handle `plaud://`, register it as the default handler. Remove an optional bar
widget from your layout separately.

Keep `~/.local/share/plaud-linux/` (or your configured data root) unless you
have deliberately backed up and decided to delete recordings, notes and login
state. Removing the checkout does not delete that data.
