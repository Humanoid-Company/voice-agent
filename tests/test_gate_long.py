"""The gate in a LONG answer of the agent: the same user's voice counts early and late, even after quiet tries to talk over her
(the learned echo used to climb with them until the voice no longer passed: «в довгому повідомленні не перебивається»)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")
def test_the_voice_counts_at_any_point_of_a_long_answer():
    out = subprocess.run([JSC, "tests/gate_long_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    for echo, by_start in res.items():
        for start, kinds in by_start.items():
            for kind, r in kinds.items():
                assert r["share"] >= 80 and 0 <= r["first"] <= 500, (echo, start, kind, r)
