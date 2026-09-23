"""Check for CAN-314 item 10: the upload notification burst.

Usage: python3 tests/test_notify.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

A 2 h recording is ~31 MB at the 36.2 kbps this app really produces, which is
7 multipart parts -- so 12 notifications for one upload, each a separate popup
the user has to dismiss. The fix collapses only the *progress* stream into one
notification that updates in place, and must leave every warning and terminal
message its own popup: those are the ones that matter, and the burst fix
exists to protect them, not to bury them.

This drives the real session bus, so it asserts what the user actually sees.
"""
import os, sys, tempfile, pathlib, types

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp(prefix="notifchk-")
sys.path.insert(0, repo)

stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

from plaud_linux import main as main_mod

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")

# Capture what reaches the bus instead of painting the user's screen.
sent = []
real_bus_notify = getattr(main_mod, "_bus_notify", None)

_next_id = [100]

def fake_bus(title, body, replaces_id):
    """Model the real daemon: replaces_id=0 mints a new id, anything else is
    echoed back unchanged. Verified against gnome-shell 46.0, which returned
    39 then 40 for replaces_id=0 and 39 for each reuse of 39."""
    sent.append((title, body, replaces_id))
    if replaces_id == 0:
        _next_id[0] += 1
        return _next_id[0]
    return replaces_id

# --- CHECK 0: the transport is D-Bus, not `notify-send -p` ------------------
# notify-send prints "1" for every call under Portal notifications on this
# machine (libnotify 0.8.3, confined mode), so an id read from it would make
# ALL notifications replace each other -- including the warnings. Only the
# real bus returns usable ids.
check("0 a replaceable-notification transport exists",
      real_bus_notify is not None,
      "(main._bus_notify present)" if real_bus_notify else "(no _bus_notify — using notify-send?)")

if real_bus_notify is None:
    print()
    print("SOME FAILED")
    sys.exit(1)

main_mod._bus_notify = fake_bus
main_mod._progress_id = 0

# --- CHECK 1: progress messages collapse into ONE notification --------------
sent.clear()
main_mod._progress_id = 0
for m in ["presigned…", "upload 1/7…", "upload 2/7…", "upload 3/7…",
          "upload 4/7…", "upload 5/7…", "upload 6/7…", "upload 7/7…",
          "merge…", "confirm…", "generating…", "done"]:
    main_mod.progress(f"Enviando: {m}")
ids = {r for (_, _, r) in sent}
# first call passes 0 (new), the rest reuse the id the bus handed back
reused = [r for (_, _, r) in sent[1:]]
check("1 the 12-message upload stream reuses one notification",
      len(sent) == 12 and sent[0][2] == 0 and len(set(reused)) == 1 and reused[0] != 0,
      f"(replaces_ids = {[r for (_,_,r) in sent]})")

# --- CHECK 2: warnings are NOT collapsed into the progress notification -----
# The two silence/segment-loss warnings and the failure message must each get
# their own popup. Collapsing them is the exact harm this fix must not cause.
sent.clear()
main_mod._progress_id = 0
main_mod.progress("Enviando: upload 1/7…")
main_mod.notify("Plaud Linux", "⚠️ A gravação parece muda (-91.0 dB)")
main_mod.notify("Plaud Linux", "⚠️ 2 trecho(s) da gravação se perderam")
main_mod.notify("Plaud Linux", "❌ Falha no upload")
warn_ids = [r for (_, _, r) in sent[1:]]
check("2 warnings each get their own notification",
      len(sent) == 4 and all(r == 0 for r in warn_ids),
      f"(warning replaces_ids = {warn_ids}, expected all 0)")

# --- CHECK 3: a second session does not resurrect the old notification ------
# Reusing a stale id across sessions would either replace a notification the
# user already dismissed, or silently no-op. Progress must reset per session.
sent.clear()
main_mod._progress_id = 0
main_mod.progress("Enviando: presigned…")
first_session_first = sent[0][2]
main_mod._progress_id = 0            # what a new session must do
main_mod.progress("Enviando: presigned…")
check("3 a new session starts a fresh notification",
      first_session_first == 0 and sent[-1][2] == 0,
      f"(ids = {[r for (_,_,r) in sent]})")

main_mod._bus_notify = real_bus_notify
print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
