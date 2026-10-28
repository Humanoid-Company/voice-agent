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


def test_the_knowledge_text_is_locked_in_the_markup_until_the_password_is_entered():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert '<div id="aboutEditor" hidden>' in html  # the text is not shown before the password
    assert 'id="aboutPass" type="password"' in html and 'id="aboutLock"' in html
    assert "localStorage" not in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
