"""The gate against speech-like echo (tests/gate_bench.js): the agent must not stop herself, and the user's voice must stop her.

The earlier simulations used steady tones, under which the gate looked perfect; on the loudness curves of speech (syllables, her
quiet ones and loud ones, bursts of the echo canceller's residue) the committed gate took her own echo for the user 25-93% of the
time and she stopped herself 16-38 times a minute («самі себе чують і перебиваються»). This is the regression test for that.

Skipped where `jsc` is missing."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")


@pytest.fixture(scope="module")
def bench():
    out = subprocess.run([JSC, "tests/gate_bench.js", "--", "web/gate.js"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_she_does_not_stop_herself_on_her_own_echo(bench):
    # the worst of three kinds of echo (the last two with strong bursts and glitches), a minute each, at four loudnesses of
    # echo: a desktop's, a loud laptop's, a phone's, a phone's at its worst. The committed gate: 16-38 a minute.
    for echo, stops in bench["selfStopsPerMin"].items():
        assert stops <= 10, f"echo {echo}: she stopped herself {stops} times in a minute: {bench['selfStopsPerMin']}"


def test_a_voice_over_her_stops_her_fast_where_the_echo_allows(bench):
    users = bench["userStops"]
    desktop = users["echo 0.2 voice 0.3"]
    assert desktop["stoppedPct"] >= 90 and desktop["medianMs"] <= 1000, desktop  # a normal voice over desktop speakers
    assert users["echo 0.2 voice 0.15"]["stoppedPct"] >= 75, users  # a quiet one, too
    assert users["echo 0.5 voice 0.3"]["stoppedPct"] >= 75 and users["echo 0.5 voice 0.3"]["medianMs"] <= 1500, users  # a loud laptop
    # a phone's speaker beside its microphone: the echo is as loud as a normal voice, so only a louder voice can tell itself from it
    assert users["echo 1 voice 0.6"]["stoppedPct"] >= 75, users
