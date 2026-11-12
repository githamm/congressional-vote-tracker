"""The page's script at least parses (needs Node; skipped without it)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node isn't installed")
def test_page_script_parses(tmp_path):
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "no inline script found"
    js = tmp_path / "page.js"
    js.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_page_has_no_copies_of_the_vote_rules():
    """The rules live in vote_rules.py; the page reads vote_info.json."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for name in ("FINAL_QUESTION", "DISPOSING_QUESTION", "isFinalVote", "partyMajority", "NON_STATE"):
        assert name not in html, name
