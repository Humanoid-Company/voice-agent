"""A phone: the speaker beside the microphone, her echo as loud as (or louder than) the sound that made it, nobody speaking.
The echo must not pass for the user (the page would stop her) and must not reach Realtime (its VAD would stop her too):
that was «щось сам себе перебиває» on an iPhone."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")
def test_her_own_echo_on_a_phone_neither_counts_as_the_user_nor_reaches_realtime():
    out = subprocess.run([JSC, "tests/gate_phone_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert set(res) == {"0.5", "0.8", "1.2", "1.8"}
    for echo, r in res.items():
        assert r["nearPct"] <= 2.0, (echo, r)  # of 40 s taken for the user's voice
        assert r["toRealtime"] <= 0.12, (echo, r)  # of her echo's level that reached the model
