from datetime import datetime
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from crypto_research import core, mcp
from crypto_research.__main__ import parser
from crypto_research.discovery import extract_links


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.dict("os.environ", {name: "" for name, *_ in core.API_SETTINGS}):
            self.run = Path(core.init("Example", root=self.root / "artifacts/runs")["run_dir"])

    def test_deep_default_quick_explicit_and_user_ceilings_preserved(self):
        deep = core.status(self.run)
        self.assertEqual(deep["profile"], "deep")
        self.assertEqual(deep["budget"]["max_requests"], 96)
        self.assertAlmostEqual((datetime.fromisoformat(deep["deadline_at"]) - datetime.fromisoformat(deep["created_at"])).total_seconds(), 3600, delta=1)
        with patch.dict("os.environ", {name: "" for name, *_ in core.API_SETTINGS}):
            quick = core.status(core.init("Quick", root=self.root, profile="quick")["run_dir"])
            limited = core.status(core.init("Limited", root=self.root, max_requests=3, minutes=2)["run_dir"])
        self.assertEqual(quick["budget"]["max_requests"], 24)
        self.assertEqual(limited["budget"]["max_requests"], 3)
        self.assertEqual(parser().parse_args(["init", "Example"]).profile, "deep")
        with self.assertRaises(core.ResearchError):
            core.init("Invalid", root=self.root, profile="unbounded")

    def test_search_saves_form_response_and_reuses_cache(self):
        body = b'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fannouncement">Official announcement</a>'
        with patch.object(core, "download", return_value=(body, "text/html", 200, "https://html.duckduckgo.com/html/")) as download:
            first = core.search(self.run, 'Example "points"')
            second = core.search(self.run, 'Example "points"')
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["untrusted_leads"], [{"url": "https://example.com/announcement", "title": "Official announcement"}])
        self.assertTrue(second["evidence"]["cache_hit"])
        self.assertEqual(core.status(self.run)["budget"]["requests_started"], 1)
        self.assertIn(b'q=Example+%22points%22', download.call_args.kwargs["payload"])
        self.assertEqual(core.evidence_body(self.run, first["evidence"]), body)
        self.assertEqual(first["evidence"]["kind"], "search")

    def test_relative_markdown_announcements_are_discovered_without_images_or_fragments(self):
        text = '[Announcement](../news.md) [same section](#weekly) ![badge](https://example.net/badge.png)'
        result = extract_links(text, "https://example.com/rewards/points.md")
        self.assertEqual(result["untrusted_leads"], [{"url": "https://example.com/news.md", "title": "Announcement"}])

    def test_captcha_is_unavailable_not_negative_evidence_and_audit_cannot_search(self):
        with patch.object(core, "download", return_value=(b'<form id="challenge-form">Verify human</form>', "text/html", 200, "https://html.duckduckgo.com/html/")) as download:
            result = mcp.dispatch(["search", str(self.run), "Example"], root=self.root)
            self.assertTrue(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"])["status"], "unavailable")
            denied = mcp.dispatch(["search", str(self.run), "Example"], root=self.root, tool_name="audit")
            self.assertTrue(denied["isError"])
        download.assert_called_once()

    def test_search_failure_is_archived_and_counts_against_budget(self):
        with patch.object(core, "download", side_effect=TimeoutError):
            result = core.search(self.run, "Example")
        self.assertEqual(result["status"], "error")
        self.assertEqual(core.status(self.run)["budget"]["requests_started"], 1)
        self.assertEqual(len(core.status(self.run)["failed_sources"]), 1)

    def test_search_snippets_cannot_be_registered_as_numeric_evidence(self):
        with patch.object(core, "download", return_value=(b'<a class="result__a" href="https://example.com/">Price 417 USD</a>', "text/html", 200, "https://html.duckduckgo.com/html/")):
            result = core.search(self.run, "Example")
        with self.assertRaisesRegex(core.ResearchError, "discovery leads"):
            core.observe(self.run, result["evidence"]["id"], "", quote="Price 417 USD", literal="417", label="Unverified price", unit="USD")

    def test_link_inspection_is_read_only_and_exposes_decisive_markdown_link(self):
        body = b'[More on the end of points](https://x.com/project/status/123)\n[Docs](https://example.com/docs)'
        with patch.object(core, "download", return_value=(body, "text/markdown", 200, "https://example.com/points")):
            record = core.fetch(self.run, "https://example.com/points", kind="official")
        before = {path.relative_to(self.run): path.read_bytes() for path in self.run.rglob("*") if path.is_file()}
        with patch.object(core, "download", side_effect=AssertionError("No network during audit")):
            result = mcp.dispatch(["inspect", str(self.run), record["id"], "--links", "--query", "points"], root=self.root, tool_name="audit")
        self.assertFalse(result["isError"])
        leads = json.loads(result["content"][0]["text"])["untrusted_leads"]
        self.assertEqual(leads[0]["url"], "https://x.com/project/status/123")
        self.assertEqual(before, {path.relative_to(self.run): path.read_bytes() for path in self.run.rglob("*") if path.is_file()})

    def test_links_are_untrusted_deduplicated_bounded_and_do_not_trigger_requests(self):
        text = ('<a href="/announcement#part">A</a><a href="/announcement">Same</a>'
                '<a href="https://example.net/b">B</a><a href="file:///etc/passwd">Bad</a>'
                '<a href="https://127.0.0.1/private">Bad</a><a href="https://localhost/a">Bad</a>'
                '<a href="https://u:p@example.net/">Credentials</a>'
                '<a href="javascript:alert(1)">Bad</a><script><a href="https://evil.example/">Ignore</a></script>')
        with patch.object(core.socket, "getaddrinfo", side_effect=AssertionError("No DNS for leads")):
            result = extract_links(text, "https://example.com/docs", limit=1)
        self.assertEqual(result["match_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["untrusted_leads"][0]["url"], "https://example.com/announcement")

    def test_search_input_bounds_before_fetch(self):
        with patch.object(core, "fetch") as fetch:
            for query, limit in (("", 1), ("x" * 501, 1), ("Example", 13), (None, 1)):
                with self.subTest(query=query, limit=limit), self.assertRaises(core.ResearchError):
                    core.search(self.run, query, limit=limit)
            fetch.assert_not_called()
