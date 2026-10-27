"""The whole voice-interruption chain on synthetic signals, run in JavaScriptCore: the gate's reports, then the page's
window rule. Speech is syllables with gaps; the echo has bursts, a delay of 120 ms and a reverberant tail. Skipped where `jsc` is missing.

Her own echo must never duck or stop her at the default sensitivity, a normal and a quiet voice must stop her fast."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


def test_the_simulation_uses_the_pages_constants():
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    sim = (ROOT / "tests/gate_chain_sim.js").read_text(encoding="utf-8")
    page = {n: float(v) for n, v in re.findall(r"const (VOICE_WINDOW|DUCK_AT|STOP_AT) = ([\d.]+);", app)}
    mirror = dict(zip(("VOICE_WINDOW", "DUCK_AT", "STOP_AT"), map(float, re.search(
        r"var VOICE_WINDOW = (\d+), DUCK_AT = (\d+), STOP_AT = (\d+);", sim).groups()), strict=True))  # fmt: skip
    assert page == mirror, "web/app.js and tests/gate_chain_sim.js disagree: the simulation proves nothing then"


@pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")
def test_echo_never_acts_and_a_voice_stops_her_fast():
    out = subprocess.run([JSC, "tests/gate_chain_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stdout + out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    for ratio in ("0.1", "0.2"):  # a good echo canceller, and a loud echo
        default = result[ratio][2]  # the default sensitivity: «Висока»
        assert default["falseStops"] == 0 and default["falseDucks"] == 0, f"she acted on her own echo (echo {ratio}): {default}"
        for level in result[ratio][:2]:
            assert level["falseStops"] == 0, f"she stopped herself at a low sensitivity (echo {ratio}): {level}"
    good = result["0.1"][2]
    assert 0 < good["normalStopMs"] <= 1000 and 0 < good["quietStopMs"] <= 1500, f"too slow or deaf: {good}"
    assert 0 < result["0.1"][3]["normalStopMs"] <= good["normalStopMs"]  # more sensitive is not slower
