import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import radar  # noqa: E402
from radar import (  # noqa: E402
    build_issue_body, build_issue_title, create_github_issue, extract_text,
    is_real_change, make_diff, open_change_issue, slugify, summarize_diff,
    text_hash,
)

FIXTURE = """
<html><head><style>.x{color:red}</style><script>var a=1;</script></head>
<body><h1>Free Tier</h1><p>2 OCPU   12 GB RAM</p><p>2 OCPU   12 GB RAM</p>
<ul><li>200 GB storage</li></ul></body></html>
"""


class TestRadar(unittest.TestCase):
    def test_extract_strips_scripts_and_dedupes(self):
        text = extract_text(FIXTURE)
        self.assertIn("Free Tier", text)
        self.assertIn("2 OCPU 12 GB RAM", text)
        self.assertNotIn("var a=1", text)
        self.assertNotIn("color:red", text)
        self.assertEqual(text.count("2 OCPU 12 GB RAM"), 1)

    def test_noise_lines_filtered(self):
        html = FIXTURE + "<p>This is the Trace Id: c0e43ce4a58684f1affc94d4bf0c9097</p>"
        self.assertNotIn("Trace Id", extract_text(html))

    def test_hash_stable(self):
        self.assertEqual(text_hash("abc"), text_hash("abc"))
        self.assertNotEqual(text_hash("abc"), text_hash("abd"))

    def test_slugify(self):
        self.assertEqual(slugify("Oracle Cloud Always Free"), "oracle-cloud-always-free")

    def test_diff_shows_change(self):
        diff = make_diff("price $5\nforever free", "price $6\nforever free", "X")
        self.assertIn("-price $5", diff)
        self.assertIn("+price $6", diff)


class TestIssueSummary(unittest.TestCase):
    DIFF = make_diff("price $5\nforever free", "price $6\nforever free", "X")

    def test_summarize_counts_and_skips_headers(self):
        s = summarize_diff(self.DIFF)
        self.assertEqual((s["added"], s["removed"]), (1, 1))
        self.assertIn("+price $6", s["sample"])
        self.assertIn("-price $5", s["sample"])
        self.assertNotIn("+++", s["sample"])

    def test_title_carries_date_and_count(self):
        title = build_issue_title("2026-11-05", [("A", "u", "d"), ("B", "u", "d")])
        self.assertIn("2026-11-05", title)
        self.assertIn("2 source(s)", title)

    def test_body_lists_each_changed_source(self):
        body = build_issue_body("2026-11-05", [
            ("Oracle", "https://example.test/o", self.DIFF),
            ("Newbie", "https://example.test/n", "(new source, baseline created)"),
        ])
        self.assertIn("Oracle", body)
        self.assertIn("https://example.test/o", body)
        self.assertIn("+1 / -1 lines", body)
        self.assertIn("baseline created", body)

    def test_body_truncates(self):
        big = [("Big", "https://example.test/b", "\n".join(f"+line {i}" for i in range(500)))]
        self.assertLessEqual(len(build_issue_body("2026-11-05", big, max_chars=500)), 560)


class TestIssueCreation(unittest.TestCase):
    def test_create_issue_posts_title_and_body(self):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"html_url": "https://github.com/x/y/issues/7"}).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode())
            captured["auth"] = req.get_header("Authorization")
            return FakeResp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            url = create_github_issue("x/y", "tok123", "T", "B")
        self.assertEqual(url, "https://github.com/x/y/issues/7")
        self.assertEqual(captured["url"], "https://api.github.com/repos/x/y/issues")
        self.assertEqual(captured["payload"], {"title": "T", "body": "B"})
        self.assertEqual(captured["auth"], "Bearer tok123")

    def test_create_issue_surfaces_api_error(self):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden",
                                         {}, io.BytesIO(b'{"message":"forbidden"}'))

        with mock.patch("urllib.request.urlopen", boom):
            with self.assertRaises(RuntimeError) as ctx:
                create_github_issue("x/y", "tok", "T", "B")
        self.assertIn("403", str(ctx.exception))

    def test_open_skips_without_changes_or_credentials(self):
        self.assertIsNone(open_change_issue([], "2026-11-05",
                                            repo="x/y", token="t"))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(open_change_issue([("A", "u", "d")], "2026-11-05"))

    def test_open_uses_injected_creator(self):
        seen = {}

        def capture(title, body):
            seen["t"], seen["b"] = title, body
            return "https://example.test/issue"

        url = open_change_issue([("A", "https://example.test/a", "diff")],
                                "2026-11-05", repo="x/y", token="t",
                                creator=capture)
        self.assertIn("2026-11-05", seen["t"])
        self.assertIn("1 source(s)", seen["t"])
        self.assertIn("https://example.test/a", seen["b"])
        self.assertEqual(url, "https://example.test/issue")


class TestRunOpensIssueOnChange(unittest.TestCase):
    """End-to-end: fetch -> diff -> issue decision, all stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sources.json").write_text(json.dumps([
            {"name": "Alpha", "url": "https://example.test/alpha"},
            {"name": "Beta", "url": "https://example.test/beta"},
        ]))
        (self.root / "snapshots").mkdir()
        (self.root / "snapshots" / "alpha.txt").write_text("price $5")
        (self.root / "snapshots" / "beta.txt").write_text("same")
        (self.root / "state.json").write_text(json.dumps({
            "alpha": {"name": "Alpha", "url": "https://example.test/alpha",
                      "hash": radar.text_hash("price $5"), "chars": 8,
                      "updated_at": "2026-11-04 01:17 UTC"},
            "beta": {"name": "Beta", "url": "https://example.test/beta",
                     "hash": radar.text_hash("same"), "chars": 4,
                     "updated_at": "2026-11-04 01:17 UTC"},
        }))
        self.orig = (radar.SOURCES_FILE, radar.STATE_FILE, radar.SNAP_DIR,
                     radar.REPORT_FILE)
        radar.SOURCES_FILE = self.root / "sources.json"
        radar.STATE_FILE = self.root / "state.json"
        radar.SNAP_DIR = self.root / "snapshots"
        radar.REPORT_FILE = self.root / "LATEST.md"

    def tearDown(self):
        (radar.SOURCES_FILE, radar.STATE_FILE, radar.SNAP_DIR,
         radar.REPORT_FILE) = self.orig
        self.tmp.cleanup()

    def test_change_opens_issue_once_then_quiet(self):
        pages = {"https://example.test/alpha": "<html><body><p>price $6</p></body></html>",
                 "https://example.test/beta": "<html><body><p>same</p></body></html>"}
        opened = []
        with mock.patch.object(radar, "fetch", side_effect=lambda u: pages[u]):
            self.assertEqual(radar.run(issue_creator=lambda t, b: opened.append((t, b)) or "url"), 0)
        self.assertEqual(len(opened), 1)
        self.assertIn("1 source(s)", opened[0][0])
        self.assertIn("Alpha", opened[0][1])
        self.assertNotIn("### Beta", opened[0][1])
        with mock.patch.object(radar, "fetch", side_effect=lambda u: pages[u]):
            self.assertEqual(radar.run(issue_creator=lambda t, b: opened.append((t, b)) or "url"), 0)
        self.assertEqual(len(opened), 1, "second run with no new change opens nothing")

    def test_no_issue_flag_suppresses(self):
        pages = {"https://example.test/alpha": "<p>price $9</p>",
                 "https://example.test/beta": "<p>same</p>"}
        opened = []
        with mock.patch.object(radar, "fetch", side_effect=lambda u: pages[u]):
            radar.run(no_issue=True,
                      issue_creator=lambda t, b: opened.append((t, b)) or "url")
        self.assertEqual(opened, [])


class TestNoiseFilter(unittest.TestCase):
    """v1.2: timestamp/marketing churn stays quiet, real changes fire.

    Fixtures are verbatim lines from production Issues #1/#10/#11.
    """

    # RackNerd comment timestamps, Issue #1 (+4/-4, pure noise).
    RACKNERD_OLD = (
        "RackNerd | VPS Specials\n"
        "Nov 4, 2026 05:33 (2 hours ago)\n"
        "Nov 3, 2026 07:33 (1 day ago)\n"
        "Nov 1, 2026 07:33 (3 days ago)\n"
        "Oct 28, 2026 07:33 (1 week ago)\n"
        "2GB RAM KVM VPS $12.99 per year"
    )
    RACKNERD_NEW = (
        "RackNerd | VPS Specials\n"
        "Nov 4, 2026 23:34 (2 hours ago)\n"
        "Nov 4, 2026 01:34 (1 day ago)\n"
        "Nov 2, 2026 01:34 (3 days ago)\n"
        "Oct 29, 2026 01:34 (1 week ago)\n"
        "2GB RAM KVM VPS $12.99 per year"
    )

    # AWS marketing rewrite, Issue #1 (+14/-13, no number/term moved).
    AWS_OLD = (
        "Free Cloud Computing Services - AWS Free Tier\n"
        "AI-powered assistant for work with research, business insights, automation, and no-code app building\n"
        "The end-to-end platform for building generative AI applications and agents\n"
        "12 months free\n"
        "750 hours of EC2 per month"
    )
    AWS_NEW = (
        "Free Cloud Computing Services - AWS Free Tier\n"
        "Our Impact\n"
        "The AI assistant built only for work\n"
        "The trusted platform to run inference and build agents at scale\n"
        "Migration\n"
        "Available on both plans\n"
        "12 months free\n"
        "750 hours of EC2 per month"
    )

    # GCP $300/Gemini terms addition, Issue #10 (real change, no price move).
    GCP_OLD = (
        "Free Google Cloud features and trial offer\n"
        "Get $300 in free credits to explore Google Cloud\n"
        "Last updated 2026-10-27 UTC."
    )
    GCP_NEW = (
        "Free Google Cloud features and trial offer\n"
        "Get $300 in free credits to explore Google Cloud\n"
        "The $300 credit can't pay for Gemini\n"
        "platform (formerly known as\n"
        "Gemini Enterprise Agent Platform, Vertex AI)\n"
        "services if you sign up for a free trial or upgrade to a paid\n"
        "account on or after November 16, 2026.\n"
        "Last updated 2026-11-10 UTC."
    )

    def test_timestamp_only_change_is_noise(self):
        self.assertFalse(is_real_change(self.RACKNERD_OLD, self.RACKNERD_NEW))

    def test_marketing_rewrite_and_reorder_is_noise(self):
        # Reworded marketing lines, plus the same lines in a new order.
        reordered = "\n".join([
            "750 hours of EC2 per month",
            "Free Cloud Computing Services - AWS Free Tier",
            "The trusted platform to run inference and build agents at scale",
            "Our Impact",
            "The AI assistant built only for work",
            "12 months free",
            "Migration",
            "Available on both plans",
        ])
        self.assertFalse(is_real_change(self.AWS_OLD, self.AWS_NEW))
        self.assertFalse(is_real_change(self.AWS_OLD, reordered))

    def test_price_change_fires(self):
        old = "Hetzner Cloud Prices\n€ 74.70 per month, excl. VAT"
        new = "Hetzner Cloud Prices\n€ 75.70 per month, excl. VAT"
        self.assertTrue(is_real_change(old, new))

    def test_quota_number_change_fires(self):
        self.assertTrue(is_real_change(
            "750 hours of EC2 per month", "500 hours of EC2 per month"))

    def test_gcp_terms_restriction_fires(self):
        # Pure-prose eligibility restriction: no price/number moved that
        # would fire on its own (the $300 appears on both sides), yet
        # the new "can't pay for Gemini … on or after November 16, 2026"
        # lines must still open an Issue.
        self.assertTrue(is_real_change(self.GCP_OLD, self.GCP_NEW))

    def test_last_updated_date_bump_is_noise(self):
        old = "Docs\nLast updated 2026-10-27 UTC.\nFree tier"
        new = "Docs\nLast updated 2026-11-10 UTC.\nFree tier"
        self.assertFalse(is_real_change(old, new))

    def test_identical_text_is_noise(self):
        self.assertFalse(is_real_change("same\ntext", "same\ntext"))


class TestRunNoiseFilter(TestRunOpensIssueOnChange):
    """End-to-end: noise updates the snapshot but opens no Issue."""

    def test_timestamp_only_change_updates_snapshot_without_issue(self):
        pages = {"https://example.test/alpha":
                 "<html><body><p>Nov 4, 2026 05:33 (2 hours ago)</p>"
                 "<p>price $5</p></body></html>",
                 "https://example.test/beta": "<html><body><p>same</p></body></html>"}
        opened = []
        with mock.patch.object(radar, "fetch", side_effect=lambda u: pages[u]):
            self.assertEqual(radar.run(issue_creator=lambda t, b: opened.append((t, b)) or "url"), 0)
        # Snapshot advanced (raw text changed) but no Issue was opened…
        self.assertEqual(opened, [])
        report = (self.root / "LATEST.md").read_text()
        self.assertIn("Noise filtered", report)
        self.assertIn("Alpha", report)
        # …and a second identical run stays quiet too.
        with mock.patch.object(radar, "fetch", side_effect=lambda u: pages[u]):
            self.assertEqual(radar.run(issue_creator=lambda t, b: opened.append((t, b)) or "url"), 0)
        self.assertEqual(opened, [])

    def test_price_change_still_opens_issue(self):
        pages = {"https://example.test/alpha":
                 "<html><body><p>price $6</p></body></html>",
                 "https://example.test/beta": "<html><body><p>same</p></body></html>"}
        opened = []
        with mock.patch.object(radar, "fetch", side_effect=lambda u: pages[u]):
            self.assertEqual(radar.run(issue_creator=lambda t, b: opened.append((t, b)) or "url"), 0)
        self.assertEqual(len(opened), 1)
        self.assertIn("Alpha", opened[0][1])


if __name__ == "__main__":
    unittest.main()
