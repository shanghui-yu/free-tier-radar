#!/usr/bin/env python3
"""Free Tier Radar — watch cloud free-tier & cheap-VPS pages for changes.

Zero dependencies (Python 3.9+ standard library only).

Usage:
    python3 radar.py            # fetch all sources, compare, write report
    python3 radar.py --seed     # (re)build the baseline snapshots, no report

State lives in state.json + snapshots/<slug>.txt (committed to the repo so
GitHub Actions runs can diff against the last known version).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
STATE_FILE = ROOT / "state.json"
SNAP_DIR = ROOT / "snapshots"
REPORT_FILE = ROOT / "LATEST.md"
UA = "Mozilla/5.0 (compatible; free-tier-radar/1.0; +https://github.com/)"


class TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style/noscript."""

    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


NOISE_PATTERNS = [
    re.compile(r"trace id", re.I),
    re.compile(r"request id", re.I),
    re.compile(r"csrf|nonce|session id", re.I),
    re.compile(r"\b[0-9a-f]{32,}\b"),          # random hex tokens / trace ids
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"),  # timestamps
]


def is_noise(line: str) -> bool:
    return any(p.search(line) for p in NOISE_PATTERNS)


def extract_text(html: str) -> str:
    parser = TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line and not is_noise(line):
            lines.append(line)
    # Collapse 3+ identical consecutive lines (menus repeated per breakpoint).
    out: list[str] = []
    for line in lines:
        if not out or out[-1] != line:
            out.append(line)
    return "\n".join(out)


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="ignore")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "source"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def make_diff(old: str, new: str, name: str, max_lines: int = 120) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"{name} (before)", tofile=f"{name} (now)", lineterm="",
        )
    )
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... diff truncated ({len(diff)} lines total)"]
    return "\n".join(diff)


def run(seed: bool = False) -> int:
    sources = load_json(SOURCES_FILE, [])
    if not sources:
        print("no sources configured", file=sys.stderr)
        return 2
    state = load_json(STATE_FILE, {})
    SNAP_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    changes, errors, unchanged = [], [], 0

    for src in sources:
        name, url = src["name"], src["url"]
        slug = slugify(name)
        snap_path = SNAP_DIR / f"{slug}.txt"
        try:
            text = extract_text(fetch(url))
        except Exception as exc:  # network, TLS, HTTP errors
            errors.append(f"- ❌ **{name}**: fetch failed ({exc.__class__.__name__}: {exc})")
            continue
        digest = text_hash(text)
        prev = state.get(slug, {})
        if seed or prev.get("hash") != digest:
            if not seed and prev:
                old_text = snap_path.read_text(encoding="utf-8") if snap_path.exists() else ""
                changes.append((name, url, make_diff(old_text, text, name)))
            elif not seed:
                changes.append((name, url, "(new source, baseline created)"))
            snap_path.write_text(text, encoding="utf-8")
            state[slug] = {
                "name": name, "url": url, "hash": digest,
                "chars": len(text), "updated_at": now,
            }
        else:
            unchanged += 1

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    if seed:
        print(f"seeded {len(state)} sources")
        return 0

    lines = [f"# Free Tier Radar — {now}", ""]
    if changes:
        lines.append(f"## 🔔 {len(changes)} source(s) changed")
        for name, url, diff in changes:
            lines += ["", f"### {name}", f"<{url}>", "", "```diff", diff, "```"]
    else:
        lines.append("## ✅ No changes detected")
    lines += ["", f"Unchanged: {unchanged} · Errors: {len(errors)}"]
    if errors:
        lines += ["", "## Fetch errors", *errors]
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(run(seed="--seed" in sys.argv))
