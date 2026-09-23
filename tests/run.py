"""Run every check file and summarize.

Usage: python3 tests/run.py [repo]

`repo` is forwarded to each check, so the whole suite can be pointed at a
`git worktree` of an older revision to confirm it still goes red there.
Exits non-zero if any check failed.
"""
import pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
repo = sys.argv[1] if len(sys.argv) > 1 else str(HERE.parent)

# Slowest last: instance spawns real processes and waits on real bus timeouts.
# The number is how many checks the file is expected to emit. It is asserted,
# not decorative: a check that silently stops running still reports "0 failed",
# and without this the summary would print a smaller total and still say ALL
# PASS. Measured: making one file exit 0 immediately turned 47 into 45 and the
# run stayed green. Update a number here only when deliberately adding or
# removing a check.
FILES = [("test_css.py", 2), ("test_lifetime.py", 4), ("test_notify.py", 4),
         ("test_tray.py", 4), ("test_overlay.py", 9), ("test_layershell.py", 5),
         ("test_pill_pointer.py", 8),
         ("test_nolayershell.py", 4), ("test_tray_valueerror.py", 5), ("test_region.py", 29),
         ("test_growth.py", 12), ("test_hang.py", 9), ("test_sidecar.py", 8),
         ("test_integration_safety.py", 20), ("test_path_safety.py", 12),
         ("test_signals.py", 11), ("test_double_stop.py", 8),
         ("test_instance.py", 11), ("test_wshost.py", 6), ("test_login.py", 7),
         ("test_wt_reexchange.py", 12),
         ("test_staleupload.py", 6), ("test_srctoggle.py", 14), ("test_fifine_fallback.py", 6),
         ("test_markpath.py", 15), ("test_markring.py", 12),
         ("test_genchoice.py", 16), ("test_panel.py", 9),
         ("test_panel_refinement.py", 5), ("test_frozen_capture.py", 17),
         ("test_recordingmain.py", 6),
         ("test_settings.py", 15), ("test_shortcuts.py", 8),
         ("test_library.py", 13), ("test_discard.py", 6), ("test_discard_ui.py", 9),
         ("test_live_highlights.py", 15), ("test_audiomark.py", 15), ("test_desktop_flow.py", 14), ("test_auth_flow.py", 10),
         ("test_generation_web.py", 21), ("test_generation_window.py", 37)]
EXPECTED_TOTAL = sum(n for _, n in FILES)

total_pass = total_fail = 0
failed_files = []

for name, expected in FILES:
    print(f"===== {name} =====", flush=True)
    # Timed out rather than run bare: several checks wait on a real bus, a real
    # child process or a real GTK loop, and a check that hangs would otherwise
    # hang the whole suite with no output to say which one -- capture_output
    # means nothing streams, so a hang looks exactly like slowness. Generous,
    # because test_instance legitimately takes minutes when the bus is
    # contended; this is a backstop, not a deadline.
    try:
        r = subprocess.run([sys.executable, str(HERE / name), repo],
                           capture_output=True, text=True, timeout=900)
        out = r.stdout + r.stderr
        rc = r.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        rc = "TIMEOUT"
    print(out, end="" if out.endswith("\n") else "\n", flush=True)
    npass = len(re.findall(r"^PASS ", out, re.M))
    nfail = len(re.findall(r"^FAIL ", out, re.M))
    total_pass += npass
    total_fail += nfail
    # A file that dies before printing its checks reports rc!=0 with no FAIL
    # lines. Count that as a failure, or the summary would call it clean.
    if rc != 0 and nfail == 0:
        failed_files.append(
            f"{name} ({'timed out' if rc == 'TIMEOUT' else f'exited {rc}'}"
            " with no result)")
    elif nfail:
        failed_files.append(f"{name} ({nfail} failed)")
    if npass + max(nfail, 0) != expected:
        failed_files.append(
            f"{name} (ran {npass + max(nfail, 0)} checks, expected {expected})")
    print(f"----- {name}: {npass} passed, {max(nfail, 0)} failed, rc={rc}\n",
          flush=True)

print("=" * 60)
print(f"TOTAL: {total_pass} passed, {total_fail} failed, across {len(FILES)} files")
for f in failed_files:
    print(f"  FAILED: {f}")
print("ALL PASS" if not failed_files else "SOME FAILED")
sys.exit(0 if not failed_files else 1)
