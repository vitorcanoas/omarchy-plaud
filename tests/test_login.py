"""Check for CAN-319: extract_auth_code() corrupting a literal '+' in auth_code.

Usage: python3 tests/test_login.py [repo]
Defaults to this checkout; pass a path to run against another tree (a
`git worktree` of an older revision, to confirm this still goes red).
Must FAIL on the pre-fix tree, PASS after.

extract_auth_code() is a pure function (no I/O, no network, no GTK), so this
drives it directly with realistic plaud:// callback URLs rather than a live
redirect.

Mechanism: urllib.parse.parse_qs() decodes query values with unquote_plus(),
which follows application/x-www-form-urlencoded semantics -- a literal '+'
means a space. That is correct for an HTML form body, but a plaud:// callback
is a URI, not a form submission (RFC 3986), so a literal '+' the backend put
in the auth_code must round-trip as '+', not become ' '. Since the auth_code
is single-use, a corrupted parse burns it and the user cannot just retry.
"""
import sys, pathlib

repo = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, repo)

from plaud_linux import login

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


# 1. Literal '+' in the auth_code (the reported defect) must survive intact.
url = "plaud://login?auth_code=abc+def123"
got = login.extract_auth_code(url)
check("1 a literal '+' in auth_code round-trips, not a space",
      got == "abc+def123", f"(got={got!r})")

# 2. Multiple literal '+' characters must all survive.
url = "plaud://login?auth_code=a+b+c"
got = login.extract_auth_code(url)
check("2 multiple literal '+' characters all round-trip",
      got == "a+b+c", f"(got={got!r})")

# 3. A percent-encoded '+' (%2B) must still decode to a literal '+' -- this
# already worked before the fix and must keep working.
url = "plaud://login?auth_code=abc%2Bdef123"
got = login.extract_auth_code(url)
check("3 %2B still decodes to a literal '+'",
      got == "abc+def123", f"(got={got!r})")

# 4. A percent-encoded space (%20) must still decode to an actual space --
# i.e. the fix must not stop decoding real encoded spaces.
url = "plaud://login?auth_code=abc%20def"
got = login.extract_auth_code(url)
check("4 %20 still decodes to a real space",
      got == "abc def", f"(got={got!r})")

# 5. Other percent-escapes (e.g. '=', '&', '/') must still decode normally --
# the fix must not regress ordinary percent-decoding.
url = "plaud://login?auth_code=abc%3D%26%2Fxyz"
got = login.extract_auth_code(url)
check("5 other percent-escapes still decode normally",
      got == "abc=&/xyz", f"(got={got!r})")

# 6. No auth_code param at all still yields None (unchanged behaviour).
url = "plaud://login?foo=bar"
got = login.extract_auth_code(url)
check("6 a missing auth_code still yields None",
      got is None, f"(got={got!r})")

# 7. auth_code with no '=' at all (malformed/valueless param) still yields
# None, matching parse_qs's behaviour of omitting a valueless param -- not ''.
url = "plaud://login?auth_code"
got = login.extract_auth_code(url)
check("7 a valueless auth_code (no '=') still yields None, not ''",
      got is None, f"(got={got!r})")

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
