# Changelog

This project keeps an **Unreleased** section while the private review copy is
under validation. No public release is implied.

## [Unreleased]

### Added

- Native recording, upload and generation-choice cards; explicit recording
  start, independent microphone and system-audio controls, a visible status
  pill, and local recovery for failed uploads.
- Timestamped notes, audio highlights and region screenshots in a resizable
  panel. Highlights can be edited or removed while original local media stays
  available. The flag analyzes the preceding audio while recording and reuses
  a successful result at upload.
- Custom generation through the official Plaud Web picker in a dedicated
  WebKit window, with browser fallback; automatic and custom completion
  notices offer a link to the generated notes.
- Optional background startup, Hyprland shortcuts and a source-checkout
  installer. The tray remains the primary desktop access point.

### Changed

- Recent uploads start collapsed and expand on request.
- Enter completes a text highlight and focuses the next-note field;
  Shift+Enter keeps multiline editing. Screenshot previews preserve aspect
  ratio at up to 90 pixels high, with the remove button beside the image.
- Region selection freezes the visible frame before choosing an area and
  releases its owned helper after success, cancel, failure, stop or exit.
- The recording pill can be moved with Super + left-drag on Hyprland while
  its controls remain clickable. Placement resets for each recording.
- User-local installation and autostart now refuse unsafe or unrecognized
  targets. Shortcut removal changes only its marked block and backs up user
  configuration; validation failure restores the prior bindings. Launcher
  logs follow `PLAUD_LINUX_HOME` and unsafe log destinations are skipped.
  Bounded cleanup touches eligible derived logs and screenshots, preserving
  recorded audio.
- The isolated regression runner now expects 459 checks across 42 files.

### Fixed

- Recorded audio remains available after annotation, upload or generation
  failures. Duplicate stop paths cannot attach the same session twice.
- OAuth callbacks are registered once and consumed codes are not exchanged
  again. A refused workspace refresh receives one bounded recovery attempt
  using the existing user login; an invalid login still requires reconnecting.
- The generation picker remains usable when Hyprland initially places it as
  fullscreen, and its language list stays inside the window.
