"""web/pcm.js (the microphone as PCM16 24 kHz for Realtime) run in JavaScriptCore on a synthetic tone."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")
def test_pcm_tap_resamples_to_24k_in_40ms_chunks():
    out = subprocess.run([JSC, "tests/pcm_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    for key in ("r48", "r44"):
        assert r[key]["allFull"]  # 960 samples = 40 ms at 24 kHz
        assert 50 <= r[key]["chunks"] <= 52  # ~2 s of sound
        assert abs(r[key]["samples"] - 2.05 * 24000) < 960 + 50
        assert abs(r[key]["hz"] - 440) <= 6  # the pitch survives the resampling
        assert 0.4 * 32767 <= r[key]["peak"] <= 0.55 * 32767  # amplitude 0.5, not clipped or lost
    assert r["clip"]["peak"] <= 32768  # a too loud input is clamped, not wrapped
