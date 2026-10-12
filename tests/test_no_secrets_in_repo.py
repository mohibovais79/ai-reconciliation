"""Fail if anything key-like is committed to the repo."""

import re
from pathlib import Path

from recon.logging_utils import KEY_PATTERNS

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", ".data", "__pycache__", ".pytest_cache", ".ruff_cache", ".hypothesis"}
TEXT_EXT = {".py", ".toml", ".md", ".txt", ".csv", ".json", ".example", ".yml", ".yaml", ".cfg"}
ALLOW = re.compile(r"SENTINEL|REDACTED|abcdefghijklmnop|1234567890")


def test_no_key_like_strings():
    hits = []
    for p in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts) or not p.is_file():
            continue
        if p.suffix not in TEXT_EXT and p.name != ".env.example":
            continue
        if p.name == ".env":
            continue
        text = p.read_text(errors="ignore")
        for pat in KEY_PATTERNS[:6]:
            for m in pat.finditer(text):
                if not ALLOW.search(m.group(0)):
                    hits.append(f"{p.relative_to(ROOT)}: {m.group(0)[:8]}...")
    assert not hits, hits
