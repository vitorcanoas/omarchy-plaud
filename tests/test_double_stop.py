"""Checks that a signal plus the Parar button stop the session exactly ONCE.

Usage: python3 tests/test_double_stop.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).

Why a new file rather than more of test_signals.py: every check there stubs the
overlay with a FakeOverlay whose _do_stop() only counts calls, so none of them
can express this failure at all. The defect lives in the REAL path -- the Parar
button queues _do_stop at PRIORITY_DEFAULT_IDLE (200) while the tray's signal
handler calls it synchronously at PRIORITY_DEFAULT (0), and with both pending
GLib dispatches the signal first and runs the queued call anyway. Two _do_stop
bodies, two on_stop() calls, two upload threads, two PlaudClients, two `done`s.
`on_stop`'s own `ended` guard cannot help: each call owns its own `ended` list.

So these drive a REAL Overlay, a REAL click on a mapped and sensitive button, a
REAL SIGTERM, and the REAL main.on_stop/_do_upload. Only the network
(PlaudClient) and ffmpeg (Recorder.start/stop) are stubbed, the former slow and
controllable so the signal can be timed against an upload actually in flight.

Judged by parts written and a completion marker, never by exit code: the losing
case exits 0. The duplicate upload fails fast, its `done` arrives first, and
Gtk.main_quit() runs while the real upload still has nine parts to write --
which loses the recording MORE silently than the signal bug this branch fixes.
"""
import os, pathlib, re, subprocess, sys, tempfile

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
PROBE = pathlib.Path(__file__).resolve().parent / "probe_double_stop.py"

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}", flush=True)


def run(mode, arm):
    """One probe run in its own process: it ends by returning from Gtk.main()."""
    home = tempfile.mkdtemp(prefix="dblstop-")
    out = pathlib.Path(home) / "out"
    env = dict(os.environ)
    env["PLAUD_LINUX_HOME"] = home
    # The launcher strips these for the same reason: a snap-contaminated
    # LD_LIBRARY_PATH makes the GTK import fail in a child process only.
    for k in ("LD_LIBRARY_PATH", "GTK_PATH", "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR"):
        env.pop(k, None)
    # A failing probe HANGS rather than crashing -- that is the "neither" half
    # of the invariant, a loop nobody can quit -- so the timeout is the only
    # thing that turns it into a FAIL instead of a suite that never returns.
    # subprocess.run's timeout uses kill(), i.e. SIGKILL, which is load-bearing:
    # the probe installs the very handlers under test, so it CATCHES SIGTERM and
    # `timeout` without -s KILL cannot end it either.
    try:
        r = subprocess.run([sys.executable, str(PROBE), repo, home, str(out), mode, arm],
                           capture_output=True, text=True, timeout=60, env=env)
        txt = r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        # bytes even under text=True -- CPython does not decode what it
        # collected before killing the child.
        def _s(b):
            return b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
        txt = _s(e.stdout) + _s(e.stderr) + "\nTIMEOUT"
    m = re.search(r"RESULT parts=(\d+)/10 marker=(\w+) clients=(\d+) stops=(\d+) ends=(\d+)", txt)
    if not m:
        return None, txt
    return dict(parts=int(m.group(1)), marker=m.group(2) == "True",
                clients=int(m.group(3)), stops=int(m.group(4)),
                ends=int(m.group(5))), txt


def fmt(r):
    if r is None:
        return "(probe produced no result)"
    return (f"(parts={r['parts']}/10, marker={r['marker']}, "
            f"clients={r['clients']}, stops={r['stops']}, ends={r['ends']})")


def ok_single(r):
    """One stop, one upload, ten parts, a completion marker. Anything else is a
    recording either lost or sent twice."""
    return (r is not None and r["stops"] == 1 and r["clients"] == 1
            and r["parts"] == 10 and r["marker"])


# --- CHECK 1: the control -- the Parar button alone --------------------------
# Establishes that the probe measures a WORKING session, so a green attack below
# means the defect is fixed rather than that the harness stopped measuring.
r, txt = run("tray", "control")
check("1 the Parar button alone uploads once and completes", ok_single(r), fmt(r))

# --- CHECK 2: THE DEFECT -- Parar plus a real SIGTERM ------------------------
# The button queues _do_stop; the signal runs it synchronously and higher; GLib
# then runs the queued one too. Red here was clients=2 stops=2 with parts=0/10
# and no marker.
r, txt = run("tray", "attack")
check("2 Parar plus a signal still stops the session exactly once", ok_single(r), fmt(r))

# --- CHECK 3: the control on the no-tray fallback ----------------------------
# A separate branch of run_tray with its own `done` policy, and the path that
# has already fooled three gates. Measured, not reasoned about.
r, txt = run("fallback", "control")
check("3 the fallback's Parar button alone uploads once and completes",
      ok_single(r), fmt(r))

# --- CHECK 4: the defect on the no-tray fallback -----------------------------
r, txt = run("fallback", "attack")
check("4 the fallback survives Parar plus a signal with one upload",
      ok_single(r), fmt(r))

# --- CHECK 5: _do_stop is idempotent, directly -------------------------------
# The narrowest statement of the fix, so a regression names itself instead of
# only showing up as a duplicate upload three checks above.
r, txt = run("tray", "twice")
check("5 calling _do_stop twice runs one stop and one upload", ok_single(r), fmt(r))

# --- CHECK 6: the signal path still defers -- CAN-321 must not regress -------
# A bare SIGTERM mid-upload, with no button press at all. The original defect:
# it must still yield 10/10 parts and a marker.
r, txt = run("tray", "signal-only")
check("6 a bare SIGTERM mid-upload still lets the upload finish", ok_single(r), fmt(r))

# --- CHECK 7: _signal_pending does not leak into the next session ------------
# _on_session_end cleared _busy/_started/_overlay but not _signal_pending, so a
# second session's first signal took the "already pending" branch: notify and
# return, recorder never stopped, no upload, no `done` -- the "neither" half of
# the invariant. Two full sessions in one process, the signal sent in the
# second, judged by the second session's own upload.
r, txt = run("tray", "second-session")
check("7 a second session's first signal still stops the recorder",
      ok_single(r), fmt(r))

# --- CHECK 8: the reverse order -- signal first, then the button -------------
# The signal stops the recorder synchronously; the user, seeing nothing happen
# yet, presses Parar on a button the overlay has not finished tearing down.
# Same two bodies, opposite order, and it goes red on the old tree the same way.
r, txt = run("tray", "signal-then-click")
check("8 a signal followed by Parar still stops the session exactly once",
      ok_single(r), fmt(r))

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
