import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radar import extract_text, make_diff, slugify, text_hash  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
