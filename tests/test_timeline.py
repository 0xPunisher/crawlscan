"""Timeline на главной: data/timeline.json и собранный index.html (вехи встроены при сборке, роадмапа нет)."""
import json, os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def built_template():
    src = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    a = re.search(r'<script type="__bundler/template">', src).end()
    return json.loads(src[a:src.find("</script>", a)])


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.data = json.load(open(os.path.join(ROOT, "data", "timeline.json"), encoding="utf-8"))
        self.t = built_template()

    def test_data(self):
        ms = self.data["milestones"]
        self.assertTrue(ms)
        for m in ms:
            self.assertEqual(set(m), {"date", "title", "text", "url"})
            self.assertRegex(m["url"], r"^https://x\.com/[A-Za-z0-9_]+/status/\d+$")
            self.assertRegex(m["date"], r"^[A-Z][a-z]{2} \d{1,2}$")
        self.assertTrue(all(isinstance(s, str) and s for s in self.data["next"]))

    def test_built_page(self):
        t = self.t
        mt = re.search(r"const TIMELINE=(.*?);\n", t)
        self.assertIsNotNone(mt)
        self.assertEqual(json.loads(mt.group(1).replace("<\\/", "</")), self.data)   # страница = файл
        self.assertIn('<section id="timeline"', t)
        self.assertIn(">Timeline</h2>", t)
        self.assertIn("What comes next", t)
        self.assertIn('target="_blank" rel="noopener noreferrer">Read the post on X ↗</a>', t)
        self.assertEqual(t.count('href="#timeline"'), 2)                              # шапка и меню
        for old in ('id="roadmap"', ">SHIPPED<", ">NEXT<", ">LATER<", "in progress</span>", 'href="#roadmap"'):
            self.assertNotIn(old, t)


if __name__ == "__main__":
    unittest.main()
