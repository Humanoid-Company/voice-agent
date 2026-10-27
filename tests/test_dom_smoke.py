"""Runs web/app.js against a fake DOM in JavaScriptCore (macOS: no Node needed). Skipped where `jsc` is missing."""
import shutil
import subprocess
from pathlib import Path

import pytest

JSC = shutil.which("jsc") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not Path(JSC).exists(), reason="JavaScriptCore shell (jsc) is not available")
def test_page_logic_in_a_fake_dom():
    out = subprocess.run([JSC, "tests/dom_smoke.js"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "SMOKE OK" in out.stdout, out.stdout + out.stderr
    assert "FAIL " not in out.stdout, out.stdout
