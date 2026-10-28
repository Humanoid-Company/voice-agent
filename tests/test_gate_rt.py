"""Output 1 of web/gate.js, the microphone for Realtime: it must not cut the user's first words (output 0 does, by
design, for the recognizer), and it must never carry her own echo."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sim():
    out = subprocess.run([JSC, "tests/gate_rt_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


pytestmark = pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")


def test_a_quiet_start_reaches_realtime_at_full_level_while_the_recognizer_gets_it_turned_down(sim):
    q = sim["quietStart"]
    assert q["o1"] > 0.85 * q["inp"]  # Realtime: the whole voice
    assert q["o0"] < 0.4 * q["inp"]  # the recognizer's output: turned down 14 dB, which cut quiet beginnings


def test_her_echo_never_reaches_realtime(sim):
    e = sim["echoOnly"]
    assert e["inp"] > 0.02 and e["o1"] < 0.05 * e["inp"]


def test_the_user_talking_over_her_passes(sim):
    t = sim["talkOver"]
    assert t["o1"] > 0.7 * t["inp"]


def test_an_answer_given_at_once_after_her_is_not_cut(sim):
    a = sim["answerAtOnce"]
    assert a["o1"] > 0.8 * a["inp"]  # 0.05 s after she stopped; output 0 still held its 0.3 s of silence there
    assert a["o0"] < 0.5 * a["o1"]
