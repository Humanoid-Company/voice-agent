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


def test_the_site_has_no_password_and_history_knowledge_and_costs_are_behind_the_gear():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'type="password"' not in html and 'id="login"' not in html and 'id="logout"' not in html
    assert '<main class="wrap">' in html  # the app is there at once
    assert 'id="openSettings"' in html and "⚙" in html  # the gear at the top right
    for tab in ("history", "knowledge", "usage"):
        assert f'data-tab="{tab}"' in html
    assert 'id="openHistory"' not in html and 'id="openUsage"' not in html  # the two buttons are gone
    main = html[html.index('<main class="wrap">') : html.index("</main>")]
    assert 'id="about"' not in main  # the knowledge is not a card of the page: it is in the settings
    assert 'id="panelKnowledge"' in html and 'id="about"' in html
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8").lower()
    assert "x-access" not in app and "/api/login" not in app and "logout" not in app
