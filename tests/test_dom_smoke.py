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


def test_the_markup_asks_for_the_password_first_and_keeps_history_knowledge_and_costs_behind_the_gear():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert html.index('id="login"') < html.index('id="app"')  # the password comes first
    assert '<main class="wrap" id="app" hidden>' in html  # the app is not shown before it
    assert 'id="loginPass" type="password"' in html and 'id="loginBtn"' in html
    assert 'id="openSettings"' in html and "⚙" in html  # the gear at the top right
    for tab in ("history", "knowledge", "usage"):
        assert f'data-tab="{tab}"' in html
    assert 'id="openHistory"' not in html and 'id="openUsage"' not in html  # the two buttons are gone
    assert 'id="logout"' in html
    # the knowledge is no longer a card of the page: it lives in the settings, and has no password of its own
    main = html[html.index('id="app"') : html.index("</main>")]
    assert 'id="about"' not in main and "aboutPass" not in html
    assert 'id="panelKnowledge"' in html and 'id="about"' in html
    assert "localStorage" not in html
