from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

from ontology_kb.common import ROOT, api_query, catalog, load_config, parse_id, read_json, safe_id, search_url, write_json, writer_lock
from ontology_kb.fulltext import cache_valid, chunks_from_blocks, fetch_paper, html_blocks, pdf_blocks, read_chunk
from ontology_kb.index import build, format_results, search
from ontology_kb.network import Client, FetchError
from ontology_kb.sync import merge, parse_feed, update


def paper(number="2401.00001", version=1, title="Ontology alignment with language models", abstract="Ontology alignment and entity matching with language models."):
    return {"id": number, "version": version, "title": title, "abstract": abstract,
            "published": "2024-01-01T00:00:00Z", "updated": f"2024-01-0{version}T00:00:00Z",
            "year_month": "2024-01", "authors": ["A Researcher"], "categories": ["cs.AI", "stat.ML"],
            "primary_category": "cs.AI", "comment": "", "doi": "", "journal_ref": "",
            "abs_url": f"https://arxiv.org/abs/{number}v{version}", "pdf_url": f"https://arxiv.org/pdf/{number}v{version}"}


def feed(items, total=None, start=0):
    entries = []
    for p in items:
        entries.append(f'''<entry><id>{p['abs_url']}</id><title>{escape(p['title'])}</title>
          <summary>{escape(p['abstract'])}</summary><published>{p['published']}</published><updated>{p['updated']}</updated>
          <author><name>A Researcher</name></author><x:primary_category term="{p['primary_category']}"/>
          {''.join('<category term="' + c + '"/>' for c in p['categories'])}</entry>''')
    return (f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/" xmlns:x="http://arxiv.org/schemas/atom">
      <o:totalResults>{len(items) if total is None else total}</o:totalResults><o:startIndex>{start}</o:startIndex>{''.join(entries)}</feed>''').encode()


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class LibraryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load_config(ROOT)
        self.cfg["page_size"] = 2
        self.cfg["title_page_size"] = 2
        write_json(self.root / "config.json", self.cfg)

    def tearDown(self):
        self.temp.cleanup()


class LibraryTests(LibraryFixture):
    def test_query_preserves_unquoted_terms_and_subjects(self):
        self.assertEqual(api_query(self.cfg), "all:ontology AND all:language AND all:model AND (cat:cs.* OR cat:econ.* OR cat:eess.* OR cat:math.* OR cat:stat.*)")
        query = parse_qs(urlparse(search_url(self.cfg)).query)
        self.assertEqual(query["terms-1-term"], ["language model"])
        self.assertEqual(query["classification-include_cross_list"], ["include"])
        self.assertNotIn("classification-physics", query)

    def test_ids_and_path_traversal(self):
        self.assertEqual(parse_id("https://arxiv.org/pdf/cs/9901001v2.pdf"), ("cs/9901001", 2))
        self.assertEqual(safe_id("cs/9901001v2"), "cs_9901001v2")
        for value in ("../../a", "2401.00001/../../x", "2401.00001v0", "https://evil.test/2401.00001"):
            with self.assertRaises(ValueError):
                parse_id(value)

    def test_atom_parses_original_abstract_and_crosslists(self):
        p = paper(abstract="A & B < C: ontology learning.")
        p["categories"] = ["physics.soc-ph", "cs.AI"]
        p["primary_category"] = "physics.soc-ph"
        total, offset, records = parse_feed(feed([p]))
        self.assertEqual((total, offset), (1, 0))
        self.assertEqual(records[0]["abstract"], p["abstract"])
        self.assertIn("cs.AI", records[0]["categories"])

    def test_api_error_and_html_challenge_rejected(self):
        for raw in (b"<html>challenge</html>", b"not xml", b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad</id><summary>Invalid query</summary></entry></feed>'):
            with self.assertRaises(ValueError):
                parse_feed(raw)

    def test_merge_is_idempotent_and_detects_old_revision(self):
        first, _ = merge({}, [paper()], "t1")
        again, counts = merge(first, [paper()], "t2")
        self.assertEqual(counts["unchanged"], 1)
        self.assertEqual(len(again), 1)
        newer, counts = merge(again, [paper(version=2)], "t3")
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(newer["2401.00001"]["first_seen"], "t1")
        stale, _ = merge(newer, [paper()], "t4")
        self.assertEqual(stale["2401.00001"]["version"], 2)

    def test_missing_records_are_retained_but_not_default_search(self):
        first, _ = merge({}, [paper()], "t1")
        second, counts = merge(first, [], "t2")
        self.assertFalse(second["2401.00001"]["in_latest_search"])
        self.assertEqual(counts["not_in_latest_search"], 1)
        self.assertFalse(search(second, "ontology"))
        self.assertTrue(search(second, "ontology", include_missing=True))

    def test_interrupted_scan_resumes_without_committing_partial_catalog(self):
        a, b, c = paper(), paper("2401.00002"), paper("2401.00003")
        first = FakeClient([feed([a, b], 3, 0), FetchError("offline")])
        with self.assertRaises(FetchError):
            update(self.root, self.cfg, first, progress=lambda _: None)
        self.assertFalse((self.root / "data/catalog.json").exists())
        self.assertEqual(read_json(self.root / "data/sync-progress.json")["next_start"], 2)
        second = FakeClient([feed([c], 3, 2)])
        data, report = update(self.root, self.cfg, second, progress=lambda _: None)
        self.assertIn("start=2", second.calls[0])
        self.assertEqual(report["added"], 3)
        self.assertEqual(len(data["papers"]), 3)
        self.assertFalse((self.root / "data/sync-progress.json").exists())

    def test_duplicate_pagination_cannot_overwrite_existing_catalog(self):
        update(self.root, self.cfg, FakeClient([feed([paper()])]), progress=lambda _: None)
        before = (self.root / "data/catalog.json").read_bytes()
        a, b = paper(), paper("2401.00002")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            update(self.root, self.cfg, FakeClient([feed([a, b], 3), feed([b], 3, 2)]), progress=lambda _: None)
        self.assertEqual((self.root / "data/catalog.json").read_bytes(), before)

    def test_result_drift_and_empty_middle_page_are_errors(self):
        for response in (feed([paper("2401.00003")], 4, 2), feed([], 3, 2)):
            with self.subTest(response=response):
                client = FakeClient([feed([paper(), paper("2401.00002")], 3), response])
                with self.assertRaises(ValueError):
                    update(self.root, self.cfg, client, restart=True, progress=lambda _: None)
                self.assertFalse((self.root / "data/catalog.json").exists())

    def test_wrong_category_fails_but_cross_list_is_kept(self):
        p = paper()
        p["categories"], p["primary_category"] = ["physics.soc-ph", "cs.AI"], "physics.soc-ph"
        data, _ = update(self.root, self.cfg, FakeClient([feed([p])]), progress=lambda _: None)
        self.assertEqual(len(data["papers"]), 1)
        p["categories"] = ["physics.soc-ph"]
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            update(self.root, self.cfg, FakeClient([feed([p])]), progress=lambda _: None)

    def test_update_query_change_cannot_silently_merge_other_dataset(self):
        update(self.root, self.cfg, FakeClient([feed([paper()])]), progress=lambda _: None)
        self.cfg["terms"] = ["something else"]
        with self.assertRaisesRegex(ValueError, "criteria changed"):
            update(self.root, self.cfg, FakeClient([]), progress=lambda _: None)

    def test_shards_and_card_links_are_portable_and_bounded(self):
        entries = [paper(f"2401.{n:05d}", title=f"Ontology alignment {n} | <tag>") for n in range(1, 6)]
        records, _ = merge({}, entries, "stamp")
        build(self.root, self.cfg, {"papers": records, "last_sync": "stamp"})
        directory = self.root / "index/topics/alignment"
        self.assertEqual(len(list(directory.glob("0*.md"))), 3)
        for path in directory.glob("0*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(re.findall(r"^\| 2024", text, re.M)), 2)
            self.assertNotIn("<tag>", text)
            for target in re.findall(r"\]\(([^)]+)\)", text):
                self.assertTrue((path.parent / target).resolve().exists())
        self.assertNotIn(entries[0]["abstract"], (self.root / "START_HERE.md").read_text(encoding="utf-8"))

    def test_bm25_chinese_alias_title_boost_and_output_budget(self):
        a = paper()
        b = paper("2401.00002", title="RNN Transducers for alignment", abstract="alignment " * 50)
        results = search({a["id"]: a, b["id"]: b}, "本体对齐", 2)
        self.assertEqual(results[0][1]["id"], a["id"])
        self.assertLessEqual(len(format_results(results, abstracts=True, max_chars=500)), 500)
        self.assertEqual(search({a["id"]: a}, "ontology", year=1999), [])

    def test_chunk_budget_with_giant_unbroken_text(self):
        chunks = chunks_from_blocks([("Intro", "word " * 900), ("Methods", "x" * 5200)], 1000)
        self.assertTrue(all(len(c["text"]) <= 1000 for c in chunks))
        self.assertTrue(all(c["sections"] for c in chunks))
        self.assertEqual(sum(c["text"].count("x") for c in chunks), 5200)

    def test_lock_prevents_concurrent_writers(self):
        with writer_lock(self.root):
            with self.assertRaises(RuntimeError):
                with writer_lock(self.root):
                    pass
        with writer_lock(self.root):
            pass

    def test_network_retries_transient_errors_but_stops_on_403(self):
        client = Client(self.cfg, self.root)
        with patch.object(client, "_throttle"), patch("ontology_kb.network.time.sleep"), patch.object(client, "_request", side_effect=[(503, b"", {}), (200, b"ok", {})]) as request:
            self.assertEqual(client.get("https://export.arxiv.org/api/query?x=y"), b"ok")
            self.assertEqual(request.call_count, 2)
        with patch.object(client, "_throttle"), patch.object(client, "_request", return_value=(403, b"", {})) as request:
            with self.assertRaises(FetchError) as error:
                client.get("https://export.arxiv.org/api/query?x=y")
            self.assertEqual(error.exception.status, 403)
            self.assertEqual(request.call_count, 1)

    def test_rate_limits_are_persisted(self):
        client = Client(self.cfg, self.root)
        with patch("ontology_kb.network.time.time", return_value=100), patch("ontology_kb.network.time.sleep") as sleep:
            client._throttle(False)
            client._throttle(False)
            self.assertAlmostEqual(sleep.call_args.args[0], 3.1)
            client._throttle(True)
            self.assertAlmostEqual(sleep.call_args.args[0], 15.1)


@unittest.skipUnless(importlib.util.find_spec("bs4"), "Optional beautifulsoup4 is not installed")
class FulltextTests(LibraryFixture):
    def test_html_preserves_math_and_sections_without_scripts(self):
        raw = ('<article class="ltx_document"><h1>Title</h1><h2>Methods</h2><p>' + "Detailed paper content. " * 30 + '</p><p><math alttext="x^2"><mi>x</mi></math></p><script>bad()</script></article>').encode()
        blocks, _ = html_blocks(raw)
        text = " ".join(t for _, t in blocks)
        self.assertIn("x^2", text)
        self.assertNotIn("bad()", text)
        self.assertEqual(blocks[-1][0], "Methods")

    def test_fetch_cache_corruption_repair_and_version_isolation(self):
        raw = ('<article><h1>Test paper</h1><p>' + 'Research content. ' * 500 + '</p></article>').encode()
        p = paper()
        client = FakeClient([raw])
        contents = fetch_paper(self.root, self.cfg, client, p)
        directory = contents.parent
        self.assertTrue(cache_valid(directory))
        self.assertEqual(len(client.calls), 1)
        fetch_paper(self.root, self.cfg, client, p)
        self.assertEqual(len(client.calls), 1)
        (directory / "chunks/0001.md").write_text("corrupt", encoding="utf-8")
        self.assertFalse(cache_valid(directory))
        with self.assertRaisesRegex(ValueError, "corrupt"):
            read_chunk(self.root, p, 1)
        fetch_paper(self.root, self.cfg, client, p)
        self.assertTrue(cache_valid(directory))
        self.assertEqual(len(client.calls), 1)
        with self.assertRaisesRegex(ValueError, "not cached"):
            read_chunk(self.root, paper(version=2))
        self.assertLessEqual(len(read_chunk(self.root, p, 1, 500)), 500)

    def test_html_404_falls_back_to_pdf_but_403_does_not(self):
        pdf = b"%PDF-test body"
        with patch("ontology_kb.fulltext.pdf_blocks", return_value=([("PDF page 1", "full text " * 100)], [])):
            client = FakeClient([FetchError("not found", 404), pdf])
            contents = fetch_paper(self.root, self.cfg, client, paper())
            self.assertEqual(read_json(contents.parent / "manifest.json")["format"], "pdf")
            self.assertEqual(len(client.calls), 2)
        client = FakeClient([FetchError("denied", 403)])
        with self.assertRaises(FetchError):
            fetch_paper(self.root, self.cfg, client, paper("2401.00002"))
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
