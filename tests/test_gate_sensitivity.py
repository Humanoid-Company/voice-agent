"""The echo gate (web/gate.js) on synthetic signals, run in JavaScriptCore. Skipped where `jsc` is missing.

The gate must never take the assistant's own echo for the user's voice, whatever the sensitivity; the higher the
sensitivity, the quieter a voice it lets through while she talks."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")
def test_echo_is_never_the_voice_and_higher_sensitivity_lets_a_quieter_voice_through():
    out = subprocess.run([JSC, "tests/gate_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    for ratio, levels in result.items():  # echo ratio 0.1 (a good canceller) and 0.2 (a loud echo)
        # the default level (2) and the lower ones must never take her echo for the user, even a loud one
        assert [lv["echoOnly"] for lv in levels[:3]] == [0, 0, 0], f"she heard herself at echo {ratio}: {levels}"
        assert levels[3]["quiet"] >= levels[2]["quiet"] >= levels[1]["quiet"] >= levels[0]["quiet"]
        assert levels[2]["normal"] > 50, f"a normal voice must interrupt her at the default level (echo {ratio})"
    # the most sensitive level (3) is labelled on the page as one that may interrupt her by her own echo, and does
    # so only when the echo is loud: with a good canceller it must stay quiet too
    assert result["0.1"][3]["echoOnly"] == 0
    # the default level (2) lets a normal voice through where Eva's own (1) did not, with a loud echo
    assert result["0.2"][2]["quiet"] > result["0.2"][1]["quiet"]
