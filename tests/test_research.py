from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import json
import io
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from crypto_research import core
from crypto_research.__main__ import main, parser


class RunTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {name: "" for name, *_ in core.API_SETTINGS})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(core.init("Variational Omni", root=self.temp.name)["run_dir"])
        self.body = b'{"revenue": 0.1, "fees": 0.2, "missing": null, "true": true}'
        self.mock = patch.object(core, "download", return_value=(self.body, "application/json", 200, "https://example.com/data"))
        self.download = self.mock.start()
        self.addCleanup(self.mock.stop)

    def evidence(self, **kwargs):
        return core.fetch(self.run, "https://example.com/data", **kwargs)

    def metric(self, path="/revenue", **kwargs):
        evidence = self.evidence()
        return core.observe(self.run, evidence["id"], path, label="Revenue", unit="USD", **kwargs)

    def final_artifacts(self):
        record = self.evidence(kind="official")
        core.identity(self.run, name="Variational Omni", official_url="https://example.com/", chains=["Arbitrum"],
                      token_status="unknown", evidence_ids=[record["id"]])
        (self.run / "report.md").write_text("Memo.\n" + core.VERIFICATION_NOTICE + "\n", encoding="utf-8")
        core.save(self.run / "claims.json", [{"id": "fact1", "text": "Example fact", "category": "fact", "evidence_ids": [record["id"]], "metric_ids": []}])

        core.save(self.run / "verification.json", {"status": "not_performed", "writer_model": None, "reviewer_model": None,
                                                  "warnings": ["No independent model review"]})
        core.save(self.run / "coverage.json", {
            "answer_status": "partial", "answer_summary": "Only an example fact is supported",
            "stop_reason": "access", "stop_reason_detail": "No independent evidence in this fixture",
            "questions": [{"id": "q1", "question": "Example fact?", "priority": "critical", "status": "answered",
                           "finding": "Example fact", "evidence_ids": [record["id"]], "claim_ids": ["fact1"],
                           "attempted_evidence_ids": [record["id"]]}],
            "source_groups": [{"owner": "Example", "relationship": "primary", "independence_basis": "Self report",
                               "evidence_ids": [record["id"]]}], "leads": [],
            "valuation": {"kind": "not_applicable", "basis": "Not a valuation fixture", "input_claim_ids": []},
            "review": {"status": "not_performed", "answer_addressed": False, "assumptions_supported": False,
                       "counterevidence_checked": False, "notes": "No model review executed"}})

    def test_duplicate_evidence_ids_fail_integrity_check(self):
        self.evidence()
        records = core.load(self.run / "evidence/index.json")
        core.save(self.run / "evidence/index.json", records + records)
        result = core.check(self.run)
        self.assertFalse(result["ok"])
        self.assertIn("Duplicate evidence IDs are not allowed", result["errors"])

    def test_cache_preserves_retrieval_time_and_budget(self):
        first, second = self.evidence(), self.evidence()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["retrieved_at"], second["retrieved_at"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(core.status(self.run)["budget"]["requests_started"], 1)
        self.download.assert_called_once()

    def test_cache_separates_api_keys_without_persisting_them(self):
        first = self.evidence(headers={"x-api-key": "first-secret"})
        second = self.evidence(headers={"x-api-key": "second-secret"})
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotIn("first-secret", (self.run / "evidence/index.json").read_text())

    def test_cache_preserves_source_kind(self):
        aggregator = self.evidence(kind="aggregator")
        official = self.evidence(kind="official")
        self.assertNotEqual(aggregator["id"], official["id"])
        self.assertEqual(official["kind"], "official")

    def test_failed_requests_consume_budget_and_are_not_zero_metrics(self):
        self.download.side_effect = HTTPError("https://example.com/data", 429, "rate limit", {}, None)
        failed = self.evidence()
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["http_status"], 429)
        with self.assertRaises(core.ResearchError):
            core.observe(self.run, failed["id"], "/revenue", label="Revenue", unit="USD")
        self.assertEqual(core.status(self.run)["budget"]["requests_started"], 1)

    def test_errors_do_not_persist_url_secrets(self):
        self.download.side_effect = URLError("https://example.com/?api_key=SUPERSECRET")
        core.fetch(self.run, "https://example.com/?api_key=SUPERSECRET")
        self.assertNotIn("SUPERSECRET", (self.run / "evidence/index.json").read_text())

    def test_parallel_budget_reservations(self):
        manifest = core.load(self.run / "run.json")
        manifest["budget"]["max_requests"] = 2
        core.save(self.run / "run.json", manifest)

        def attempt(index):
            try:
                return core.fetch(self.run, "https://example.com/data?i=" + str(index))["status"]
            except core.ResearchError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(results.count("ok"), 2)
        self.assertEqual(results.count("blocked"), 6)
        self.assertEqual(core.status(self.run)["evidence_count"], 2)

    def test_deadline_blocks_new_fetch_but_not_calculation(self):
        metric = self.metric()
        manifest = core.load(self.run / "run.json")
        manifest["deadline_at"] = (core.now() - timedelta(seconds=1)).isoformat()
        core.save(self.run / "run.json", manifest)
        with self.assertRaises(core.ResearchError):
            core.fetch(self.run, "https://example.com/new")
        result = core.calculate(self.run, "x * 2", {"x": metric["id"]}, label="Double", unit="USD")
        self.assertEqual(result["value"], "0.2")

    def test_decimal_observation_and_reproducible_calculation(self):
        revenue, fees = self.metric(as_of="2026-10-14"), self.metric("/fees", as_of="2026-10-14")
        result = core.calculate(self.run, "r + f", {"r": revenue["id"], "f": fees["id"]}, label="Sum", unit="USD")
        self.assertEqual(result["value"], "0.3")
        self.assertTrue(core.check(self.run)["ok"])

    def test_missing_boolean_and_non_numeric_observations_rejected(self):
        for path in ("/missing", "/true", "/absent"):
            with self.subTest(path=path), self.assertRaises(core.ResearchError):
                self.metric(path)

    def test_unknown_period_warns(self):
        metric = self.metric()
        self.assertTrue(any(metric["id"] in warning for warning in core.check(self.run)["warnings"]))

    def test_text_observation_requires_exact_saved_quote_and_number(self):
        self.download.return_value = (b"Team allocation [50%], locked for 12 months.", "text/markdown", 200, "https://example.com/data")
        record = self.evidence(kind="official")
        metric = core.observe(self.run, record["id"], "", label="Announced allocation", unit="percent",
                              quote="Team allocation [50%]", literal="50", as_of="announcement, launch unconfirmed")
        self.assertEqual(metric["value"], "50")
        self.assertTrue(core.check(self.run)["ok"])
        for quote, literal in (("Invented [99%]", "99"), ("Team allocation [50%]", "5"), ("Team allocation [50%]", "49")):
            with self.subTest(quote=quote, literal=literal), self.assertRaises(core.ResearchError):
                core.observe(self.run, record["id"], "", label="Invalid", unit="percent", quote=quote, literal=literal)

    def test_text_observation_reproduction_detects_altered_quote(self):
        self.download.return_value = (b"Team allocation [50%]", "text/markdown", 200, "https://example.com/data")
        record = self.evidence()
        core.observe(self.run, record["id"], "", label="Allocation", unit="percent", quote="Team allocation [50%]", literal="50")
        metrics = core.load(self.run / "metrics.json")
        metrics[0]["quote"] = "Changed [50%]"
        core.save(self.run / "metrics.json", metrics)
        self.assertFalse(core.check(self.run)["ok"])

    def test_text_numeric_literals_preserve_sign_and_exponent(self):
        record = {"kind": "official"}
        for body, literal in ((b"Loss: -50%", "50"), (b"Supply: 1e3", "1")):
            with self.subTest(body=body), self.assertRaises(core.ResearchError):
                core.observation_value(record, body, "", quote=body.decode(), literal=literal)
        self.assertEqual(core.observation_value(record, b"Loss: -50%", "", quote="Loss: -50%", literal="-50"), Decimal("-50"))

    def test_grouped_and_word_numbers_reproduce_from_exact_quotes(self):
        body = b"Retro: 3,000,000 points. Vesting: minimum of three years."
        self.download.return_value = (body, "text/markdown", 200, "https://example.com/data")
        record = self.evidence(kind="official")
        for quote, literal, expected in (("Retro: 3,000,000 points.", "3,000,000", "3000000"),
                                         ("minimum of three years", "three", "3")):
            metric = core.observe(self.run, record["id"], "", label="Exact extraction", unit="source_units",
                                  quote=quote, literal=literal, as_of="announcement")
            self.assertEqual(metric["value"], expected)
            self.assertEqual(metric["value_literal"], literal)
        self.assertTrue(core.check(self.run)["ok"])

    def test_number_boundaries_reject_partial_grouped_or_compound_literals(self):
        cases = (("3,000,000", "3"), ("3,000,000", "000"), ("3,00", "3,00"),
                 ("1,,000", "1"), ("1234,000", "1234,000"), ("-3,000", "3,000"),
                 ("one hundred", "one"), ("twenty-three", "three"), ("one point five", "one"),
                 ("three years", "3"), ("minus three", "three"), ("positive three", "three"),
                 ("1e3", "1"), ("1.2.3", "2"))
        for quote, literal in cases:
            with self.subTest(quote=quote, literal=literal), self.assertRaises(core.ResearchError):
                core.observation_value({}, quote.encode(), "", quote=quote, literal=literal)

    def test_grouped_numbers_keep_decimal_sign_exponent_and_delimiters(self):
        for quote, literal, expected in (("-3,000.5e+1", "-3,000.5e+1", "-30005"),
                                         ("32, 18 and 50", "32", "32"), ("$1M", "1", "1"),
                                         ("Three years", "Three", "3"), ("+0.5%", "+0.5", "0.5")):
            with self.subTest(quote=quote):
                self.assertEqual(core.observation_value({}, quote.encode(), "", quote=quote, literal=literal), Decimal(expected))

    def test_failed_inspection_and_status_show_only_safe_metadata(self):
        self.download.side_effect = URLError("https://example.com/?api_key=SUPERSECRET")
        record = core.fetch(self.run, "https://example.com/?api_key=SUPERSECRET")
        with patch.object(core, "evidence_body", side_effect=AssertionError("No failed body reads")):
            result = core.inspect_source(self.run, record["id"])
            status = core.status(self.run)
        self.assertFalse(result["body_inspected"])
        self.assertEqual(status["failed_sources"], [record["id"]])
        self.assertEqual(status["failed_source_details"], [result])
        self.assertNotIn("SUPERSECRET", json.dumps(status))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["inspect", str(self.run), record["id"]]), 0)
        self.assertNotIn("SUPERSECRET", stdout.getvalue())
        with self.assertRaises(core.ResearchError):
            core.inspect_source(self.run, record["id"], "/result")
        with self.assertRaises(core.ResearchError):
            core.observe(self.run, record["id"], "", label="No failed observations", unit="USD", quote="0", literal="0")

    def test_text_search_is_local_bounded_and_literal(self):
        self.download.return_value = (("start " + "x" * 9000 + "needle.* " * 20).encode(), "text/html", 200, "https://example.com/data")
        record = self.evidence()
        budget = core.status(self.run)["budget"].copy()
        result = core.inspect_source(self.run, record["id"], query="needle.*", limit=2)
        self.assertEqual(result["match_count"], 20)
        self.assertEqual(len(result["untrusted_matches"]), 2)
        self.assertTrue(result["truncated"])
        self.assertLess(len(json.dumps(result)), 2000)
        self.assertEqual(core.status(self.run)["budget"], budget)
        self.download.assert_called_once()

    def test_tampered_raw_evidence_fails_check(self):
        record = self.evidence()
        (self.run / record["body_path"]).write_bytes(b"{}")
        self.assertFalse(core.check(self.run)["ok"])

    def test_tampered_metric_fails_reproduction(self):
        self.metric()
        metrics = core.load(self.run / "metrics.json")
        metrics[0]["value"] = "999"
        core.save(self.run / "metrics.json", metrics)
        self.assertFalse(core.check(self.run)["ok"])

    def test_calculation_must_use_saved_metrics(self):
        with self.assertRaises(core.ResearchError):
            core.calculate(self.run, "r * 100", {"r": "invented"}, label="Invented", unit="USD")

    def test_identity_requires_official_same_host_evidence(self):
        record = self.evidence(kind="aggregator")
        with self.assertRaises(core.ResearchError):
            core.identity(self.run, name="Test", official_url="https://example.com/", chains=[], token_status="unknown", evidence_ids=[record["id"]])
        record = self.evidence(kind="official", ttl=0)
        with self.assertRaises(core.ResearchError):
            core.identity(self.run, name="Test", official_url="https://unrelated.com/", chains=[], token_status="unknown", evidence_ids=[record["id"]])

    def test_final_requires_identity_report_claims_and_verification(self):
        self.assertFalse(core.check(self.run, final=True)["ok"])
        self.final_artifacts()
        self.assertTrue(core.check(self.run, final=True)["ok"])

    def test_uncited_fact_fails_final(self):
        self.final_artifacts()
        claims = core.load(self.run / "claims.json")
        claims[0]["evidence_ids"] = []
        core.save(self.run / "claims.json", claims)
        self.assertFalse(core.check(self.run, final=True)["ok"])

    def test_unknown_claim_source_fails_final(self):
        self.final_artifacts()
        claims = core.load(self.run / "claims.json")
        claims[0]["evidence_ids"] = ["invented"]
        core.save(self.run / "claims.json", claims)
        self.assertFalse(core.check(self.run, final=True)["ok"])

    def test_verification_limitation_cannot_be_silent(self):
        self.final_artifacts()
        core.save(self.run / "verification.json", {"status": "not_performed", "warnings": []})
        self.assertFalse(core.check(self.run, final=True)["ok"])

    def test_same_model_cannot_claim_independence(self):
        self.final_artifacts()
        core.save(self.run / "verification.json", {"status": "independent", "writer_model": "model-a", "reviewer_model": "model-a"})
        self.assertFalse(core.check(self.run, final=True)["ok"])

    def test_rpc_rejects_write_methods_and_full_blocks(self):
        for method, params in (("eth_sendRawTransaction", ["0x0"]), ("eth_getBlockByNumber", ["latest", True])):
            with self.subTest(method=method), self.assertRaises(core.ResearchError):
                core.rpc(self.run, method, params)

    def test_rpc_numeric_hex_observation_and_endpoint_redaction(self):
        self.download.return_value = (b'{"jsonrpc":"2.0","id":1,"result":"0xa4b1"}', "application/json", 200, "https://rpc.example.com/SECRET")
        with patch.dict("os.environ", {"CR_RPC_URL": "https://rpc.example.com/SECRET"}):
            record = core.rpc(self.run, "eth_chainId", [])
        self.assertNotIn("SECRET", json.dumps(record))
        metric = core.observe(self.run, record["id"], "/result", label="Chain ID", unit="chain_id")
        self.assertEqual(metric["value"], "42161")
        self.assertTrue(core.check(self.run)["ok"])

    def test_rpc_error_is_not_successful_evidence(self):
        self.download.return_value = (b'{"jsonrpc":"2.0","error":{"code":-1}}', "application/json", 200, "https://rpc.example.com/")
        with patch.dict("os.environ", {"CR_RPC_URL": "https://rpc.example.com/"}):
            self.assertEqual(core.rpc(self.run, "eth_chainId", [])["status"], "error")

    def test_malformed_rpc_json_is_error_and_not_cached(self):
        self.download.return_value = (b"not JSON", "application/json", 200, "https://rpc.example.com/")
        with patch.dict("os.environ", {"CR_RPC_URL": "https://rpc.example.com/"}):
            first = core.rpc(self.run, "eth_chainId", [])
            second = core.rpc(self.run, "eth_chainId", [])
        self.assertEqual(first["status"], "error")
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue((self.run / first["body_path"]).is_file())

    def test_embedded_url_credentials_never_persist(self):
        self.download.side_effect = core.ResearchError("Credentials rejected")
        for kind in ("other", "rpc"):
            core.fetch(self.run, "https://user:SUPERSECRET@example.com/data", kind=kind)
        self.assertNotIn("SUPERSECRET", (self.run / "evidence/index.json").read_text())

    def test_redirects_consume_budget_and_check_deadline(self):
        from urllib.request import Request
        manifest = core.load(self.run / "run.json")
        manifest["budget"]["max_requests"] = 1
        core.save(self.run / "run.json", manifest)
        core.reserve_request(self.run)
        handler = core.PublicRedirects(lambda: core.reserve_request(self.run))
        with patch.object(core, "public_url", side_effect=lambda url: url):
            with self.assertRaises(core.ResearchError):
                handler.redirect_request(Request("https://example.com/"), None, 302, "Found", {}, "https://example.com/next")
            manifest["budget"]["max_requests"] = 5
            manifest["deadline_at"] = (core.now() - timedelta(seconds=1)).isoformat()
            core.save(self.run / "run.json", manifest)
            with self.assertRaises(core.ResearchError):
                handler.redirect_request(Request("https://example.com/"), None, 302, "Found", {}, "https://example.com/next")

    def test_saved_failed_rpc_response_hash_is_checked(self):
        self.download.return_value = (b'{"jsonrpc":"2.0","error":{"code":-1}}', "application/json", 200, "https://rpc.example.com/")
        with patch.dict("os.environ", {"CR_RPC_URL": "https://rpc.example.com/"}):
            record = core.rpc(self.run, "eth_chainId", [])
        (self.run / record["body_path"]).write_bytes(b"changed")
        self.assertFalse(core.check(self.run)["ok"])

    def test_final_revalidates_identity_provenance(self):
        self.final_artifacts()
        manifest = core.load(self.run / "run.json")
        manifest["identity"] = {"status": "resolved"}
        core.save(self.run / "run.json", manifest)
        self.assertFalse(core.check(self.run, final=True)["ok"])

    def test_observation_requires_single_source_and_matching_time(self):
        self.metric()
        second = core.fetch(self.run, "https://example.com/second")
        metrics = core.load(self.run / "metrics.json")
        metrics[0]["evidence_ids"].append(second["id"])
        metrics[0]["retrieved_at"] = "2099-01-01T00:00:00+00:00"
        core.save(self.run / "metrics.json", metrics)
        self.assertFalse(core.check(self.run)["ok"])
        metrics[0]["evidence_ids"].pop()
        core.save(self.run / "metrics.json", metrics)
        self.assertFalse(core.check(self.run)["ok"])

    def test_report_cannot_omit_verification_notice(self):
        self.final_artifacts()
        (self.run / "report.md").write_text("Overconfident report.", encoding="utf-8")
        self.assertFalse(core.check(self.run, final=True)["ok"])

    def test_evidence_path_cannot_escape_run(self):
        record = self.evidence()
        record["body_path"] = "../../elsewhere"
        with self.assertRaises(core.ResearchError):
            core.evidence_body(self.run, record)

    def test_inspection_preserves_decimal_and_directory_pointers(self):
        record = self.evidence()
        result = core.inspect_source(self.run, record["id"])
        self.assertEqual(result["untrusted_preview"]["revenue"], "0.1")
        self.download.return_value = (b'[{"name":"Other"},{"name":"Variational","slug":"variational"}]', "application/json", 200, "https://example.com/data")
        record = self.evidence(ttl=0)
        result = core.inspect_source(self.run, record["id"], query="variational")
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["untrusted_items"][0]["pointer"], "/1")


class DownloadTests(unittest.TestCase):
    def response(self, chunks):
        response = MagicMock()
        response.read1.side_effect = chunks
        response.headers = {"Content-Type": "text/plain"}
        response.status = 200
        response.geturl.return_value = "https://example.com/data"
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        return response, opener

    def test_download_reads_bounded_chunks_without_network(self):
        response, opener = self.response([b"one", b"two", b""])
        with patch.object(core, "public_url"), patch.object(core, "build_opener", return_value=opener):
            self.assertEqual(core.download("https://example.com/data")[0], b"onetwo")
        response.read.assert_not_called()
        self.assertTrue(all(call.args[0] <= 64 * 1024 for call in response.read1.call_args_list))

    def test_slow_stream_times_out_between_reads_and_closes_response(self):
        response, opener = self.response([b"one", b"two"])
        with patch.object(core, "public_url"), patch.object(core, "build_opener", return_value=opener), \
                patch.object(core, "monotonic", side_effect=[0, 0, 0, core.DOWNLOAD_TIMEOUT + 1]):
            with self.assertRaisesRegex(core.ResearchError, "timed out"):
                core.download("https://example.com/data")
        response.read1.assert_called_once()
        opener.open.return_value.__exit__.assert_called_once()

    def test_response_size_limit_still_applies(self):
        _, opener = self.response([b"12345"])
        with patch.object(core, "public_url"), patch.object(core, "build_opener", return_value=opener), \
                patch.object(core, "MAX_BODY", 4):
            with self.assertRaisesRegex(core.ResearchError, "exceeds"):
                core.download("https://example.com/data")


class NetworkSafetyTests(unittest.TestCase):
    def address(self, host="93.184.216.34"):
        return (core.socket.AF_INET, core.socket.SOCK_STREAM, core.socket.IPPROTO_TCP, "", (host, 443))

    def test_rebinding_to_private_address_is_rejected_at_connect_time(self):
        with patch.object(core.socket, "getaddrinfo", side_effect=[
                [self.address()], [self.address("127.0.0.1")]]), patch.object(core.socket, "socket") as create:
            core.public_url("https://example.com/data")
            with self.assertRaisesRegex(core.ResearchError, "Private"):
                core.connect_public(("example.com", 443), 2)
        create.assert_not_called()

    def test_connection_uses_validated_sockaddr_without_another_dns_lookup(self):
        address = self.address()
        connection = MagicMock()
        with patch.object(core.socket, "getaddrinfo", return_value=[address]) as dns, \
                patch.object(core.socket, "socket", return_value=connection), \
                patch.object(core.socket, "create_connection") as unsafe:
            self.assertIs(core.connect_public(("example.com", 443), 2), connection)
        dns.assert_called_once_with("example.com", 443, type=core.socket.SOCK_STREAM)
        connection.connect.assert_called_once_with(address[4])
        unsafe.assert_not_called()

    def test_mixed_public_private_dns_answers_are_rejected_before_any_socket(self):
        with patch.object(core.socket, "getaddrinfo", return_value=[self.address(), self.address("10.0.0.1")]), \
                patch.object(core.socket, "socket") as create:
            with self.assertRaises(core.ResearchError):
                core.connect_public(("example.com", 443), 2)
        create.assert_not_called()

    def test_connection_failure_closes_socket_before_fallback(self):
        first, second = MagicMock(), MagicMock()
        first.connect.side_effect = OSError("connect failed")
        with patch.object(core.socket, "getaddrinfo", return_value=[self.address(), self.address("1.1.1.1")]), \
                patch.object(core.socket, "socket", side_effect=[first, second]):
            self.assertIs(core.connect_public(("example.com", 443), 2), second)
        first.close.assert_called_once()
        second.close.assert_not_called()

    def test_socket_creation_failure_still_allows_public_fallback(self):
        connection = MagicMock()
        with patch.object(core.socket, "getaddrinfo", return_value=[self.address(), self.address("1.1.1.1")]), \
                patch.object(core.socket, "socket", side_effect=[OSError("no socket"), connection]):
            self.assertIs(core.connect_public(("example.com", 443), 2), connection)

    def test_tls_keeps_original_hostname_and_certificate_verification(self):
        import ssl

        context = MagicMock()
        raw = MagicMock()
        with patch.object(core, "connect_public", return_value=raw) as connect:
            connection = core.PublicHTTPSConnection("example.com", timeout=2, context=context)
            connection.connect()
        connect.assert_called_once_with(("example.com", 443), 2, None)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")
        default = core.PublicHTTPSConnection("example.com", timeout=2)
        self.assertTrue(default._context.check_hostname)
        self.assertEqual(default._context.verify_mode, ssl.CERT_REQUIRED)

    def test_download_disables_implicit_proxies_and_uses_public_connection_handler(self):
        response = MagicMock()
        response.read1.return_value = b""
        response.headers.get.return_value = "text/plain"
        response.status = 200
        response.geturl.return_value = "https://example.com/data"
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(core, "public_url"), patch.object(core, "build_opener", return_value=opener) as build:
            core.download("https://example.com/data")
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(handler, core.ProxyHandler) and handler.proxies == {} for handler in handlers))
        self.assertTrue(any(isinstance(handler, core.PublicHTTPSHandler) for handler in handlers))


class PreflightTests(unittest.TestCase):
    def settings(self, result):
        return {item["environment"]: item for item in result["settings"]}

    def test_no_keys_needed_and_no_network_dns_or_file_reads(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(core, "download") as download, \
                patch.object(core.socket, "getaddrinfo") as dns, \
                patch.object(core.Path, "read_text") as read_text, \
                patch.object(core.Path, "read_bytes") as read_bytes, \
                patch.object(core, "save") as save:
            result = core.preflight()
        for operation in (download, dns, read_text, read_bytes, save):
            operation.assert_not_called()
        self.assertFalse(result["network_checked"])
        settings = self.settings(result)
        self.assertTrue(all(not item["configured"] for item in settings.values()))
        self.assertEqual(settings["COINGECKO_DEMO_API_KEY"]["status"], "keyless")
        self.assertTrue(settings["COINGECKO_DEMO_API_KEY"]["usable_by_local_adapter"])
        self.assertEqual(settings["CR_RPC_URL"]["status"], "missing")
        self.assertFalse(settings["CR_RPC_URL"]["usable_by_local_adapter"])
        self.assertIn("DefiLlama", result["public_sources"])

    def test_every_template_setting_is_checked_without_secret_values(self):
        values = {name: "canary-secret-" + str(index) for index, (name, *_) in enumerate(core.API_SETTINGS)}
        values["CR_RPC_URL"] = "https://rpc.example.com/canary-path?api_key=canary-query"
        values["UNRELATED_PRIVATE_TOKEN"] = "unrelated-private-canary"
        with patch.dict("os.environ", values, clear=True):
            result = core.preflight()
        encoded = json.dumps(result)
        for value in values.values():
            self.assertNotIn(value, encoded)
        for fragment in ("rpc.example.com", "canary-path", "canary-query", "UNRELATED_PRIVATE_TOKEN"):
            self.assertNotIn(fragment, encoded)
        settings = self.settings(result)
        self.assertEqual(set(settings), {name for name, *_ in core.API_SETTINGS})
        self.assertTrue(all(item["configured"] for item in settings.values()))
        for name, _, _, supported in core.API_SETTINGS:
            with self.subTest(name=name):
                self.assertEqual(settings[name]["usable_by_local_adapter"], supported)
                self.assertEqual(settings[name]["status"], "configured" if supported else "not_integrated")

    def test_blank_values_are_missing_and_invalid_values_are_safe(self):
        values = {"COINGECKO_DEMO_API_KEY": " \t\n", "CR_RPC_URL": " ",
                  "EXA_API_KEY": "canary-first\ncanary-second"}
        with patch.dict("os.environ", values, clear=True):
            result = core.preflight()
            self.assertEqual(core.coingecko_headers(), {})
        settings = self.settings(result)
        self.assertEqual(settings["COINGECKO_DEMO_API_KEY"]["value_status"], "missing")
        self.assertEqual(settings["CR_RPC_URL"]["value_status"], "missing")
        self.assertEqual(settings["EXA_API_KEY"]["value_status"], "invalid")
        self.assertNotIn("canary-first", json.dumps(result))
        self.assertNotIn("canary-second", json.dumps(result))

    def test_invalid_rpc_configuration_is_detected_without_dns(self):
        urls = ("http://example.com/canary", "https://user:canary@example.com", "https://example.com/#canary",
                "https://example.com:8080/canary", "https://example.com:99999/canary", "https://[broken/canary",
                "https://localhost/canary", "https://node.localhost/canary", "https://127.0.0.1/canary",
                "https://10.0.0.1/canary", "https://[::1]/canary", "https://example.com/canary\nheader")
        for url in urls:
            with self.subTest(url=url), patch.dict("os.environ", {"CR_RPC_URL": url}, clear=True), \
                    patch.object(core.socket, "getaddrinfo") as dns:
                result = core.preflight()
                setting = self.settings(result)["CR_RPC_URL"]
                self.assertEqual(setting["status"], "invalid")
                self.assertFalse(setting["usable_by_local_adapter"])
                self.assertNotIn("canary", json.dumps(result))
                dns.assert_not_called()

    def test_valid_rpc_configuration_still_does_not_verify_network_or_chain(self):
        with patch.dict("os.environ", {"CR_RPC_URL": "https://rpc.example.com:443/canary"}, clear=True), \
                patch.object(core.socket, "getaddrinfo") as dns:
            result = core.preflight()
        dns.assert_not_called()
        self.assertEqual(self.settings(result)["CR_RPC_URL"]["status"], "configured")
        self.assertFalse(result["network_checked"])
        self.assertNotIn("chain_id", json.dumps(result))

    def test_runtime_helpers_reject_invalid_settings_without_requests(self):
        with patch.dict("os.environ", {"COINGECKO_DEMO_API_KEY": "canary\nheader",
                                       "CR_RPC_URL": "https://user:canary@example.com"}, clear=True), \
                patch.object(core, "fetch") as fetch:
            with self.assertRaises(core.ResearchError) as key_error:
                core.coingecko_headers()
            with self.assertRaises(core.ResearchError) as rpc_error:
                core.rpc("unused", "eth_chainId", [])
        fetch.assert_not_called()
        self.assertNotIn("canary", str(key_error.exception) + str(rpc_error.exception))

    def test_init_saves_only_safe_snapshot_and_status_does_not_rescan(self):
        values = {"COINGECKO_DEMO_API_KEY": "demo-canary", "DUNE_API_KEY": "dune-canary",
                  "CR_RPC_URL": "https://rpc.example.com/canary"}
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", values, clear=True), \
                patch.object(core, "download") as download, patch.object(core.socket, "getaddrinfo") as dns:
            result = core.init("Example", root=directory)
            run = Path(result["run_dir"])
            manifest = core.load(run / "run.json")
            self.assertEqual(manifest["preflight"], result["preflight"])
            self.assertEqual(manifest["budget"]["requests_started"], 0)
            for path in run.rglob("*"):
                if path.is_file():
                    self.assertNotIn("canary", path.read_text(encoding="utf-8"))
            with patch.dict("os.environ", {}, clear=True), \
                    patch.object(core, "preflight", side_effect=AssertionError("No live scans during status")):
                self.assertEqual(core.status(run)["preflight"], manifest["preflight"])
        download.assert_not_called()
        dns.assert_not_called()

    def test_cli_preflight_requires_no_run_and_returns_redacted_json(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"CONTEXT7_API_KEY": "context-canary"}, clear=True), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["preflight"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("context-canary", stdout.getvalue())
        settings = self.settings(json.loads(stdout.getvalue()))
        self.assertEqual(settings["CONTEXT7_API_KEY"]["status"], "not_integrated")


class UtilityTests(unittest.TestCase):
    def test_json_array_pointer_rejects_negative_or_noncanonical_indices(self):
        for index in ("-1", "01", "+1", "-", " 1"):
            with self.subTest(index=index), self.assertRaises(core.ResearchError):
                core.pointer([10, 20], "/" + index)
        self.assertEqual(core.pointer([10, 20], "/0"), 10)
        self.assertEqual(core.pointer({"01": 20}, "/01"), 20)

    def test_formula_is_not_arbitrary_python(self):
        for formula in ("__import__('os').system('true')", "x.__class__", "x ** 2", "[x][0]", "x / 0"):
            with self.subTest(formula=formula), self.assertRaises(core.ResearchError):
                core.evaluate(formula, {"x": "2"})

    def test_decimal_constants_are_not_float_rounded(self):
        self.assertEqual(core.evaluate("x + 0.2", {"x": "0.1"}), "0.3")

    def test_unused_formula_inputs_rejected(self):
        with self.assertRaises(core.ResearchError):
            core.evaluate("x + 1", {"x": "1", "hidden": "10"})

    def test_nonfinite_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e10000", None, True):
            with self.subTest(value=value), self.assertRaises(core.ResearchError):
                core.number(value)

    def test_json_pointer_escaped_keys(self):
        self.assertEqual(core.pointer({"a/b": {"c~d": [Decimal("0.1")]}}, "/a~1b/c~0d/0"), Decimal("0.1"))

    def test_private_destinations_and_insecure_urls_rejected(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com:8080", "https://example.com/#x"):
            with self.subTest(url=url), self.assertRaises(core.ResearchError):
                core.public_url(url)
        with patch.object(core.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(core.ResearchError):
                core.public_url("https://example.com")

    def test_url_redaction(self):
        self.assertNotIn("SECRET", core.safe_url("https://example.com/?api_key=SECRET&query=variational"))
        self.assertIn("variational", core.safe_url("https://example.com/?api_key=SECRET&query=variational"))

    def test_parser_and_unknown_run_exit(self):
        args = parser().parse_args(["init", "Variational Omni", "--mode", "both"])
        self.assertEqual(args.mode, "both")
        with patch("sys.stderr"):
            self.assertEqual(main(["status", "/nonexistent/research-run"]), 1)


if __name__ == "__main__":
    unittest.main()
