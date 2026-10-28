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


def test_the_lines_that_change_ten_times_a_second_have_a_fixed_height_so_a_phone_does_not_shake():
    import re

    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.hearing-status\s*\{[^}]*height:\s*calc\(1\.4em \* 3\)", css)
    assert re.search(r"#recog:not\(\[hidden\]\)\s*\{[^}]*height:\s*calc\(1\.4em \* 2\)", css)
    assert 'class="hint hearing-status"' in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    # and what the hearing line says fits its three lines on a narrow screen (about 38 characters a line)
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function renderHearing()") : js.index("function hearEvent(")]
    texts = []
    for line in body.splitlines():
        if "text = " in line and '"' in line:
            expr = re.sub(r'" \+ n\([^)]*\) \+ "', "99.9", line.split("text = ", 1)[1])
            texts.append(expr[expr.index('"') + 1 : expr.rindex('"')])
    assert len(texts) >= 8 and max(len(t) for t in texts) <= 105, max(texts, key=len)
