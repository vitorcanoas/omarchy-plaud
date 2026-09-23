#!/usr/bin/env python3
"""
Test that pick_system_monitor() filters fifine sinks correctly in all fallback paths.

This test monkeypatches the sink-discovery functions to simulate a scenario where:
  - No audio is currently playing (playing_sink() -> None)
  - A fifine sink is RUNNING (running_sinks() includes it)
  - Default sink may or may not be fifine
  - list_sinks() provides fallback options

CORRECTED 2026-09-06. This file used to assert that a fifine-named sink must
never be chosen outside a last resort. That rule cost the user a real
recording: the K690 has a headphone jack, so it is a genuine sound card, and
when the user selects it as the system default it is where the audio goes.
With nothing playing, the name filter rejected it, the choice fell through to
a SUSPENDED HDMI sink, and the session wrote a zero-byte segment. The
screenshot and flag in that same session both worked; only the audio was lost.

What it verifies now:
  1. The DEFAULT sink is chosen whatever it is named -- it is the machine's
     real output, which is the whole definition of "system audio".
  2. A sink that is actually playing still wins over everything (tocando-agora).
  3. The name filter survives only where it still earns its place: choosing
     blindly among sinks nobody selected and nothing is playing on.
"""

import sys
import os

# Support standalone execution with `python3 tests/test_fifine_fallback.py [repo]`
# where repo defaults to the current project or can point at another checkout (for master).
if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    sys.path.insert(0, repo)

from plaud_linux import audio
from contextlib import contextmanager


@contextmanager
def monkeypatch_sinks(playing=None, running=None, default=None, all_sinks=None):
    """
    Temporarily replace the sink-discovery functions.

    Args:
        playing: value to return from playing_sink()
        running: list to return from running_sinks()
        default: value to return from default_sink()
        all_sinks: list to return from list_sinks()
    """
    orig_playing = audio.playing_sink
    orig_running = audio.running_sinks
    orig_default = audio.default_sink
    orig_list = audio.list_sinks

    audio.playing_sink = lambda: playing
    audio.running_sinks = lambda: running if running is not None else []
    audio.default_sink = lambda: default
    audio.list_sinks = lambda: all_sinks if all_sinks is not None else []

    try:
        yield
    finally:
        audio.playing_sink = orig_playing
        audio.running_sinks = orig_running
        audio.default_sink = orig_default
        audio.list_sinks = orig_list


def check(condition, desc):
    """Print PASS or FAIL for a condition (matching test output format)."""
    status = "PASS" if condition else "FAIL"
    # Print with " " (space) at line start to match test harness pattern recognition
    print(f"{status}  {desc}")
    if not condition:
        return False
    return True


all_pass = True

# Test 1: Bug reproduction (main scenario)
# No audio playing, fifine sink is RUNNING, should NOT pick fifine in steps 2/2b.
print("\n--- Test 1: Fifine sink is RUNNING (no audio playing) ---")
with monkeypatch_sinks(
    playing=None,
    running=["alsa_output.usb-fifine-audio.stereo-fallback"],
    default="alsa_output.usb-fifine-audio.stereo-fallback",
    all_sinks=["alsa_output.usb-fifine-audio.stereo-fallback", "alsa_output.pci-0000_00_1f.3.analog-stereo"]
):
    mon, sink, why = audio.pick_system_monitor()

    # CORRECTED 2026-09-06 -- same correction as Test 4, same setup, and the
    # same reason. The fifine here is the DEFAULT sink, so it is where the
    # machine's audio actually goes, and rejecting it by name sent a real
    # session to a suspended HDMI sink that recorded zero bytes.
    is_fifine = "fifine" in sink.lower() if sink else False
    all_pass &= check(
        is_fifine and why == "sink-default",
        f"The default sink wins even when named fifine: sink='{sink}' reason='{why}'"
    )

# Test 2: Step 1 regression guard (tocando-agora still returns fifine if playing)
print("\n--- Test 2: Fifine is actually playing (tocando-agora path) ---")
with monkeypatch_sinks(
    playing="alsa_output.usb-fifine-audio.stereo-fallback",  # Playing NOW
    running=["alsa_output.usb-fifine-audio.stereo-fallback"],
    default="alsa_output.usb-fifine-audio.stereo-fallback",
    all_sinks=["alsa_output.usb-fifine-audio.stereo-fallback"]
):
    mon, sink, why = audio.pick_system_monitor()
    is_fifine = "fifine" in sink.lower() if sink else False

    # Step 1 should deliberately return fifine even though it's a fifine device
    # because the docstring says: "um sink com sink-input ativo está tocando de verdade"
    all_pass &= check(
        is_fifine and why == "tocando-agora",
        f"Step 1 (tocando-agora) returns fifine: sink='{sink}' reason='{why}'"
    )

# Test 3: Step 5 last resort (all sinks are fifine, no alternatives)
print("\n--- Test 3: All sinks are fifine (last resort fallback) ---")
with monkeypatch_sinks(
    playing=None,
    running=[],  # Nothing running
    default="alsa_output.usb-fifine-audio.stereo-fallback",
    all_sinks=["alsa_output.usb-fifine-audio.stereo-fallback"]
):
    mon, sink, why = audio.pick_system_monitor()
    is_fifine = "fifine" in sink.lower() if sink else False

    # The only sink there is, and it IS the default. Reached through the
    # default step now rather than a last resort -- the outcome that matters
    # (record the machine's actual output) is the same, by a shorter route.
    all_pass &= check(
        is_fifine and why == "sink-default",
        f"The only sink, which is also default, is chosen: sink='{sink}' reason='{why}'"
    )

# Test 4: Running fifine but non-fifine alternative exists
print("\n--- Test 4: Fifine running, but non-fifine exists in list ---")
with monkeypatch_sinks(
    playing=None,
    running=["alsa_output.usb-fifine-audio.stereo-fallback"],
    default="alsa_output.usb-fifine-audio.stereo-fallback",
    all_sinks=[
        "alsa_output.usb-fifine-audio.stereo-fallback",
        "alsa_output.pci-0000_00_1f.3.analog-stereo"
    ]
):
    mon, sink, why = audio.pick_system_monitor()
    is_fifine = "fifine" in sink.lower() if sink else False

    # CORRECTED 2026-09-06, and this check is the reason the fix exists.
    #
    # This used to demand `not is_fifine` -- reject the sink because of its
    # NAME, even when the user has it selected as the system default. That is
    # not a hypothetical: the K690 has a headphone jack, so it is a real sound
    # card, and when it is the default it is where the audio actually goes.
    #
    # Measured with the user watching: default = fifine, nothing playing, the
    # name filter rejected it, the choice fell to a SUSPENDED HDMI sink, and
    # the session recorded seg_000.opus at ZERO bytes. The screenshot and the
    # flag in that same session both worked; only the audio was lost.
    #
    # The default sink is the answer, whatever it is called. The name filter
    # survives only where it still earns its place: choosing blindly among
    # sinks nobody selected and nothing is playing on.
    all_pass &= check(
        is_fifine and why == "sink-default",
        f"The default sink wins even when named fifine: sink='{sink}' reason='{why}'"
    )

# Test 5: Default sink is not fifine (step 3)
print("\n--- Test 5: Default sink is not fifine (step 3 path) ---")
with monkeypatch_sinks(
    playing=None,
    running=["alsa_output.usb-fifine-audio.stereo-fallback"],
    default="alsa_output.pci-0000_00_1f.3.analog-stereo",
    all_sinks=[
        "alsa_output.usb-fifine-audio.stereo-fallback",
        "alsa_output.pci-0000_00_1f.3.analog-stereo"
    ]
):
    mon, sink, why = audio.pick_system_monitor()

    # Default is not fifine and is not in running, so steps 2/2b skip; step 3 returns it
    all_pass &= check(
        sink == "alsa_output.pci-0000_00_1f.3.analog-stereo" and why == "sink-default",
        f"Step 3 returns non-fifine default: sink='{sink}' reason='{why}'"
    )

# Test 6: Running has non-fifine, default does not exist (step 2b)
print("\n--- Test 6: Non-fifine in running, default missing (step 2b path) ---")
with monkeypatch_sinks(
    playing=None,
    running=["alsa_output.pci-0000_00_1f.3.analog-stereo"],
    default=None,
    all_sinks=["alsa_output.pci-0000_00_1f.3.analog-stereo"]
):
    mon, sink, why = audio.pick_system_monitor()

    all_pass &= check(
        sink == "alsa_output.pci-0000_00_1f.3.analog-stereo" and why == "sink-running",
        f"Step 2b returns non-fifine running: sink='{sink}' reason='{why}'"
    )

print("\n" + ("=== ALL CHECKS PASSED ===" if all_pass else "=== SOME CHECKS FAILED ==="))
sys.exit(0 if all_pass else 1)
