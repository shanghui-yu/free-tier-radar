#!/usr/bin/env python3
"""Free Tier Radar — watch cloud free-tier & cheap-VPS pages for changes.

Zero dependencies (Python 3.9+ standard library only).

Usage:
    python3 radar.py            # fetch all sources, compare, write report
    python3 radar.py --seed     # (re)build the baseline snapshots, no report
    python3 radar.py --no-issue # same as default, but never open an Issue

State lives in state.json + snapshots/<slug>.txt (committed to the repo so
GitHub Actions runs can diff against the last known version).

Since v1.1, when a run detects changes it also opens one GitHub Issue
summarising them (title carries the UTC date and the number of changed
sources), so subscribers get notified without watching commits. Issue
creation needs GITHUB_TOKEN and GITHUB_REPOSITORY in the environment
(GitHub Actions provides both; the workflow grants `issues: write`).
Local runs without a token simply skip the Issue step and still write
the report. Set RADAR_NO_ISSUE=1 or pass --no-issue to skip it even
when a token is present.

Since v1.2, a raw text change is not enough to open an Issue: the
change first passes through a noise filter (`is_real_change`). It
normalizes away timestamps and relative dates (RackNerd comment
timestamps such as "Nov 4, 2026 05:33 (2 hours ago)"), treats pure
paragraph reordering as noise, and only treats a change as real when
the multiset of numbers (prices, quotas, durations, amounts) or the
set of eligibility/restriction terms moved — so marketing rewrites
that keep every number and every eligibility line ("The AI assistant
built only for work") stay quiet, while a price move (€74.70 → €75.70)
or a new terms restriction ("The $300 credit can't pay for Gemini …
on or after November 16, 2026") still opens an Issue. Noise-only
changes still update the snapshot (so they are not re-reported the
next day) and are listed in the report as filtered noise.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import urllib.request
import urllib.error
from collections import Counter
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


# --- v1.2 noise filter -------------------------------------------------
# Goal: a page changing must not, by itself, open an Issue. Three kinds
# of churn proved to be noise in production (Issues #1-#11):
#   * comment timestamps that advance every run ("Nov 4, 2026 05:33
#     (2 hours ago)") — RackNerd;
#   * marketing copy rewrites and paragraph reordering that keep every
#     number and every eligibility line — AWS Free Tier (Issue #1);
#   * "Last updated <date>" stamps and feedback-widget JSON blobs.
# A change is real only when the numbers on the page move (price,
# quota, duration, amount) or when eligibility / restriction wording
# moves. That keeps the Hetzner €74.70 -> €75.70 move and the GCP
# "$300 credit can't pay for Gemini … on or after November 16, 2026"
# terms addition (Issue #10) firing, while the noise above stays quiet.
# The comparison is deliberately number/term based instead of line based:
# rewording "12 months free" as "free for 12 months" keeps the number
# multiset intact, so it does not fire; reordering lines cannot fire
# either. Safe direction: when in doubt the filter fires (a number
# dropped by a rewrite, or a restriction line reworded, still opens an
# Issue) — a false alarm is cheaper than a missed terms change.

# Date/time strings that churn on their own. Each match is replaced by
# one placeholder before comparison, so only the surrounding words count.
TIMESTAMP_PATTERNS = [
    # ISO dates, optionally with a time: 2026-10-27, 2026-11-10 07:48:13
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b"),
    # "Nov 4, 2026 05:33" / "November 16, 2026" (with or without a time)
    re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
        r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
        r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\b",
        re.I,
    ),
    # "4 November 2026" / "16 November 2026"
    re.compile(
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{4}\b",
        re.I,
    ),
    # Relative times: (2 hours ago), 1 day ago, 3 weeks ago
    re.compile(
        r"\b\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)"
        r"\s+ago\b",
        re.I,
    ),
    # Bare clock times left over after the patterns above: 05:33, 23:34:12
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
]

# Words that mark eligibility / scope / restriction wording. A line (or
# the page) gaining or losing one of these terms is a terms change, even
# when no number moves — e.g. the GCP $300/Gemini restriction, which is
# pure prose. English + Chinese, matched as whole words where the
# language has word boundaries.
RESTRICTION_TERMS = [
    "eligible", "eligibility", "ineligible", "not eligible", "no longer",
    "cannot", "can't", "can not", "must not", "not available",
    "not supported", "excluded", "exclusion", "restricted", "restriction",
    "limited to", "only available", "applies to",
    "does not apply", "not be used", "cannot be used",
    "valid until", "expires", "requires", "requirement", "qualify",
    "qualification", "terms of",
    "资格", "适用", "限制", "不可", "不再", "仅限", "仅适用于", "有效期",
    "到期", "排除", "不支持", "不能用于",
]
RESTRICTION_RE = re.compile(
    r"|".join(re.escape(t) for t in RESTRICTION_TERMS), re.I
)

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")


def normalize_line(line: str) -> str:
    """Mask timestamps/dates in one line, collapse whitespace."""
    for pattern in TIMESTAMP_PATTERNS:
        line = pattern.sub("<DATE>", line)
    return re.sub(r"\s+", " ", line).strip()


def normalize_text(text: str) -> str:
    return "\n".join(normalize_line(line) for line in text.splitlines())


def numbers_of(text: str):
    """Multiset of the numbers on a (normalized) page: prices, quotas,
    durations, amounts. Timestamps are masked first, so a date rolling
    forward does not count as a number moving."""
    return Counter(NUMBER_RE.findall(normalize_text(text)))


def restriction_terms_of(text: str):
    """Multiset of eligibility/restriction terms on a (normalized) page."""
    return Counter(m.lower() for m in RESTRICTION_RE.findall(normalize_text(text)))


def is_real_change(old: str, new: str) -> bool:
    """v1.2 gate: is this raw text change worth opening an Issue for?

    False when the normalized texts are identical (timestamp-only churn),
    when only the line order changed (paragraph reordering), or when the
    numbers and the eligibility/restriction terms are all untouched
    (marketing rewrite). True otherwise.
    """
    old_n, new_n = normalize_text(old), normalize_text(new)
    if old_n == new_n:
        return False
    if Counter(old_n.splitlines()) == Counter(new_n.splitlines()):
        return False  # pure reordering
    if numbers_of(old) != numbers_of(new):
        return True
    if restriction_terms_of(old) != restriction_terms_of(new):
        return True
    return False


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


def summarize_diff(diff: str, max_sample: int = 8) -> dict:
    """Count added/removed lines in a unified diff and sample them.

    Returns {"added": int, "removed": int, "sample": str} where sample
    holds up to max_sample added/removed lines (diff headers excluded),
    so an Issue body stays readable even for a large page change.
    """
    added, removed, sample = 0, 0, []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
            if len(sample) < max_sample:
                sample.append(line)
        elif line.startswith("-"):
            removed += 1
            if len(sample) < max_sample:
                sample.append(line)
    return {"added": added, "removed": removed, "sample": "\n".join(sample)}


def build_issue_title(date_str: str, changes) -> str:
    """Issue title: UTC date + number of changed sources."""
    return f"🔔 {date_str}: {len(changes)} source(s) changed"


def build_issue_body(date_str: str, changes, max_chars: int = 8000) -> str:
    """One Issue body listing every changed source with a diff summary."""
    lines = [
        f"Free Tier Radar detected changes on {date_str} UTC.",
        "",
        f"**{len(changes)} source(s) changed.** Full diffs are in the "
        "`LATEST.md` report committed with this run.",
        "",
    ]
    for name, url, diff in changes:
        if diff == "(new source, baseline created)":
            summary = "new source, baseline created (no previous snapshot)"
            sample = ""
        else:
            s = summarize_diff(diff)
            summary = f"+{s['added']} / -{s['removed']} lines"
            sample = s["sample"]
        lines += [f"### {name}", f"<{url}>", "", summary, ""]
        if sample:
            lines += ["```diff", sample, "```", ""]
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n... (truncated)"
    return body


def create_github_issue(repo: str, token: str, title: str, body: str) -> str:
    """Open one Issue via the GitHub REST API (stdlib only).

    Returns the Issue's HTML URL. Raises on HTTP/API errors so the
    caller can log the failure without failing the whole radar run.
    """
    payload = json.dumps({"title": title, "body": body}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload, method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc
    return result.get("html_url", "")


def open_change_issue(changes, date_str: str, repo=None, token=None,
                      creator=None):
    """Open the change-summary Issue, or return None when skipped.

    Skipped (no error) when there are no changes, or when no token/repo
    is configured. `creator` is an injectable (title, body) -> url
    callable used by tests and dry runs; by default the real API call
    runs. Returns the Issue URL, or None when skipped.
    """
    if not changes:
        return None
    title = build_issue_title(date_str, changes)
    body = build_issue_body(date_str, changes)
    if creator is not None:
        # Injected creator (tests / dry runs) bypasses credentials.
        return creator(title, body)
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    token = token or os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("no GITHUB_TOKEN/GITHUB_REPOSITORY, skipping Issue", file=sys.stderr)
        return None
    return create_github_issue(repo, token, title, body)


def run(seed: bool = False, no_issue: bool = False,
        issue_creator=None) -> int:
    sources = load_json(SOURCES_FILE, [])
    if not sources:
        print("no sources configured", file=sys.stderr)
        return 2
    state = load_json(STATE_FILE, {})
    SNAP_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    changes, filtered, errors, unchanged = [], [], [], 0

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
                # v1.2: snapshot/state always advance on a raw change (so
                # noise is not re-reported tomorrow), but only a real
                # change joins `changes` and can open an Issue.
                if is_real_change(old_text, text):
                    changes.append((name, url, make_diff(old_text, text, name)))
                else:
                    s = summarize_diff(make_diff(old_text, text, name))
                    filtered.append((name, url, s["added"], s["removed"]))
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
    if filtered:
        lines += ["", f"## 🔇 Noise filtered ({len(filtered)} source(s), no Issue opened)"]
        for name, url, added, removed in filtered:
            lines.append(f"- **{name}**: +{added} / -{removed} lines "
                         f"(timestamps / marketing rewrite / reordering only)")
    lines += ["", f"Unchanged: {unchanged} · Errors: {len(errors)}"]
    if errors:
        lines += ["", "## Fetch errors", *errors]
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

    if not seed and changes and not no_issue \
            and not os.environ.get("RADAR_NO_ISSUE"):
        date_str = now.split()[0]
        try:
            url = open_change_issue(changes, date_str, creator=issue_creator)
            if url:
                print(f"opened Issue: {url}")
        except Exception as exc:  # Issue failure must not fail the run
            print(f"warning: failed to open Issue: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run(seed="--seed" in sys.argv,
                 no_issue="--no-issue" in sys.argv))
