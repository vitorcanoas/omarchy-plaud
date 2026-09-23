#!/usr/bin/env python3
"""The screenshot/mark path: what it sends, and what it must survive.

attach_screenshots() had zero coverage. It was verified by hand against the
live API (CAN-337) and works -- which is exactly the problem: nothing proves
it keeps working, and every trap in it is the kind a tidy-up would spring.

What these checks pin, and why each is a trap:

  * `data` from get_s3_upload_link is a LIST (PLAUD_API_NOTES step 1). Reading
    it as an object is the defect that broke CAN-308.
  * The S3 PUT must carry NO Content-Type (step 2). The URL is signed without
    it; adding one yields 403 SignatureDoesNotMatch. `_headers()` sets
    Content-Type on every business call, so "be consistent" is a live temptation.
  * `picture_link` is the field name even though it carries a storage location,
    and for an InputMark it is the empty string -- not absent, not null
    (step 5 / InputMark section). The name is a misnomer in the official
    client too; it is still the wire contract.
  * A block that never gets a mark_id is DROPPED from the payload, not sent
    half-formed. Sending a block nothing identifies is worse than sending none.
  * The whole path is best-effort and swallows per image (CLAUDE.md,
    Architecture). A screenshot that explodes must not take the upload or the
    recording down with it. The broad `except` there is load-bearing.

What these checks deliberately do NOT assert: that update_source_info returned
`status: 0`. The spec (step 5) says it returns 0 even for a nonexistent
file_id, so asserting on it would prove nothing. Everything here asserts on
what left this machine -- the recorded request payloads -- which is the only
thing the client actually controls.

Every scenario runs in a CHILD PROCESS. plaud_api caches module state and the
stubbed requests handlers bind to one module instance, so reloading in-process
silently leaks stubs between scenarios and makes these checks pass whether the
defect is present or not -- see the same warning in test_wshost.py.

Run standalone:  python3 tests/test_markpath.py
"""
import json
import os
import subprocess
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}")
        if detail:
            print(f"  {detail}")


# ---------------------------------------------------------------- the probe
#
# A fake backend that records every call. The scenario name selects how it
# misbehaves, so one probe covers the happy path and each failure mode.
PROBE = r'''
import json, os, sys, tempfile
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, {root!r})

SCENARIO = {scenario!r}

from plaud_linux import plaud_api as api

# No real sleeping: the poll loop sleeps 2s per attempt and one scenario
# exhausts all ten of them.
api.time.sleep = lambda *_: None

calls = []          # every business request: (path, payload)
puts = []           # every S3 PUT: (url, headers, nbytes)


class Resp:
    def __init__(self, payload, status=200, text=""):
        self._p = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._p


def s3_link_payload():
    if SCENARIO == "link_object_shaped":
        # What a backend that returned an OBJECT would send. The real one
        # never does; this exists only to show the list-handling is real.
        return {{"status": 0, "data": {{
            "upload_link": "https://s3.example/put/obj",
            "storage_location": "loc/obj.png"}}}}
    return {{"status": 0, "data": [{{
        "upload_link": "https://s3.example/put/one",
        "storage_location": "audio/mark/deadbeef.png",
        "file_suffix": "png", "ppc_status": 1}}]}}


def request(method, url, **kw):
    path = url.split("plaud.ai", 1)[-1]
    calls.append((path, kw.get("json")))
    if "get_s3_upload_link" in path:
        return Resp(s3_link_payload())
    if "mark/create_task" in path:
        if SCENARIO == "create_task_no_mark_id":
            return Resp({{"status": 0, "data": {{"ppc_status": 1}}}})
        return Resp({{"status": 0, "data": {{"mark_id": "MARK1", "ppc_status": 1}}}})
    if "mark/get_mark_result" in path:
        if SCENARIO == "never_completes":
            # Exactly what the live API sends while pending: mark_result null,
            # status 1. Never resolves -- the poll must exhaust.
            return Resp({{"status": 0, "data": {{"mark_results": [
                {{"mark_id": "MARK1", "mark_result": None, "status": 1}}]}}}})
        return Resp({{"status": 0, "data": {{"mark_results": [
            {{"mark_id": "MARK1", "status": 1,
              "mark_result": {{"result": "Uma janela de terminal."}}}}]}}}})
    if "update_source_info" in path:
        # status 0 even for a nonexistent file_id -- per the spec. Returning
        # it here is the point: nothing downstream may treat it as proof.
        return Resp({{"status": 0, "msg": "success",
                     "data": {{"source_id": "S1", "file_version": 7}}}})
    return Resp({{"status": 0, "data": {{}}}})


def put(url, **kw):
    data = kw.get("data") or b""
    puts.append((url, dict(kw.get("headers") or {{}}), len(data)))
    if SCENARIO == "s3_rejects":
        return Resp(None, status=403, text="SignatureDoesNotMatch")
    if SCENARIO == "s3_raises":
        raise api.requests.exceptions.ConnectionError("connection reset")
    return Resp(None, status=200)


api.requests.request = request
api.requests.put = put

c = api.PlaudClient()
# A logged-in client, without touching a real token store: _wt() short-circuits
# on a WT that has not expired.
c.tokens = {{"wt": "WT", "wt_expire": 9999999999, "ws_id": "ws9",
            "wrt": "WRT", "wrt_expire": 9999999999}}
api._save_tokens = lambda *_a, **_k: None

d = tempfile.mkdtemp()
shot = os.path.join(d, "shot.png")
with open(shot, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 200)

shots = [{{"path": shot, "t": 12}}]
if SCENARIO == "missing_file":
    shots = [{{"path": os.path.join(d, "gone.png"), "t": 3}}]
if SCENARIO == "two_shots_one_broken":
    shots = [{{"path": os.path.join(d, "gone.png"), "t": 3}},
             {{"path": shot, "t": 12}}]

# t=5 is EARLIER than the screenshot at t=12, and notes are appended after
# every screenshot -- so insertion order and timestamp order disagree here.
# With a note at t=30 the two coincide and the sort is untestable.
notes = [{{"text": "lembrar disso", "t": 5}}] if {with_notes!r} else None

raised = None
n = None
try:
    n = c.attach_screenshots("FILE1", shots, notes=notes)
except BaseException as e:
    raised = "{{}}: {{}}".format(type(e).__name__, e)

usi = [p for path, p in calls if "update_source_info" in path]
print("@@RESULT@@" + json.dumps({{
    "returned": n,
    "raised": raised,
    "paths": [p for p, _ in calls],
    "put_headers": [h for _, h, _ in puts],
    "put_urls": [u for u, _, _ in puts],
    "put_sizes": [s for _, _, s in puts],
    "usi_count": len(usi),
    "usi": usi[0] if usi else None,
    "marks": json.loads(usi[0]["source_content"]) if usi else None,
}}))
'''


def run_scenario(scenario, with_notes=False):
    """One full attach_screenshots() run, in a fresh interpreter."""
    src = PROBE.format(root=ROOT, scenario=scenario, with_notes=with_notes)
    r = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True, timeout=120)
    for line in r.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    raise AssertionError(
        f"probe {scenario!r} produced no result\n"
        f"--- stdout ---\n{r.stdout[-2000:]}\n--- stderr ---\n{r.stderr[-2000:]}")


def run():
    # ---------------------------------------------------------- happy path
    ok = run_scenario("happy", with_notes=True)

    # 1. The list-shaped data. If it were read as an object, upload_link would
    # be None, the image would be skipped, and no PUT would ever happen.
    check("1 the list-shaped s3 link is indexed, not read as an object",
          len(ok["put_urls"]) == 1
          and ok["put_urls"][0] == "https://s3.example/put/one",
          f"put_urls={ok['put_urls']!r} -- data[0] was not unwrapped")

    # 2. The signature is computed without Content-Type, so sending one is a
    # 403. requests fills in no Content-Type of its own for a bytes body, so
    # the correct header set here is empty.
    hdrs = ok["put_headers"][0] if ok["put_headers"] else {}
    ct = [k for k in hdrs if k.lower() == "content-type"]
    check("2 the s3 PUT carries no Content-Type header",
          not ct,
          f"headers={hdrs!r} -- S3 answers 403 SignatureDoesNotMatch")

    # 3. The order of the five calls IS the contract (spec steps 1-5), and
    # update_source_info is sent ONCE, batched -- not once per block.
    p = ok["paths"]
    check("3 the five steps run in spec order and update_source_info is batched once",
          p[0].endswith("/file/get_s3_upload_link")
          and p[1].endswith("/ai/mark/create_task")
          and "/ai/mark/get_mark_result" in p[2]
          and p[-1].endswith("/ai/update_source_info")
          and ok["usi_count"] == 1,
          f"paths={p!r} usi_count={ok['usi_count']}")

    marks = ok["marks"] or []
    pic = [m for m in marks if m["mark_type"] == 2]
    inp = [m for m in marks if m["mark_type"] == 3]

    # 4. picture_link carries the STORAGE LOCATION for a picture mark. The name
    # is a misnomer in the official client too -- renaming it breaks the wire.
    check("4 picture_link carries the storage location, misnomer and all",
          len(pic) == 1 and pic[0].get("picture_link") == "audio/mark/deadbeef.png",
          f"picture marks={pic!r}")

    # 5. mark_content is the AI result text, and timestamp is MILLISECONDS
    # (block.timestamp * 1000, highlightService.ts:100-148). Seconds would put
    # every mark in the first instant of the recording.
    check("5 mark_content is the AI result and timestamp is milliseconds",
          len(pic) == 1
          and pic[0].get("mark_content") == "Uma janela de terminal."
          and pic[0].get("timestamp") == 12000,
          f"picture mark={(pic[0] if pic else None)!r}")

    # 6. An InputMark skips S3 entirely and carries the EMPTY STRING in both
    # mark_id and picture_link -- not absent, not null (types.ts:87-90).
    check("6 a note is an InputMark with empty-string mark_id and picture_link",
          len(inp) == 1
          and inp[0].get("mark_id") == ""
          and inp[0].get("picture_link") == ""
          and inp[0].get("mark_content") == "lembrar disso"
          and inp[0].get("timestamp") == 5000,
          f"input marks={inp!r}")

    # 7. The client sorts by timestamp before sending. The note (t=5) is
    # appended AFTER the screenshot (t=12), so unsorted output would be
    # [12000, 5000] -- the official client relies on insertion order, and we
    # collect the two kinds in separate passes, so the sort is what restores
    # it. One S3 upload total: the note never entered the picture pipeline.
    check("7 marks are ordered by timestamp and the note skipped the S3 flow",
          [m["timestamp"] for m in marks] == [5000, 12000]
          and len(ok["put_urls"]) == 1,
          f"timestamps={[m['timestamp'] for m in marks]!r} puts={len(ok['put_urls'])}")

    # ------------------------------------------- blocks that never complete
    #
    # Two distinct outcomes, and they are not the same thing:
    #   - no mark_id -> nothing identifies the block, so it CANNOT be sent
    #   - poll exhausted -> the image is on S3 and has an id, so it IS sent,
    #     with empty mark_content, rather than losing the screenshot
    nomid = run_scenario("create_task_no_mark_id")
    check("8 a block with no mark_id is dropped from the payload, not sent empty",
          nomid["returned"] == 0 and nomid["usi_count"] == 0,
          f"returned={nomid['returned']} usi={nomid['usi']!r}")

    stuck = run_scenario("never_completes")
    smarks = stuck["marks"] or []
    check("9 an unfinished poll attaches the mark with empty content rather than losing it",
          stuck["returned"] == 1 and len(smarks) == 1
          and smarks[0]["mark_content"] == ""
          and smarks[0]["mark_id"] == "MARK1"
          and stuck["paths"].count("/ai/mark/get_mark_result") == 10,
          f"returned={stuck['returned']} marks={smarks!r} "
          f"polls={stuck['paths'].count('/ai/mark/get_mark_result')}")

    # ------------------------------------------------ best-effort guarantee
    #
    # CLAUDE.md, Architecture: this path swallows per image so it can never
    # lose a recording. Each of these must return normally -- an exception
    # escaping here reaches main.py's worker, which would flip the sidecar to
    # ok=False on a recording that is already safely on the server.
    for scen, label in (("s3_rejects", "10 an S3 403"),
                        ("s3_raises", "11 a network error during the S3 PUT"),
                        ("missing_file", "12 a screenshot file that vanished")):
        r = run_scenario(scen)
        check(f"{label} is swallowed, not raised",
              r["raised"] is None and r["returned"] == 0 and r["usi_count"] == 0,
              f"raised={r['raised']!r} returned={r['returned']}")

    # 13. Per IMAGE, not per call: one broken screenshot must not cost the
    # good one. This is the difference between the try/except sitting inside
    # the loop and sitting around it.
    mixed = run_scenario("two_shots_one_broken")
    mmarks = mixed["marks"] or []
    check("13 one broken screenshot does not drop the good one",
          mixed["raised"] is None and mixed["returned"] == 1
          and len(mmarks) == 1 and mmarks[0]["timestamp"] == 12000,
          f"raised={mixed['raised']!r} returned={mixed['returned']} marks={mmarks!r}")

    # 14. The envelope of the batched call. source_type and source_title are
    # both required (spec step 5), and source_content is a JSON STRING, not a
    # nested object -- the backend parses it itself.
    u = ok["usi"] or {}
    check("14 update_source_info carries file_id, mark_memo type, title and a JSON string",
          u.get("file_id") == "FILE1"
          and u.get("source_type") == "mark_memo"
          and bool(u.get("source_title"))
          and isinstance(u.get("source_content"), str),
          f"payload={ {k: v for k, v in u.items() if k != 'source_content'} !r}")

    # 15. No marks at all means no call. update_source_info answers status 0
    # for anything, so an empty batch would look like success while writing an
    # empty mark_memo over the recording.
    empty = run_scenario("missing_file")
    check("15 no surviving marks means update_source_info is never called",
          empty["usi_count"] == 0,
          f"usi_count={empty['usi_count']}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
