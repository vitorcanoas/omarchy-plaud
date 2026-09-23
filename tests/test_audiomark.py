#!/usr/bin/env python3
"""The AudioMark (flag) path: mark_type 1, and the four literals that 400 it.

CAN-309 phase 2. The ring buffer landed in phase 1 and the flag button has
existed since then, but until now it called a stub -- the AudioMark contract
had never been sent on the wire. These checks pin the contract before the
first live call, because the failure mode is a 400 with no local symptom.

The AudioMark path is the PictureMark path with two literals changed, which
is precisely why it needs its own coverage: every one of those literals is
the kind a reader would "make consistent" with the path beside it.

  * `file_suffixs: ["ogg"]`, not "png" and not "opus". The codec is Opus and
    the container is Ogg; the official client sends "ogg"
    (highlightService-CUAOVE3u.js:162). "opus" is the plausible wrong answer.
  * TWO discriminators in one create_task payload, in DIFFERENT vocabularies:
    `mark_type: 1` (numeric enum) and `type: "audio"` (string). They are not
    redundant. In the official client both derive from one `block.type` so
    they cannot disagree; here they are written separately and can, which
    makes disagreement the single most likely way this 400s.
  * `picture_link` carries the OGG's storage location. The name is a misnomer
    in the official client too. Renaming it to audio_link is a 400.
  * `timestamp` is MILLISECONDS. Seconds is off by 1000x and the mark lands
    at the wrong point in a note that may be an hour long.

And one absence, asserted deliberately: there is NO duration, start_time or
end_time field. The clip is a separately uploaded file, not an offset into
the recording -- when the official client runs this pipeline the recording
has not been uploaded and has no file_id, so an offset would have nothing to
point into. A reader who assumes otherwise would add a field the backend
does not expect.

What these checks do NOT assert: that update_source_info returned status 0.
It returns 0 for a nonexistent file_id, so it proves nothing. Everything here
asserts on what left this machine.

Every scenario runs in a CHILD PROCESS, for the reason test_markpath.py
gives: plaud_api caches module state and stubbed handlers bind to one module
instance, so reloading in-process leaks stubs between scenarios and makes
checks pass whether the defect is there or not.

Run standalone:  python3 tests/test_audiomark.py
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
PROBE = r'''
import json, os, sys, tempfile
os.environ["PLAUD_LINUX_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, {root!r})

SCENARIO = {scenario!r}

from plaud_linux import plaud_api as api

api.time.sleep = lambda *_: None

calls = []
puts = []


class Resp:
    def __init__(self, payload, status=200, text=""):
        self._p = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._p


def request(method, url, **kw):
    path = url.split("plaud.ai", 1)[-1]
    calls.append((path, kw.get("json")))
    if "get_s3_upload_link" in path:
        return Resp({{"status": 0, "data": [{{
            "upload_link": "https://s3.example/put/snippet",
            "storage_location": "audio/mark/cafebabe.ogg",
            "file_suffix": "ogg", "ppc_status": 1}}]}})
    if "mark/create_task" in path:
        return Resp({{"status": 0, "data": {{"mark_id": "AMARK1", "ppc_status": 1}}}})
    if "mark/get_mark_result" in path:
        return Resp({{"status": 0, "data": {{"mark_results": [
            {{"mark_id": "AMARK1", "status": 1,
              "mark_result": {{"result": "O professor explica o teorema."}}}}]}}}})
    if "update_source_info" in path:
        return Resp({{"status": 0, "msg": "success", "data": {{"source_id": "S1"}}}})
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
c.tokens = {{"wt": "WT", "wt_expire": 9999999999, "ws_id": "ws9",
            "wrt": "WRT", "wrt_expire": 9999999999}}
api._save_tokens = lambda *_a, **_k: None

d = tempfile.mkdtemp()
snip = os.path.join(d, "flag_000042_120000.ogg")
with open(snip, "wb") as f:
    f.write(b"OggS" + b"z" * 500)

flags = [{{"path": snip, "t": 42}}]
if SCENARIO == "missing_file":
    flags = [{{"path": os.path.join(d, "gone.ogg"), "t": 7}}]

# A screenshot at t=99 alongside a flag at t=42. Later in insertion order,
# earlier nowhere -- so if the batch is not sorted, the flag comes out second.
shots = []
if SCENARIO == "mixed_types":
    shot = os.path.join(d, "shot.png")
    with open(shot, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 200)
    shots = [{{"path": shot, "t": 99}}]

raised = None
n = None
try:
    n = c.attach_screenshots("FILE1", shots, flags=flags)
except BaseException as e:
    raised = "{{}}: {{}}".format(type(e).__name__, e)

usi = [p for path, p in calls if "update_source_info" in path]
link = [p for path, p in calls if "get_s3_upload_link" in path]
task = [p for path, p in calls if "mark/create_task" in path]
print("@@RESULT@@" + json.dumps({{
    "returned": n,
    "raised": raised,
    "paths": [p for p, _ in calls],
    "link_payloads": link,
    "task_payloads": task,
    "put_headers": [h for _, h, _ in puts],
    "put_sizes": [s for _, _, s in puts],
    "usi_count": len(usi),
    "marks": json.loads(usi[0]["source_content"]) if usi else None,
}}))
'''


def run_scenario(scenario):
    """One full attach_screenshots() run with flags, in a fresh interpreter."""
    src = PROBE.format(root=ROOT, scenario=scenario)
    r = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True, timeout=120)
    for line in r.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    raise AssertionError(
        f"probe {scenario!r} produced no result\n"
        f"--- stdout ---\n{r.stdout[-2000:]}\n--- stderr ---\n{r.stderr[-2000:]}")


def run():
    ok = run_scenario("happy")
    marks = ok["marks"] or []
    flag = next((m for m in marks if m.get("mark_type") == 1), None)

    # 1. The pipeline runs at all, in the documented order. A flag that skips
    # straight to update_source_info (the way an InputMark legitimately does)
    # would send a block with no mark_id and no uploaded audio.
    order = [p for p in ok["paths"]
             if any(k in p for k in ("get_s3_upload_link", "create_task",
                                     "get_mark_result", "update_source_info"))]
    check("1 the flag runs the full five-step pipeline in order",
          ok["raised"] is None
          and "get_s3_upload_link" in order[0]
          and "create_task" in order[1]
          and "get_mark_result" in order[2]
          and "update_source_info" in order[-1],
          f"order={order!r} raised={ok['raised']!r}")

    # 2. THE suffix. "opus" is the plausible wrong answer -- the codec IS
    # Opus -- and the official client sends "ogg" anyway.
    lp = (ok["link_payloads"] or [{}])[0] or {}
    check("2 get_s3_upload_link asks for 'ogg', not 'png' and not 'opus'",
          lp.get("file_suffixs") == ["ogg"] and lp.get("op_type") == "mark",
          f"payload={lp!r}")

    # 3. The two discriminators, which is the check this file exists for.
    # Numeric 1 AND string "audio", together, in one payload.
    tp = (ok["task_payloads"] or [{}])[0] or {}
    tc = (tp.get("mark_content") or [{}])[0] or {}
    check("3 create_task sends mark_type 1 AND type 'audio' together",
          tp.get("mark_type") == 1 and tc.get("type") == "audio",
          f"payload={tp!r}")

    # 4. The uploaded location is what the block carries, not a re-derived
    # path. If these disagree the backend has a block pointing at nothing.
    check("4 create_task content is the storage_location from step 1",
          tc.get("content") == "audio/mark/cafebabe.ogg",
          f"content={tc.get('content')!r}")

    # 5. No Content-Type on the S3 PUT. The URL is signed without it; adding
    # one yields 403. _headers() sets it on every business call, so "be
    # consistent with the rest of the file" is a live temptation here.
    hdrs = (ok["put_headers"] or [{}])[0] or {}
    check("5 the S3 PUT carries no Content-Type",
          not any(k.lower() == "content-type" for k in hdrs),
          f"headers={hdrs!r}")

    # 6. The ogg bytes actually went up, rather than an empty body.
    check("6 the snippet bytes are what was PUT",
          ok["put_sizes"] == [504],
          f"sizes={ok['put_sizes']!r}")

    # 7. mark_type survives into the batched block. The create_task payload
    # being right does not prove the block is.
    check("7 the block carries mark_type 1",
          flag is not None and flag.get("mark_type") == 1,
          f"flag={flag!r}")

    # 8. picture_link carries the OGG. The misnomer is the wire contract.
    check("8 picture_link carries the ogg's storage location",
          flag is not None and flag.get("picture_link") == "audio/mark/cafebabe.ogg",
          f"flag={flag!r}")

    # 9. MILLISECONDS. Seconds is off by 1000x, and on a long recording that
    # puts the mark somewhere else entirely.
    check("9 timestamp is milliseconds, not seconds",
          flag is not None and flag.get("timestamp") == 42000,
          f"timestamp={(flag or {}).get('timestamp')!r}")

    # 10. mark_content is the AI's transcript of the clip -- the whole point
    # of the flag. An empty string here means the poll result was dropped.
    check("10 mark_content carries the AI result from get_mark_result",
          flag is not None and flag.get("mark_content") == "O professor explica o teorema.",
          f"content={(flag or {}).get('mark_content')!r}")

    # 11. The absence. There is no duration/start/end field, and adding one
    # would be inventing contract. See the module docstring for why the clip
    # cannot be an offset.
    check("11 no duration or time-range field is sent",
          flag is not None
          and not any(k in flag for k in
                      ("duration", "start_time", "end_time", "audio_length")),
          f"keys={sorted((flag or {}).keys())!r}")

    # 12. Ordering across types. The flag at t=42 must precede the screenshot
    # at t=99 even though the screenshot is collected first -- the official
    # client relies on insertion order, so ours restores it by sorting.
    mixed = run_scenario("mixed_types")
    mm = mixed["marks"] or []
    check("12 a flag sorts before a later screenshot regardless of collection order",
          len(mm) == 2 and mm[0]["mark_type"] == 1 and mm[0]["timestamp"] == 42000
          and mm[1]["mark_type"] == 2 and mm[1]["timestamp"] == 99000,
          f"marks={[(m['mark_type'], m['timestamp']) for m in mm]!r}")

    # 13-15. Best-effort, per CLAUDE.md Architecture: this path must never be
    # what turns a successful upload into a reported failure. An exception
    # escaping reaches main.py's worker and flips the sidecar to ok=False on a
    # recording that is already safely on the server.
    for scen, label in (("s3_rejects", "13 an S3 403"),
                        ("s3_raises", "14 a network error during the S3 PUT"),
                        ("missing_file", "15 a snippet file that vanished")):
        r = run_scenario(scen)
        check(f"{label} is swallowed, not raised",
              r["raised"] is None and r["returned"] == 0 and r["usi_count"] == 0,
              f"raised={r['raised']!r} returned={r['returned']}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
