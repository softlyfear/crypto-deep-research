from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
import json
import io
import hashlib
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from crypto_research import core, mcp
from crypto_research.publication import project_slug, readable_date


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict("os.environ", {name: "" for name, *_ in core.API_SETTINGS})
        environment.start()
        self.addCleanup(environment.stop)
        self.run = Path(core.init("Varitional Omni; user has 417 points", root=self.root / "artifacts/runs")["run_dir"])
        with patch.object(core, "download", return_value=(b'{"allocation":32}', "application/json", 200, "https://example.com/data")):
            self.record = core.fetch(self.run, "https://example.com/data", kind="official", label="Official allocation")
        core.identity(self.run, name="Variational Omni", official_url="https://example.com/", chains=["Arbitrum"],
                      token_status="unknown", evidence_ids=[self.record["id"]])
        metric = core.observe(self.run, self.record["id"], "/allocation", label="Announced allocation",
                              unit="percent", as_of="announced future TGE")
        self.metric = metric["id"]
        self.report = ("# Variational Omni: 417 points\n\nOriginal query: Varitional Omni; 417 points\n\n"
                       "## Identity\n\nArbitrum. Live token unconfirmed. [claim-001]\n\n"
                       "## Краткое заключение\n\n**Денежная стоимость неизвестна.** 417 is an assumption. [claim-001]\n\n"
                       "## Scenarios\n\n| Assumption | Value | Metric ID |\n| --- | --- | --- |\n"
                       "| Future allocation | 32% | " + self.metric + " |\n\n"
                       "```text\n## This is code, not a heading\n417 < P; exact code is preserved\n```\n\n"
                       "## Проверка\n\n" + core.VERIFICATION_NOTICE + "\nРевью не выполнено.\n\n"
                       "## Источники\n\n" + self.record["id"] + ": https://example.com/data ; period is an announcement\n")
        (self.run / "report.md").write_text(self.report, encoding="utf-8")
        core.save(self.run / "claims.json", [{"id": "claim-001", "text": "An announced allocation, not live supply",
                  "category": "fact", "evidence_ids": [self.record["id"]], "metric_ids": [self.metric]}])
        core.save(self.run / "verification.json", {"status": "not_performed", "writer_model": None, "reviewer_model": None,
                                                   "warnings": ["No confirmed independent review"]})
        core.save(self.run / "coverage.json", {
            "answer_status": "partial", "answer_summary": "An allocation is known; a monetary value is not",
            "stop_reason": "access", "stop_reason_detail": "No supported final denominator or price",
            "questions": [{"id": "q1", "question": "Allocation?", "priority": "critical", "status": "answered",
                           "finding": "An announcement, not an issued token", "evidence_ids": [self.record["id"]],
                           "claim_ids": ["claim-001"], "attempted_evidence_ids": [self.record["id"]]}],
            "source_groups": [{"owner": "Project", "relationship": "primary", "independence_basis": "Official announcement",
                               "evidence_ids": [self.record["id"]]}], "leads": [],
            "valuation": {"kind": "unavailable", "basis": "No final denominator or price", "input_claim_ids": []},
            "review": {"status": "not_performed", "answer_addressed": False, "assumptions_supported": False,
                       "counterevidence_checked": False, "notes": "No executed review"}})

    def publish(self):
        return core.publish(self.run, project_root=self.root)

    def archive(self):
        return {path.relative_to(self.run): path.read_bytes() for path in self.run.rglob("*") if path.is_file()}

    def test_publish_keeps_full_research_values_sources_and_warnings(self):
        before = self.archive()
        with patch.object(core, "download", side_effect=AssertionError("No collection")), \
                patch.object(core, "preflight", side_effect=AssertionError("No environment scan")):
            result = self.publish()
        self.assertTrue(result["ok"])
        path = Path(result["report_path"])
        self.assertEqual(path.parent, self.root / "reports")
        self.assertRegex(path.name, r"^variational-omni-\d{4}-\d{2}-\d{2}\.md$")
        text = path.read_text(encoding="utf-8")
        for fragment in ("417", "32%", "Денежная стоимость неизвестна", "Live token unconfirmed",
                          "announced future TGE", "https://example.com/data", "Ревью не выполнено",
                         "Структурная проверка", "Время получения не равно", "## Содержание"):
            self.assertIn(fragment, text)
        self.assertIn("417 < P; exact code is preserved", text)
        self.assertNotIn(self.metric, text)
        self.assertNotIn(self.record["id"], text)
        self.assertNotIn("[claim-001]", text)
        self.assertLess(text.index("## Краткое заключение"), text.index("## Содержание"))
        self.assertEqual(before, self.archive())
        self.assertEqual(list((self.root / "reports").iterdir()), [path])

    def test_publish_is_idempotent_and_does_not_create_duplicate_files(self):
        first, second = self.publish(), self.publish()
        self.assertEqual(first["report_path"], second["report_path"])
        self.assertTrue(second["already_published"])
        self.assertEqual(len(list((self.root / "reports").iterdir())), 1)

    def test_export_has_no_generated_html_or_internal_source_anchors(self):
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        for fragment in ("<a ", "</a>", "<details>", "<summary>", "</details>", "#source-", "#section-"):
            self.assertNotIn(fragment, text)
        self.assertNotRegex(text, r"\[\d+\]\(")
        self.assertIn("**[Official allocation](https://example.com/data)**", text)
        self.assertIn("### Проверка публикации", text)

    def test_duplicate_metric_citations_and_lineage_lists_move_to_one_final_source(self):
        paragraph = ("Все значения округлены. Доли: " + self.metric + ", " + self.metric +
                     " (`417/P × 100`). Требования: " + self.metric + "; " + self.metric +
                     " (`target × P / 417`). Историческая база не текущий P. [claim-001]\n")
        (self.run / "report.md").write_text(self.report + "\n## Тест сценария\n\n" + paragraph, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        section = text.split("## Тест сценария", 1)[1].split("## Источники", 1)[0]
        self.assertNotIn("https://example.com/data", section)
        self.assertEqual(text.count("**[Official allocation](https://example.com/data)**"), 1)
        self.assertIn("`417/P × 100`", section)
        self.assertIn("`target × P / 417`", section)
        self.assertIn("Историческая база не текущий P", section)
        self.assertNotIn("[1], [1]", section)
        self.assertNotIn(self.metric, section)

    def test_paragraphs_and_table_rows_keep_values_but_not_inline_citations(self):
        repeated = "Условие сохраняется. [claim-001] [claim-001]"
        (self.run / "report.md").write_text(self.report + "\n## Условия\n\n" + repeated + "\n\n" + repeated, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        self.assertEqual(text.count("Условие сохраняется."), 2)
        self.assertNotRegex(text, r"\[\d+\]\(")
        self.assertIn("| Future allocation | 32% |", text)
        self.assertNotIn("Metric ID", text)
        self.assertEqual(text.count("**[Official allocation](https://example.com/data)**"), 1)

    def test_legacy_prose_reference_roles_do_not_turn_into_empty_sentences(self):
        paragraph = ("Сумма корзин 100% — " + self.metric + ".\n\n"
                     "Источник масштаба — " + self.metric + ", историческое событие, не текущий P. [claim-001]\n\n"
                     "Boost от 0% до 5%, " + self.metric + " и " + self.metric + "; персональная база. [claim-001]\n")
        (self.run / "report.md").write_text(self.report + "\n## Ссылки\n\n" + paragraph, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        for broken in ("— .", "— ,", "источник .", ", и ;"):
            self.assertNotIn(broken, text)
        self.assertIn("Сумма корзин 100%.", text)
        self.assertIn("историческое событие, не текущий P", text)
        self.assertIn("Boost от 0% до 5%", text)

    def test_dates_are_short_utc_and_unknown_period_is_not_repeated_per_source(self):
        self.assertEqual(readable_date("2026-10-14T21:15:21.064171+00:00"), "14.10.2026, 21:15 UTC")
        self.assertEqual(readable_date("2026-10-14T23:15:21.123456+02:00", seconds=True), "14.10.2026, 21:15:21 UTC")
        self.assertEqual(readable_date("2026-09-11"), "11.09.2026")
        self.assertEqual(readable_date("announced future TGE"), "announced future TGE")
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("Период наблюдения/правил: не установлен", text)
        self.assertNotIn("<a id=", text)
        self.assertIn("Недатированные документы", text)

    def test_undated_asof_does_not_turn_fetch_date_into_source_period(self):
        metrics = core.load(self.run / "metrics.json")
        metrics[0]["as_of"] = "undated rewards docs with implementation warning; fetched 2026-10-14"
        core.save(self.run / "metrics.json", metrics)
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("undated rewards docs", text)
        self.assertNotIn("Период данных: 14.10.2026", text)
        self.assertIn("Время получения не равно", text)

    def test_meaningful_source_annotations_survive_without_duplicate_fetch_timestamp(self):
        report = self.report.replace("period is an announcement", "получено 2026-10-14T21:15:21.064171Z; анонс, не текущая supply")
        (self.run / "report.md").write_text(report, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        self.assertIn("анонс, не текущая supply", text)
        self.assertNotIn("2026-10-14T21:15:21.064171Z", text)

    def test_unknown_reference_cannot_be_hidden_by_lineage_cleanup(self):
        (self.run / "report.md").write_text(self.report + "\nМетрики: calc_000000000000.\n", encoding="utf-8")
        with self.assertRaises(core.ResearchError):
            self.publish()
        self.assertFalse((self.root / "reports").exists())

    def test_operational_text_does_not_hide_claimed_findings(self):
        (self.run / "report.md").write_text(self.report +
            "\nDedicated apply_patch недоступен; это техническая трассировка.\n\n"
            "Dedicated apply_patch не меняет важное условие. [claim-001]\n", encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("это техническая трассировка", text)
        self.assertIn("не меняет важное условие", text)

    def test_fences_inline_math_and_urls_are_preserved(self):
        extra = ("\n## Примеры\n\n`x < y` и https://example.com/2026-10-14T21:15:21Z.\n\n"
                 "````text\n```nested\n## Still code\nraw <a id=\"literal\"></a>\n````\n")
        (self.run / "report.md").write_text(self.report + extra, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        self.assertIn("`x < y`", text)
        self.assertIn("https://example.com/2026-10-14T21:15:21Z", text)
        self.assertIn('raw <a id="literal"></a>', text)
        self.assertIn("## Still code", text)

    def test_warnings_are_readable_and_do_not_expose_metric_lists(self):
        metrics = core.load(self.run / "metrics.json")
        metrics[0]["as_of"] = None
        core.save(self.run / "metrics.json", metrics)
        text = Path(self.publish()["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("Observation period unknown:", text)
        self.assertNotIn(self.metric, text)
        self.assertIn("У части наблюдений нет собственной даты", text)
        self.assertIn("Ревью не выполнено", text)

    def test_supplied_fdv_scenario_eliminates_supply_without_setting_fair_value(self):
        inputs = {"points": Decimal(417), "eligible": Decimal(10200000),
                  "genesis": Decimal(32), "fdv": Decimal(2200000000)}
        direct = Decimal(core.evaluate("points / eligible * genesis / 100 * fdv", inputs))
        self.assertEqual(direct.quantize(Decimal("0.01")), Decimal("28781.18"))
        for supply in (Decimal(1000000000), Decimal(10000000000)):
            allocation = Decimal(core.evaluate("points / eligible * genesis / 100 * supply",
                                               {key: value for key, value in {**inputs, "supply": supply}.items() if key != "fdv"}))
            price = inputs["fdv"] / supply
            self.assertAlmostEqual(allocation * price, direct, places=18)

    def test_changed_report_same_project_day_atomically_updates_one_canonical_file(self):
        first = self.publish()
        original = Path(first["report_path"]).read_bytes()
        (self.run / "report.md").write_text(self.report.replace("417 is an assumption", "417 remains an unverified assumption"), encoding="utf-8")
        archive = self.archive()
        second = self.publish()
        self.assertEqual(first["report_path"], second["report_path"])
        self.assertNotEqual(Path(first["report_path"]).read_bytes(), original)
        self.assertIn("417 remains an unverified assumption", Path(second["report_path"]).read_text())
        self.assertFalse(second["already_published"])
        self.assertNotIn("--", Path(second["report_path"]).name)
        self.assertEqual(list((self.root / "reports").iterdir()), [Path(second["report_path"])])
        self.assertEqual(archive, self.archive())

    def test_parallel_publication_does_not_overwrite_or_leave_temporary_files(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.publish(), range(4)))
        self.assertEqual(len({result["report_path"] for result in results}), 1)
        self.assertEqual(len(list((self.root / "reports").iterdir())), 1)

    def test_failed_final_gate_does_not_create_reports_directory(self):
        (self.run / "report.md").write_text("Incomplete", encoding="utf-8")
        result = self.publish()
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "reports").exists())
        result = mcp.dispatch(["publish", str(self.run)], root=self.root)
        self.assertTrue(result["isError"])
        self.assertFalse((self.root / "reports").exists())

    def test_missing_identity_and_tampered_source_cannot_publish(self):
        (self.run / self.record["body_path"]).write_bytes(b"changed")
        self.assertFalse(self.publish()["ok"])
        self.assertFalse((self.root / "reports").exists())

    def test_unknown_report_reference_fails_without_export(self):
        (self.run / "report.md").write_text(self.report + "\n[claim-unknown]\n", encoding="utf-8")
        with self.assertRaises(core.ResearchError):
            self.publish()
        self.assertFalse((self.root / "reports").exists())

    def test_unclosed_code_block_cannot_hide_report_or_review_notice(self):
        (self.run / "report.md").write_text(self.report + "\n```text\nunclosed", encoding="utf-8")
        with self.assertRaisesRegex(core.ResearchError, "unclosed"):
            self.publish()
        self.assertFalse((self.root / "reports").exists())

    def test_reports_directory_and_output_file_symlinks_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "reports").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.ResearchError):
            self.publish()
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "reports").unlink()
        first = self.publish()
        destination = Path(first["report_path"])
        destination.unlink()
        target = outside / "keep.md"
        target.write_text("Untouched", encoding="utf-8")
        destination.symlink_to(target)
        with self.assertRaises(core.ResearchError):
            self.publish()
        self.assertEqual(target.read_text(), "Untouched")

    def test_outside_or_symlinked_run_cannot_publish(self):
        with self.assertRaises(core.ResearchError):
            core.publish(self.run, project_root=self.root / "wrong-root")
        alias = self.run.parent / "alias"
        alias.symlink_to(self.run, target_is_directory=True)
        with self.assertRaises(core.ResearchError):
            core.publish(alias, project_root=self.root)

    def test_publication_uses_checked_snapshot_not_a_later_file_read(self):
        check = core.check

        def mutate_after_check(*args, **kwargs):
            result = check(*args, **kwargs)
            (self.run / "report.md").write_text("UNVALIDATED replacement", encoding="utf-8")
            return result

        with patch.object(core, "check", side_effect=mutate_after_check):
            result = self.publish()
        self.assertIn("32%", Path(result["report_path"]).read_text())
        self.assertNotIn("UNVALIDATED replacement", Path(result["report_path"]).read_text())

    def test_mcp_publish_confined_and_audit_rejects_publication(self):
        before = self.archive()
        research = mcp.dispatch(["publish", str(self.run)], root=self.root)
        self.assertFalse(research["isError"])
        value = json.loads(research["content"][0]["text"])
        self.assertTrue(value["ok"])
        self.assertTrue(Path(value["report_path"]).is_file())
        audit = mcp.dispatch(["publish", str(self.run)], root=self.root, tool_name="audit")
        self.assertTrue(audit["isError"])
        for flag in ("--root", "--project-root", "--output"):
            with redirect_stderr(io.StringIO()):
                self.assertTrue(mcp.dispatch(["publish", str(self.run), flag, str(self.root / "outside")], root=self.root)["isError"])
        self.assertEqual(before, self.archive())

    def test_slug_never_produces_paths_or_hidden_filenames(self):
        for name in ("../../.env", "/absolute/path", "Project\nName", "Проект Омни", "...", "a" * 200):
            with self.subTest(name=name):
                slug = project_slug(name)
                self.assertNotIn("/", slug)
                self.assertNotIn(".", slug)
                self.assertTrue(slug)
                self.assertLessEqual(len(slug), 80)

    def test_legacy_c_claims_ranges_and_s_source_aliases_are_resolved(self):
        claims = core.load(self.run / "claims.json")
        claims[0]["id"] = "C1"
        claims.append({**claims[0], "id": "C2"})
        core.save(self.run / "claims.json", claims)
        coverage = core.load(self.run / "coverage.json")
        coverage["questions"][0]["claim_ids"] = ["C1", "C2"]
        core.save(self.run / "coverage.json", coverage)
        report = self.report.replace("[claim-001]", "[C1–C2]")
        report += "\n- S1 — " + self.record["id"] + ": https://example.com/data\n"
        report += "- S2 — " + self.record["id"] + ": https://example.com/data\n"
        report = report.replace("## Scenarios", "Source aliases also resolve. [S1–S2]\n\n## Scenarios")
        (self.run / "report.md").write_text(report, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        self.assertNotIn("[C1", text)
        self.assertNotIn("[S1", text)
        self.assertIn("Source aliases also resolve.", text)
        self.assertNotRegex(text, r"\[\d+\]\(")
        self.assertEqual(text.count("**[Official allocation](https://example.com/data)**"), 1)

    def test_unknown_legacy_claim_is_not_silently_ignored(self):
        (self.run / "report.md").write_text(self.report + "\nUnknown. [C999]\n", encoding="utf-8")
        with self.assertRaises(core.ResearchError):
            self.publish()

    def test_unused_quote_timestamp_is_not_assigned_to_whole_source(self):
        metrics = core.load(self.run / "metrics.json")
        metrics[0]["as_of"] = None
        core.save(self.run / "metrics.json", metrics)
        unused = core.observe(self.run, self.record["id"], "/allocation", label="BTC quote timestamp, not TVL",
                              unit="percent", as_of="2026-10-16T17:05:43Z")
        text = Path(self.publish()["report_path"]).read_text()
        self.assertNotIn("17:05:43", text)
        self.assertNotIn(unused["label"], text)

    def test_used_period_is_scoped_to_metric_not_global_source_time(self):
        text = Path(self.publish()["report_path"]).read_text()
        self.assertIn("Период показателя", text)
        self.assertIn("Announced allocation", text)
        self.assertNotIn("/allocation", text)
        self.assertNotIn("Период данных:", text)

    def test_machine_coverage_diagnostics_are_not_injected_into_reader_summary(self):
        coverage = core.load(self.run / "coverage.json")
        coverage["answer_summary"] = "Объявлены32%Genesisпропорциональноpoints100%unlock"
        coverage["stop_reason_detail"] = "reviewerfinding Chineseoriginalsuccess finalP/currentprice"
        core.save(self.run / "coverage.json", coverage)
        text = Path(self.publish()["report_path"]).read_text()
        self.assertNotIn(coverage["answer_summary"], text)
        self.assertNotIn(coverage["stop_reason_detail"], text)
        self.assertNotIn("Ответ на исходный вопрос:", text)
        self.assertNotIn("Причина остановки:", text)
        self.assertIn("частичный ответ — решающие пробелы сохраняются", text)
        self.assertIn("**Денежная стоимость неизвестна.**", text)

    def test_sources_are_last_and_non_numbered_with_review_limits_before_them(self):
        text = Path(self.publish()["report_path"]).read_text()
        body, sources = text.split("## Источники\n", 1)
        self.assertNotIn("https://example.com/data", body)
        self.assertIn("- **[Official allocation](https://example.com/data)**", sources)
        self.assertNotIn("## Даты и границы исследования", sources)
        self.assertNotIn("### Проверка публикации", sources)
        self.assertIn("Ревью не выполнено", body)
        self.assertIn("Структурная проверка", body)

    def test_nested_source_list_is_moved_to_the_final_bibliography(self):
        extra = ("\n## Limits\n\nMeaningful limitation. [claim-001]\n\n"
                 "### Основные источники\n\n- [Official allocation](https://example.com/data)\n\n"
                 "### Still analysis\n\nMeaningful risk. [claim-001]\n")
        (self.run / "report.md").write_text(self.report + extra, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        body, sources = text.split("## Источники\n", 1)
        self.assertNotIn("### Основные источники", body)
        self.assertNotIn("https://example.com/data", body)
        self.assertIn("### Still analysis", body)
        self.assertIn("Meaningful risk.", body)
        self.assertIn("https://example.com/data", sources)

    def test_known_authored_numeric_and_named_source_links_move_to_the_end(self):
        extra = ("\n## Links\n\nЦифра. [1](https://example.com/data)\n\n"
                 "Содержательный [официальный документ](https://example.com/data).\n\n"
                 "`[1](https://example.com/data)` is literal code.\n")
        (self.run / "report.md").write_text(self.report + extra, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        section = text.split("## Links\n", 1)[1].split("## Даты и границы", 1)[0]
        self.assertIn("Цифра.", section)
        self.assertIn("Содержательный официальный документ.", section)
        self.assertNotIn("Цифра. [1]", section)
        self.assertIn("`[1](https://example.com/data)` is literal code.", section)

    def test_lineage_only_table_column_does_not_hide_unknown_references(self):
        report = self.report.replace("| " + self.metric + " |", "| obs_000000000000 |")
        (self.run / "report.md").write_text(report, encoding="utf-8")
        with self.assertRaises(core.ResearchError):
            self.publish()
        self.assertFalse((self.root / "reports").exists())

    def test_unknown_numeric_source_link_is_not_silently_dropped(self):
        (self.run / "report.md").write_text(self.report + "\nFact. [9](https://unknown.example/data)\n", encoding="utf-8")
        with self.assertRaisesRegex(core.ResearchError, "not backed by saved evidence"):
            self.publish()
        self.assertFalse((self.root / "reports").exists())

    def test_a_table_containing_only_lineage_does_not_leave_an_empty_table(self):
        extra = "\n## Lineage\n\n| Metric ID |\n| --- |\n| " + self.metric + " |\n"
        (self.run / "report.md").write_text(self.report + extra, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        section = text.split("## Lineage\n", 1)[1].split("## Даты и границы", 1)[0]
        self.assertNotIn("|", section)
        self.assertIn("https://example.com/data", text.split("## Источники\n", 1)[1])

    def test_optional_trailing_table_pipe_never_drops_the_last_value(self):
        extra = "\n## Table\n\n| First | Last\n| --- | ---\n| 32% | 417 [claim-001]\n"
        (self.run / "report.md").write_text(self.report + extra, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        self.assertIn("| First | Last |", text)
        self.assertIn("| 32% | 417 |", text)

    def test_escaped_final_table_pipe_is_cell_content_not_a_delimiter(self):
        extra = "\n## Table\n\n| First | Last\n| --- | ---\n| 32% | escaped \\|\n"
        (self.run / "report.md").write_text(self.report + extra, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        self.assertIn("| 32% | escaped \\| |", text)

    def test_source_column_with_analysis_is_preserved_instead_of_silently_dropped(self):
        report = self.report.replace("Metric ID", "Источники").replace(
            "| " + self.metric + " |", "| Объявлено, не исполнено. " + self.metric + " |")
        (self.run / "report.md").write_text(report, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        self.assertIn("Объявлено, не исполнено.", text)
        self.assertIn("| Источники |", text)

    def test_headings_quotes_and_nested_lists_keep_markdown_block_boundaries(self):
        extra = ("\n## Layout\n### Факты\nТекст без пустой строки. [claim-001]\n\n"
                 "> **Ограничение:** не текущая цена. [claim-001]\n\n"
                 "- Первый пункт. [claim-001]\n  - Вложенный пункт. [claim-001]\n"
                 "- Второй пункт. [claim-001]\n")
        (self.run / "report.md").write_text(self.report + extra, encoding="utf-8")
        text = Path(self.publish()["report_path"]).read_text()
        self.assertIn("### Факты\nТекст без пустой строки.", text)
        self.assertIn("> **Ограничение:** не текущая цена.", text)
        self.assertIn("- Первый пункт.\n  - Вложенный пункт.\n- Второй пункт.", text)

    def test_failed_atomic_republication_keeps_previous_file_and_cleans_temporary(self):
        first = self.publish()
        destination = Path(first["report_path"])
        original = destination.read_bytes()
        (self.run / "report.md").write_text(self.report + "\nNew analysis. [claim-001]\n", encoding="utf-8")
        with patch("crypto_research.publication.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.publish()
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_non_regular_destination_is_rejected(self):
        destination = Path(self.publish()["report_path"])
        destination.unlink()
        destination.mkdir()
        with self.assertRaisesRegex(core.ResearchError, "regular file"):
            self.publish()

    def legacy_export(self):
        destination = Path(self.publish()["report_path"])
        content = destination.read_bytes().replace(b"417 is an assumption", b"417 was an assumption")
        legacy = destination.with_stem(destination.stem + "--" + hashlib.sha256(content).hexdigest()[:12])
        legacy.write_bytes(content)
        return destination, legacy

    def test_republication_removes_only_verified_legacy_hash_exports(self):
        destination, legacy = self.legacy_export()
        archive = self.archive()
        result = self.publish()
        self.assertEqual(result["removed_legacy_reports"], [legacy.name])
        self.assertEqual(result["cleanup_warnings"], [])
        self.assertFalse(legacy.exists())
        self.assertEqual(list(destination.parent.iterdir()), [destination])
        self.assertEqual(self.archive(), archive)

    def test_legacy_cleanup_preserves_user_modified_reports(self):
        destination, legacy = self.legacy_export()
        legacy.write_bytes(legacy.read_bytes() + b"\nUser note.\n")
        result = self.publish()
        self.assertTrue(legacy.exists())
        self.assertEqual(result["removed_legacy_reports"], [])
        self.assertTrue(result["cleanup_warnings"])
        self.assertTrue(destination.exists())

    def test_legacy_cleanup_does_not_follow_symlinks_or_delete_unrecognised_files(self):
        destination, legacy = self.legacy_export()
        target = self.root / "keep.md"
        legacy.replace(target)
        legacy.symlink_to(target)
        content = b"User's research, not a generated export."
        user_file = destination.with_stem(destination.stem + "--" + hashlib.sha256(content).hexdigest()[:12])
        user_file.write_bytes(content)
        result = self.publish()
        self.assertTrue(legacy.is_symlink())
        self.assertTrue(target.is_file())
        self.assertEqual(user_file.read_bytes(), content)
        self.assertEqual(result["removed_legacy_reports"], [])

    def test_legacy_cleanup_failure_keeps_canonical_publication_intact(self):
        destination, legacy = self.legacy_export()
        unlink = Path.unlink

        def fail_legacy(path, *args, **kwargs):
            if path == legacy:
                raise PermissionError("no removal")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_legacy):
            result = self.publish()
        self.assertTrue(result["ok"])
        self.assertTrue(destination.is_file())
        self.assertTrue(legacy.is_file())
        self.assertEqual(result["removed_legacy_reports"], [])
        self.assertTrue(result["cleanup_warnings"])
