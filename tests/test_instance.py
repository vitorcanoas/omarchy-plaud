"""Checks for CAN-314 item 4: single instance + plaud:// routing to the live process.

Usage: python3 tests/test_instance.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on 6ddf662, PASS after.

Pre-fix there is no bus name at all, so every launch is an owner: a second bare
launch starts a second recording, and the browser's plaud:// callback runs in a
*separate* process -- which is how it came to run prune_old_logs() and truncate
the live session's app.log.

Several checks spawn REAL processes (an owner sitting in a real Gtk.main(), a
client that must exit with no loop of its own), because the failures under test
-- a 25 s stall, an invisible eternal process -- only exist across process
boundaries. Every spawn is under a timeout and reaped in a finally, so a hang is
reported as a failed check rather than blocking the run.

This file re-execs itself under a PRIVATE session bus; see _isolate_bus().
"""
import os, sys, pathlib, shutil, signal, subprocess, tempfile, time, textwrap, urllib.parse


def _isolate_bus():
    """Re-exec this file under a session bus nobody else can reach.

    The single-instance mechanism identifies itself by ONE well-known bus name,
    'ai.plaud.LinuxRecorder'. That name is a single global resource per session
    bus, so any two runs of this file sharing a bus fight over it: whichever
    spawn_owner() loses reports the identical, and thoroughly misleading,
    "could not establish a live instance".

    That is not hypothetical. Four agents measured this suite on the SAME
    untouched master and got four different totals -- 51, 49, 50, 45 -- with
    every difference inside this file. The runs were concurrent, in separate
    `git worktree` checkouts, all sharing the one login session bus. Measured
    directly: a sibling worktree's CHECK 5 blocker was holding the name while
    this file's checks ran. The variance tracked how many siblings happened to
    be running, not anything in the code.

    An unstable baseline is worse than a failing one, because it destroys the
    red-then-green measurement the whole project verifies changes with: no
    agent could honestly say "51/51 before, 51/51 after" when the number moved
    on its own.

    So isolate rather than coordinate. A private bus makes the checks own the
    name they are asserting about, which is what they meant all along -- and it
    keeps them honest, since they still drive real bus-name ownership across
    real process boundaries. Nothing is stubbed and no assertion is relaxed.

    Done by re-exec rather than by asking callers to wrap the command, so the
    property holds however it is invoked: directly, through tests/run.py, or by
    somebody who has never read this comment.
    """
    if os.environ.get("PLAUD_CHECK_PRIVATE_BUS"):
        return                      # already inside our own bus
    dbus = shutil.which("dbus-run-session")
    if not dbus:
        # Degrade loudly rather than silently sharing a bus: without isolation
        # the results are only trustworthy if nothing else is running, and the
        # reader cannot tell from the output that that was the case.
        print("WARNING: dbus-run-session not found -- running on the SHARED "
              "session bus. Results are unreliable if another copy of this "
              "file is running.", flush=True)
        return
    os.environ["PLAUD_CHECK_PRIVATE_BUS"] = "1"
    os.execvp(dbus, [dbus, "--", sys.executable, os.path.abspath(__file__)]
                    + sys.argv[1:])


_isolate_bus()

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
HOME = tempfile.mkdtemp(prefix="instchk-")
os.environ["PLAUD_LINUX_HOME"] = HOME
sys.path.insert(0, repo)

import types
stub = types.ModuleType("requests")
def _boom(*a, **k):
    raise AssertionError("NETWORK CALL — test invalid")
stub.get = stub.post = stub.put = _boom
stub.exceptions = types.SimpleNamespace(RequestException=Exception)
sys.modules["requests"] = stub

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gio
from plaud_linux import main as main_mod

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# The auth_code the real flow carries: base64 with the three characters that
# actually survive-or-die in a URL roundtrip. If routing mangles these, login
# fails with "authorization code invalid" and looks like a server problem.
#
# Percent-encoded, because that is what the browser really hands the plaud://
# handler. Worth stating why, since an earlier draft of this check used a
# LITERAL '+' and caught a corruption that is real but not this item's: parse_qs
# form-decodes '+' to a space, so extract_auth_code() returns 'AbC def/123=='.
# Measured identically on 6ddf662, so it is pre-existing, not a regression --
# and Gio.File is innocent either way, preserving the query byte-for-byte.
# Asserting the '%2B' form tests what routing must actually preserve.
CODE = "AbC+def/123=="
URL = "plaud://login?auth_code=" + urllib.parse.quote(CODE, safe="") + "&from=desktop"

def _code_of(out):
    """The auth_code the fallback actually received, decoded.

    A substring match against the raw output cannot work: the URL on the wire
    is percent-encoded, so the literal '+' never appears in it. Parsing is also
    the stronger assertion -- it proves the code survives decoding, which is
    the thing login_with_auth_code() depends on.
    """
    for line in out.splitlines():
        if line.startswith("HANDLED-LOCALLY:"):
            q = urllib.parse.urlparse(line[len("HANDLED-LOCALLY:"):]).query
            return urllib.parse.parse_qs(q).get("auth_code", [None])[0]
    return None


ENV = dict(os.environ)
for v in ("LD_LIBRARY_PATH", "GTK_PATH", "XDG_DATA_HOME", "GIO_MODULE_DIR",
          "GI_TYPELIB_PATH", "LD_PRELOAD", "GSETTINGS_SCHEMA_DIR"):
    ENV.pop(v, None)
ENV["PLAUD_LINUX_HOME"] = HOME
ENV["PYTHONUNBUFFERED"] = "1"

# A live recording session, driven through the app's OWN main(). It goes in via
# main() with no args and a stubbed start_session that does what the real one
# does structurally: claim whatever main() hands it, then sit in a real
# Gtk.main(). Nothing here calls _claim_instance directly.
#
# This is what makes the check honest on both sides. On the pre-fix tree main()
# has no bus name to give, so this owner legitimately holds nothing and the
# second launch really does start a second recording -- the failure being
# tested. Probing for a helper that does not exist yet would instead fail every
# check with "missing attribute", which proves nothing about behaviour.
OWNER_SRC = textwrap.dedent('''
    import os, sys, types
    sys.path.insert(0, %(repo)r)
    stub = types.ModuleType("requests")
    def _boom(*a, **k): raise AssertionError("NETWORK")
    stub.get = stub.post = stub.put = _boom
    stub.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = stub
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib, Gio
    from plaud_linux import main as m
    import plaud_linux.login as L
    m.notify = lambda *a, **k: None
    L.handle_url = lambda u: print("ROUTED:" + u, flush=True)

    def fake_session(app=None):
        # Mirrors the real start_session(): wire `open` if main() gave us an
        # instance to wire, then own the loop.
        if app is not None:
            action = Gio.SimpleAction.new("show", None)
            action.connect("activate", lambda *_: print("PANEL-SHOWN", flush=True))
            app.add_action(action)
            app.connect("open",
                        lambda a, f, n, h: [L.handle_url(x.get_uri()) for x in f])
        print("OWNER-READY", flush=True)
        GLib.timeout_add(%(ms)d, lambda: (Gtk.main_quit(), False)[1])
        Gtk.main()

    m.start_session = fake_session
    # Since CAN-310 a bare launch goes to tray.run_tray(), not start_session(),
    # so stubbing start_session alone left the REAL tray running: it built an
    # indicator, entered its own Gtk.main() with nothing to quit it, and this
    # owner never printed OWNER-READY. Measured: the whole file hung on check 1
    # and produced no results at all, on a free bus.
    #
    # Stubbed only if present, and never imported unconditionally: on a
    # revision before the tray there is no such module, and requiring one would
    # make every check fail with an import error rather than exercise the
    # behaviour -- which is the same trap the comment above warns about.
    try:
        from plaud_linux import tray as _tray
    except Exception:
        _tray = None
    if _tray is not None:
        _tray.run_tray = lambda app=None, **kwargs: fake_session(app=app)
    sys.argv = ["plaud-linux"]
    m.main()
    print("OWNER-DONE", flush=True)
''')


def _name_owner():
    """Who holds the instance name right now, or None. Diagnostic only."""
    try:
        c = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = c.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "GetNameOwner",
                        GLib.Variant("(s)", ("ai.plaud.LinuxRecorder",)),
                        None, Gio.DBusCallFlags.NONE, 2000, None)
        return r.unpack()[0]
    except Exception:
        return None


def spawn_owner(ms=15000):
    """Start a live session and wait until its loop is running."""
    p = subprocess.Popen(
        [sys.executable, "-c", OWNER_SRC % {"repo": repo, "ms": ms}],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=ENV)
    t0 = time.time()
    while time.time() - t0 < 20:
        line = p.stdout.readline()
        if not line:
            break
        if "OWNER-READY" in line:
            return p
    p.kill(); p.wait()
    return None


def _no_owner_detail():
    """Why spawn_owner() failed, in terms the reader can act on.

    The bare "could not establish a live instance" cost four agents a day
    between them: it names the symptom and says nothing about the cause, so it
    reads as a bug in the code under test when it is nearly always contention
    for the bus name. Say who holds it.
    """
    holder = _name_owner()
    if holder is not None:
        return ("(could not establish a live instance -- the name "
                f"ai.plaud.LinuxRecorder is already owned by {holder}; another "
                "copy of these checks is probably running on this bus)")
    return "(could not establish a live instance -- the name is unowned)"


def run_main(args, timeout=40, env=None):
    """Run the app's main() with `args` in a child. Returns (rc, output)."""
    src = textwrap.dedent('''
        import sys, types
        sys.path.insert(0, %(repo)r)
        stub = types.ModuleType("requests")
        def _boom(*a, **k): raise AssertionError("NETWORK")
        stub.get = stub.post = stub.put = _boom
        stub.exceptions = types.SimpleNamespace(RequestException=Exception)
        sys.modules["requests"] = stub
        import gi
        gi.require_version("Gtk", "3.0")
        from plaud_linux import main as m
        m.notify = lambda t, b: print("NOTIFY:" + b, flush=True)
        # If anything reaches a real session we must know: starting a second
        # recording is the failure this whole item exists to prevent. Both
        # doors are stubbed -- since CAN-310 a bare launch arrives through
        # tray.run_tray(), so watching start_session alone would let a second
        # tray start unnoticed AND hang here in its own Gtk.main().
        m.start_session = lambda *a, **k: print("STARTED-SESSION", flush=True)
        try:
            from plaud_linux import tray as _tray
        except Exception:
            _tray = None
        if _tray is not None:
            _tray.run_tray = lambda app=None, **kwargs: print("STARTED-SESSION", flush=True)
        import plaud_linux.login as L
        L.handle_url = lambda u: print("HANDLED-LOCALLY:" + u, flush=True)
        L.begin_login = lambda: print("BEGAN-LOGIN", flush=True)
        sys.argv = ["plaud-linux"] + %(args)r
        m.main()
        print("MAIN-RETURNED", flush=True)
    ''') % {"repo": repo, "args": args}
    try:
        r = subprocess.run([sys.executable, "-c", src], capture_output=True,
                           text=True, timeout=timeout, env=env or ENV)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        return None, out if isinstance(out, str) else out.decode("utf-8", "replace")


# --- CHECK 1: a second bare launch must not start a second recording --------
owner = spawn_owner()
try:
    if owner is None:
        check("1 second bare launch does not start a second recording",
              False, _no_owner_detail())
    else:
        rc, out = run_main([])
        started = "STARTED-SESSION" in out
        oout, _ = owner.communicate(timeout=25)
        declined = "PANEL-SHOWN" in oout
        check("1 second bare launch does not start a second recording",
              (not started) and declined and rc == 0,
              f"(started={started}, panel_shown={declined}, rc={rc})")
finally:
    if owner:
        owner.kill(); owner.wait()

# --- CHECK 2: plaud:// reaches the LIVE instance, auth_code intact ----------
# The base64 code roundtrips through Gio.File as a URI, so this asserts the
# exact characters (+ / =) survive -- not merely that *something* arrived.
owner = spawn_owner()
try:
    if owner is None:
        check("2 plaud:// reaches the live instance with the auth_code intact",
              False, _no_owner_detail())
    else:
        rc, out = run_main([URL])
        # The client must not have handled it itself...
        local = "HANDLED-LOCALLY" in out
        # ...and the owner must have received it. Read the owner's remaining
        # output; its own timeout ends the loop.
        try:
            oout, _ = owner.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            owner.kill(); oout, _ = owner.communicate()
        routed = [l for l in oout.splitlines() if l.startswith("ROUTED:")]
        arrived = None
        if routed:
            q = urllib.parse.urlparse(routed[0][len("ROUTED:"):]).query
            arrived = urllib.parse.parse_qs(q).get("auth_code", [None])[0]
        check("2 plaud:// reaches the live instance with the auth_code intact",
              bool(routed) and arrived == CODE and not local,
              f"(routed={bool(routed)}, code={arrived!r}, handled_locally={local})")
finally:
    if owner and owner.poll() is None:
        owner.kill(); owner.wait()

# --- CHECK 3: --status works with NO instance, and does not route -----------
# A read-only CLI probe. It must answer from local state alone: routing it
# would make it wait on another process's loop, which check 5 shows costs 25 s.
rc, out = run_main(["--status"])
answered = "logged_in:" in out
check("3 --status answers with no instance running",
      answered and rc == 0 and "STARTED-SESSION" not in out,
      f"(answered={answered}, rc={rc})")

# And with an instance running it must STILL answer locally, not route.
owner = spawn_owner()
try:
    if owner is None:
        check("3b --status does not route to a live instance", False,
              _no_owner_detail())
    else:
        t0 = time.time()
        rc, out = run_main(["--status"])
        dt = time.time() - t0
        # Must not have consulted the bus at all: a routed --status inherits the
        # 25 s stall measured in check 5. Answering promptly is the assertion.
        check("3b --status does not route to a live instance",
              "logged_in:" in out and rc == 0 and dt < 10,
              f"(answered={'logged_in:' in out}, rc={rc}, {dt:.1f}s)")
finally:
    if owner:
        owner.kill(); owner.wait()

# --- CHECK 4: stale ownership self-heals after SIGKILL ----------------------
# A killed owner leaves no state to clean up: the bus drops the name with the
# connection. Asserting this is what lets the fix ship with no pidfile and no
# "delete this file if the app crashed" instruction.
owner = spawn_owner()
if owner is None:
    check("4 stale ownership self-heals after SIGKILL", False,
          _no_owner_detail())
else:
    owner.send_signal(signal.SIGKILL)
    owner.wait()
    time.sleep(1.0)   # let the bus notice the dropped connection
    rc, out = run_main([])
    check("4 stale ownership self-heals after SIGKILL",
          "STARTED-SESSION" in out,
          f"(next launch started a session={'STARTED-SESSION' in out})")

# --- CHECK 5: an unreachable instance must not lose the auth_code -----------
# The measured trap: with the owner's loop blocked -- peak_dbfs() really blocks
# 34.7 s on a 2 h file -- register() stalls 25 s and then RAISES. A bare
# register() would let that GLib.Error escape and drop a single-use auth_code.
BLOCKER = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, %(repo)r)
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gio
    app = Gio.Application(application_id="ai.plaud.LinuxRecorder",
                          flags=Gio.ApplicationFlags.HANDLES_OPEN)
    app.register(None)
    print("BLOCKED", flush=True)
    time.sleep(60)
''') % {"repo": repo}
blk = subprocess.Popen([sys.executable, "-c", BLOCKER], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, env=ENV)
try:
    ready = False
    t0 = time.time()
    while time.time() - t0 < 15:
        if "BLOCKED" in (blk.stdout.readline() or ""):
            ready = True
            break
    if not ready:
        check("5 an unreachable instance still consumes the auth_code", False,
              "(could not establish a blocked instance)")
    else:
        rc, out = run_main([URL], timeout=90)
        # It must fall back to handling the code HERE. Dropping it means the
        # user redoes the whole browser login; a redundant process does not.
        handled = "HANDLED-LOCALLY:" in out and _code_of(out) == CODE
        crashed = "Traceback" in out or "GLib.Error" in out
        check("5 an unreachable instance still consumes the auth_code",
              handled and not crashed and rc == 0,
              f"(handled_locally={handled}, crashed={crashed}, rc={rc})")
finally:
    blk.kill(); blk.wait()

# --- CHECK 6: no session bus at all must degrade, not break -----------------
nobus = dict(ENV)
nobus["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/plaud-check-bus"
rc, out = run_main([], env=nobus)
started = "STARTED-SESSION" in out
crashed = "Traceback" in out
check("6 no session bus degrades to standalone",
      started and not crashed and rc == 0,
      f"(started={started}, crashed={crashed}, rc={rc})")

# And the plaud:// path with no bus must still consume the code locally.
rc, out = run_main([URL], env=nobus)
check("6b no session bus still consumes the auth_code",
      "HANDLED-LOCALLY:" in out and _code_of(out) == CODE and "Traceback" not in out,
      f"(handled={'HANDLED-LOCALLY:' in out}, rc={rc})")

# --- CHECK 7: the routing path must never enter a GTK loop (PR #27 guard) ---
# PR #27 fixed a pending login leaving an invisible eternal process -- a
# Gtk.main() with no window and nothing that could ever quit it. A routing
# client that entered a loop would recreate exactly that, and it would be
# invisible again. Assert it RETURNS, and that it never called Gtk.main().
owner = spawn_owner()
try:
    if owner is None:
        check("7 the routing path never enters a GTK loop", False,
              _no_owner_detail())
    else:
        src = textwrap.dedent('''
            import sys, types
            sys.path.insert(0, %(repo)r)
            stub = types.ModuleType("requests")
            def _boom(*a, **k): raise AssertionError("NETWORK")
            stub.get = stub.post = stub.put = _boom
            stub.exceptions = types.SimpleNamespace(RequestException=Exception)
            sys.modules["requests"] = stub
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk
            entered = []
            real_main = Gtk.main
            def spy():
                entered.append(True)
                print("ENTERED-GTK-MAIN", flush=True)
                # Do NOT actually block: the bug is the hang, and a check that
                # hangs proves nothing it can report.
                return None
            Gtk.main = spy
            from plaud_linux import main as m
            m.notify = lambda t, b: None
            sys.argv = ["plaud-linux", %(url)r]
            m.main()
            print("ROUTING-RETURNED", flush=True)
        ''') % {"repo": repo, "url": URL}
        try:
            r = subprocess.run([sys.executable, "-c", src], capture_output=True,
                               text=True, timeout=60, env=ENV)
            out2, rc2 = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            out2 = ((e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            out2 = out2.decode("utf-8", "replace") if isinstance(out2, bytes) else out2
            rc2 = None
        looped = "ENTERED-GTK-MAIN" in out2
        returned = "ROUTING-RETURNED" in out2
        check("7 the routing path never enters a GTK loop",
              returned and not looped and rc2 == 0,
              f"(returned={returned}, entered_loop={looped}, rc={rc2})")
finally:
    if owner:
        owner.kill(); owner.wait()

# --- CHECK 8: --login must not claim the instance name ----------------------
# The name means "a session is running", and only a process with a main loop
# can answer on it. --login has no loop -- begin_login() hands off to the
# browser and returns -- so claiming there left the name owned by a process
# that could not reply. A launch arriving in that window got NoReply instead
# of a remote, read it as "no session", and started recording anyway: the
# single-instance guarantee silently off, for the whole time the browser is
# open. Check 1 could not see it, because its owner is a *recording* owner
# with a real loop.
#
# Asserts on the code path rather than on wall-clock timing, so it cannot go
# flaky: run --login and require that it never registered.
src8 = textwrap.dedent('''
    import sys, types
    sys.path.insert(0, %(repo)r)
    stub = types.ModuleType("requests")
    def _boom(*a, **k): raise AssertionError("NETWORK")
    stub.get = stub.post = stub.put = _boom
    stub.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = stub
    import gi
    gi.require_version("Gtk", "3.0")
    from plaud_linux import main as m
    claimed = []
    real_claim = m._claim_instance
    def spy():
        claimed.append(True)
        print("CLAIMED-THE-NAME", flush=True)
        return real_claim()
    m._claim_instance = spy
    import plaud_linux.login as L
    L.begin_login = lambda: print("BEGAN-LOGIN", flush=True)
    class _C:
        def is_logged_in(self): return False
    m.plaud_api.PlaudClient = lambda *a, **k: _C()
    sys.argv = ["plaud-linux", "--login"]
    m.main()
    print("MAIN-RETURNED", flush=True)
''') % {"repo": repo}
try:
    r8 = subprocess.run([sys.executable, "-c", src8], capture_output=True,
                        text=True, timeout=40, env=ENV)
    out8, rc8 = r8.stdout + r8.stderr, r8.returncode
except subprocess.TimeoutExpired as e:
    out8 = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
           ((e.stderr or "") if isinstance(e.stderr, str) else "")
    rc8 = None
claimed8 = "CLAIMED-THE-NAME" in out8
began8 = "BEGAN-LOGIN" in out8
check("8 --login does not claim the instance name",
      (not claimed8) and began8 and rc8 == 0,
      f"(claimed={claimed8}, began_login={began8}, rc={rc8})")

# --- CHECK 9: a remote.open() that fails must not lose the auth_code --------
# _claim_instance() guards register() and argues at length that "a lost code is
# a failed login the user must redo" -- but the guard stopped one line short of
# the call that actually transmits it. If the owner dies between register()
# returning a remote and open() landing, the bare call raised, the traceback
# escaped main(), and the single-use code was gone. Narrow window, whole login.
src9 = textwrap.dedent('''
    import sys, types
    sys.path.insert(0, %(repo)r)
    stub = types.ModuleType("requests")
    def _boom(*a, **k): raise AssertionError("NETWORK")
    stub.get = stub.post = stub.put = _boom
    stub.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = stub
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib
    from plaud_linux import main as m
    import plaud_linux.login as L
    L.handle_url = lambda u: print("HANDLED-LOCALLY:" + u, flush=True)
    m.notify = lambda t, b: None

    class _DeadRemote:
        """A remote that vanished between register() and open()."""
        def open(self, files, hint):
            raise GLib.Error("simulated bus failure mid-open")

    m._claim_instance = lambda: (None, _DeadRemote())
    sys.argv = ["plaud-linux", %(url)r]
    try:
        m.main()
        print("MAIN-RETURNED", flush=True)
    except BaseException as e:
        print("MAIN-RAISED:" + type(e).__name__, flush=True)
''') % {"repo": repo, "url": URL}
try:
    r9 = subprocess.run([sys.executable, "-c", src9], capture_output=True,
                        text=True, timeout=40, env=ENV)
    out9, rc9 = r9.stdout + r9.stderr, r9.returncode
except subprocess.TimeoutExpired as e:
    out9 = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
           ((e.stderr or "") if isinstance(e.stderr, str) else "")
    rc9 = None
preserved9 = "HANDLED-LOCALLY:" in out9
raised9 = "MAIN-RAISED:" in out9
check("9 a failing remote.open() falls back instead of losing the code",
      preserved9 and not raised9 and rc9 == 0,
      f"(handled_locally={preserved9}, main_raised={raised9}, rc={rc9})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
