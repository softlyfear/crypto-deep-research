from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crypto_research import core
from crypto_research.quality import assess


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.dict("os.environ", {name: "" for name, *_ in core.API_SETTINGS}):
            self.run = Path(core.init("What changed in the rules?", root=self.root / "artifacts/runs")["run_dir"])
        with patch.object(core, "download", return_value=(b'{"allocation":32}', "application/json", 200, "https://example.com/rules")):
            self.primary = core.fetch(self.run, "https://example.com/rules", kind="official")
        with patch.object(core, "download", return_value=(b'{"allocation":32}', "application/json", 200, "https://independent.example/analysis")):
            self.independent = core.fetch(self.run, "https://independent.example/analysis", kind="other")
        core.identity(self.run, name="Example", official_url="https://example.com/", chains=[], token_status="unknown",
                      evidence_ids=[self.primary["id"]])
        self.claims = [{"id": "claim-001", "text": "The announcement changed the rules", "category": "fact",
                        "evidence_ids": [self.primary["id"], self.independent["id"]], "metric_ids": []}]
        self.verification = {"status": "same_model_or_unknown", "writer_model": None, "reviewer_model": None,
                             "warnings": ["Same inherited model; not independent cross-model verification"]}
        self.coverage = {
            "answer_status": "answered", "answer_summary": "The changed rule is documented and independently examined",
            "stop_reason": "saturation", "stop_reason_detail": "Original announcement, history and independent analysis agree; targeted follow-up added no new decisive facts",
            "questions": [{"id": "q1", "question": "What changed?", "priority": "critical", "status": "answered",
                           "finding": "Specific rule change", "evidence_ids": [self.primary["id"]],
                           "claim_ids": ["claim-001"], "attempted_evidence_ids": [self.primary["id"]]}],
            "source_groups": [
                {"owner": "Project", "relationship": "primary", "independence_basis": "Original rules",
                 "evidence_ids": [self.primary["id"]]},
                {"owner": "Independent analyst", "relationship": "independent", "independence_basis": "Original externally owned analysis, not syndication",
                 "evidence_ids": [self.independent["id"]]}],
            "leads": [], "valuation": {"kind": "not_applicable", "basis": "Question concerns a rule change, not a price", "input_claim_ids": []},
            "review": {"status": "pass", "answer_addressed": True, "assumptions_supported": True,
                       "counterevidence_checked": True, "notes": "Original and independent accounts reconcile; no decisive gaps in scoped question"}}
        (self.run / "report.md").write_text("# Rules\n\nSpecific change. [claim-001]\n\n" + core.VERIFICATION_NOTICE + "\n", encoding="utf-8")
        core.save(self.run / "claims.json", self.claims)
        core.save(self.run / "verification.json", self.verification)

    def check(self):
        core.save(self.run / "coverage.json", self.coverage)
        return core.check(self.run, final=True)

    def test_new_runs_require_explicit_coverage(self):
        result = core.check(self.run, final=True)
        self.assertFalse(result["ok"])
        self.assertIn("Missing final artifact: coverage.json", result["errors"])
        self.assertEqual(result["quality"]["status"], "unassessed")

    def test_supported_coverage_is_separate_from_integrity_and_independence(self):
        result = self.check()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["quality"]["status"], "adequate")
        self.assertIn("No confirmed independent cross-model verification", result["warnings"])

    def test_honest_partial_report_may_publish_but_not_as_full_research(self):
        self.coverage.update(answer_status="partial", stop_reason="access")
        self.coverage["valuation"] = {"kind": "illustrative_only", "basis": "No final denominator or executable price", "input_claim_ids": []}
        self.coverage["review"].update(status="needs_work", answer_addressed=False, assumptions_supported=False)
        self.assertTrue(self.check()["ok"])
        result = core.publish(self.run, project_root=self.root)
        self.assertEqual(result["final_check"]["quality"]["status"], "partial")
        text = Path(result["report_path"]).read_text()
        self.assertIn("частичный ответ", text)
        self.assertNotIn("Полное исследование", text)

    def test_arbitrary_scenario_cannot_be_labeled_answered(self):
        self.coverage["valuation"].update(kind="illustrative_only", basis="Arbitrary dollar pool and point count")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("Coverage cannot claim answered while decisive coverage gaps remain", result["errors"])

    def test_missing_critical_denominator_prevents_complete_label(self):
        self.coverage["questions"][0].update(status="unanswered", gap="Final eligible denominator unknown")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("Critical question unresolved: q1", result["quality"]["gaps"])

    def test_unfollowed_decisive_announcement_is_not_saturation(self):
        self.coverage["leads"] = [{"source_evidence_id": self.primary["id"], "url": "https://example.com/announcement",
                                   "priority": "critical", "status": "pending", "reason": "Linked from rules, not read",
                                   "resolved_evidence_id": None}]
        self.assertFalse(self.check()["ok"])

    def test_mirrors_cannot_count_same_evidence_twice(self):
        self.coverage["source_groups"][1]["evidence_ids"] = [self.primary["id"]]
        self.assertIn("An evidence item cannot count as multiple independent owners", self.check()["errors"])

    def test_distinct_urls_do_not_allow_duplicate_origin_groups(self):
        self.coverage["source_groups"][1]["owner"] = " PROJECT "
        self.assertFalse(self.check()["ok"])

    def test_unexecuted_review_cannot_supply_sufficiency_pass(self):
        core.save(self.run / "verification.json", {**self.verification, "status": "not_performed"})
        self.assertIn("A sufficiency pass requires a recorded executed reviewer", self.check()["errors"])

    def test_search_page_cannot_be_counted_as_independent_support(self):
        evidence = {record["id"]: record for record in (self.primary, self.independent)}
        evidence[self.independent["id"]] = {**self.independent, "kind": "search"}
        _, errors = assess(self.coverage, evidence, self.claims, verification=self.verification)
        self.assertIn("Search pages cannot be primary or independent economic evidence", errors)

    def test_budget_or_access_stop_cannot_be_disguised_as_complete(self):
        for reason in ("budget", "deadline", "access"):
            with self.subTest(reason=reason):
                self.coverage["stop_reason"] = reason
                self.assertFalse(self.check()["ok"])

    def test_scenario_claim_is_not_an_evidence_based_input(self):
        core.save(self.run / "claims.json", [{**self.claims[0], "category": "scenario"}])
        self.coverage["valuation"].update(kind="evidence_based", input_claim_ids=["claim-001"])
        self.assertFalse(self.check()["ok"])

    def test_critical_question_cannot_be_answered_only_by_scenarios(self):
        core.save(self.run / "claims.json", [{**self.claims[0], "category": "scenario"}])
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("Critical answer rests only on hypothetical scenarios: q1", result["quality"]["gaps"])

    def test_valuation_requires_registered_input_metrics(self):
        self.coverage["valuation"].update(kind="evidence_based", input_claim_ids=["claim-001"])
        self.assertIn("An evidence-based valuation requires registered quantitative input metrics", self.check()["errors"])
        metric = core.observe(self.run, self.primary["id"], "/allocation", label="Announced allocation", unit="percent", as_of="announced terms, update date unknown")
        core.save(self.run / "claims.json", [{**self.claims[0], "metric_ids": [metric["id"]]}])
        self.assertTrue(self.check()["ok"])

    def test_followed_lead_requires_matching_successful_destination(self):
        self.coverage["leads"] = [{"source_evidence_id": self.primary["id"], "url": "https://example.com/missing-announcement",
                                   "priority": "critical", "status": "followed", "reason": "Claimed examined",
                                   "resolved_evidence_id": self.independent["id"]}]
        self.assertIn("Followed lead destination does not match its saved evidence", self.check()["errors"])
        self.coverage["leads"][0]["url"] = self.independent["url"]
        self.assertTrue(self.check()["ok"])

    def test_unknown_claim_or_evidence_fails(self):
        for field in ("evidence_ids", "claim_ids", "attempted_evidence_ids"):
            with self.subTest(field=field):
                original = self.coverage["questions"][0][field]
                self.coverage["questions"][0][field] = ["fabricated"]
                self.assertFalse(self.check()["ok"])
                self.coverage["questions"][0][field] = original

    def test_legacy_archive_is_not_modified_or_promoted_to_full(self):
        manifest = core.load(self.run / "run.json")
        manifest.pop("coverage_schema")
        core.save(self.run / "run.json", manifest)
        before = {path.relative_to(self.run): path.read_bytes() for path in self.run.rglob("*") if path.is_file()}
        result = core.publish(self.run, project_root=self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["final_check"]["quality"]["status"], "unassessed")
        self.assertIn("полнота ответа не оценена", Path(result["report_path"]).read_text())
        self.assertEqual(before, {path.relative_to(self.run): path.read_bytes() for path in self.run.rglob("*") if path.is_file()})

    def test_malformed_coverage_types_report_errors_not_exceptions(self):
        evidence = {record["id"]: record for record in (self.primary, self.independent)}
        cases = [[], {}, {**self.coverage, "answer_status": []}, {**self.coverage, "stop_reason": {}},
                 {**self.coverage, "questions": [None]}, {**self.coverage, "source_groups": [None]},
                 {**self.coverage, "leads": [{"source_evidence_id": [], "resolved_evidence_id": []}]},
                 {**self.coverage, "valuation": {"kind": []}}, {**self.coverage, "review": {"status": []}}]
        for value in cases:
            with self.subTest(value=value):
                _, errors = assess(deepcopy(value), evidence, self.claims, required=True, verification=self.verification)
                self.assertTrue(errors)
