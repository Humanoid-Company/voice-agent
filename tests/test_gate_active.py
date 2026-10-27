"""The gate's `active` flag in JavaScriptCore: on through the gaps between syllables, off soon after the speech ends,
never turned on by her own echo. It is what keeps the page from cutting a phrase in two. Skipped without `jsc`."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")
def test_active_flag_follows_the_voice_and_not_the_echo():
    out = subprocess.run([JSC, "tests/gate_active_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["speechActive"] >= 0.95, f"gaps between syllables must not look like the end of the phrase: {r}"
    assert r["silenceActive"] <= 0.1, f"the phrase must be seen as ended soon after the voice stops: {r}"
    assert r["quietBeforeActive"] <= 0.05, f"a quiet room is not activity: {r}"
    assert r["echoOnlyActive"] <= 0.02, f"her own echo must not hold the user's phrase open: {r}"
