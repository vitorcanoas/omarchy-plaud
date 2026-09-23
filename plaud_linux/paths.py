#!/usr/bin/env python3
"""
plaud-linux :: runtime data paths

Code lives in the project dir (versioned in Git). USER DATA — recordings, tokens,
device id, logs — lives under XDG data home, never in the repo. Override the data
root with PLAUD_LINUX_HOME if you want (useful for tests).
"""
try:
    from . import safe_io
except ImportError:
    import safe_io

DATA_HOME = safe_io.data_home()

RECORDINGS = DATA_HOME / "recordings"
LOGS = DATA_HOME / "logs"
STATE = DATA_HOME / "state"
SCREENSHOTS = RECORDINGS / "screenshots"

for _d in (RECORDINGS, LOGS, STATE):
    _d.mkdir(parents=True, exist_ok=True)

# How long derived files are kept. Recordings are NOT derived and are never
# covered by any of this.
LOG_KEEP_DAYS = 30
SHOT_KEEP_DAYS = 30
APP_LOG_MAX_BYTES = 2 * 1024 * 1024
# What survives a prune. Keeping a tail rather than truncating to zero is what
# makes an overlapping session survivable: it loses old lines nobody was going
# to read, not the run someone is currently tailing.
APP_LOG_KEEP_BYTES = 256 * 1024


def prune_old_logs():
    """Bound derivatives through retained descriptors; never prune audio.

    Explicit DATA_HOME/recordings relocation remains supported. Unsafe
    housekeeping links or special files are skipped without affecting capture.
    """
    safe_io.prune(DATA_HOME, RECORDINGS, log_days=LOG_KEEP_DAYS,
                  shot_days=SHOT_KEEP_DAYS, max_log=APP_LOG_MAX_BYTES,
                  keep_log=APP_LOG_KEEP_BYTES)
