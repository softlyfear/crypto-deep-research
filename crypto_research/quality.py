"""Validate declared coverage separately from artifact integrity.

These gates check traceability and contradictions, not source entailment,
independent ownership, or investment quality. Those still require review.
"""


def assess(coverage, evidence, claims, *, required=False, verification=None):
    errors, gaps = [], []
    if coverage is None:
        if required:
            errors.append("Missing final artifact: coverage.json")
        return {"status": "unassessed", "answer_status": "unknown", "gaps": ["Research coverage has not been assessed"]}, errors
    if not isinstance(coverage, dict):
        return {"status": "unassessed", "gaps": []}, ["coverage.json must be an object"]
    answer = coverage.get("answer_status")
    if answer not in ("answered", "partial", "unanswered"):
        errors.append("Coverage requires answer_status: answered, partial or unanswered")
    for name in ("answer_summary", "stop_reason_detail"):
        if not isinstance(coverage.get(name), str) or not coverage[name].strip():
            errors.append("Coverage requires nonempty " + name)
    stop = coverage.get("stop_reason")
    if stop not in ("saturation", "budget", "deadline", "access"):
        errors.append("Coverage requires a supported stop_reason")
    if answer != "answered":
        gaps.append("The user's question is not fully answered")

    by_claim = {claim["id"]: claim for claim in (claims if isinstance(claims, list) else [])
                if isinstance(claim, dict) and isinstance(claim.get("id"), str)}

    def references(item, field, known):
        values = item.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) or value not in known for value in values):
            errors.append("Invalid coverage references: " + field)
            return []
        return values

    successful = {key: record for key, record in evidence.items() if record["status"] == "ok"}
    questions = coverage.get("questions")
    if not isinstance(questions, list) or not questions:
        errors.append("Coverage requires a nonempty question matrix")
        questions = []
    ids, critical = set(), 0
    for item in questions:
        if not isinstance(item, dict):
            errors.append("Malformed coverage question")
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            errors.append("Missing or duplicate coverage question ID")
        else:
            ids.add(identifier)
        for name in ("question", "finding"):
            if not isinstance(item.get(name), str) or not item[name].strip():
                errors.append("Coverage question requires " + name)
        if item.get("priority") not in ("critical", "supporting") or item.get("status") not in ("answered", "partial", "unanswered"):
            errors.append("Invalid question priority/status")
        sources = references(item, "evidence_ids", successful)
        claim_ids = references(item, "claim_ids", by_claim)
        attempts = references(item, "attempted_evidence_ids", evidence)
        if item.get("status") == "answered" and (not sources or not claim_ids):
            errors.append("Answered questions require evidence and linked claims")
        if item.get("status") == "answered" and sources and all(successful[source].get("kind") == "search" for source in sources):
            errors.append("Search results are leads, not a supported answer")
        if item.get("priority") == "critical" and item.get("status") == "answered" and claim_ids and all(by_claim[key].get("category") == "scenario" for key in claim_ids):
            gaps.append("Critical answer rests only on hypothetical scenarios: " + str(identifier))
        if item.get("status") != "answered":
            if not isinstance(item.get("gap"), str) or not item["gap"].strip():
                errors.append("Unanswered questions require an explicit gap")
            if not attempts:
                gaps.append("No saved investigation attempts for " + str(identifier))
        if item.get("priority") == "critical":
            critical += 1
            if item.get("status") != "answered":
                gaps.append("Critical question unresolved: " + str(identifier))
    if not critical:
        errors.append("Coverage requires at least one critical question")

    groups = coverage.get("source_groups")
    if not isinstance(groups, list):
        errors.append("Coverage source_groups must be an array")
        groups = []
    relationships, owners = set(), set()
    assigned = {}
    for group in groups:
        if not isinstance(group, dict):
            errors.append("Malformed coverage source group")
            continue
        owner = group.get("owner")
        relation = group.get("relationship")
        if not isinstance(owner, str) or not owner.strip() or owner.strip().casefold() in owners:
            errors.append("Source groups require distinct named owners, not duplicate domains")
        else:
            owners.add(owner.strip().casefold())
        if relation not in ("primary", "independent", "discovery"):
            errors.append("Invalid source relationship")
        if not isinstance(group.get("independence_basis"), str) or not group["independence_basis"].strip():
            errors.append("Source groups require an independence_basis")
        sources = references(group, "evidence_ids", successful)
        for source in sources:
            if source in assigned:
                errors.append("An evidence item cannot count as multiple independent owners")
            assigned[source] = owner
        if relation in ("primary", "independent") and any(successful[source].get("kind") == "search" for source in sources):
            errors.append("Search pages cannot be primary or independent economic evidence")
        if sources and relation in ("primary", "independent", "discovery"):
            relationships.add(relation)
    if not {"primary", "independent"}.issubset(relationships):
        gaps.append("Primary and independently originated evidence are not both established")

    leads = coverage.get("leads")
    if not isinstance(leads, list):
        errors.append("Coverage leads must be an array")
        leads = []
    for lead in leads:
        if not isinstance(lead, dict):
            errors.append("Malformed coverage lead")
            continue
        if not isinstance(lead.get("source_evidence_id"), str) or lead["source_evidence_id"] not in successful:
            errors.append("Lead requires successful discovery evidence")
        if not isinstance(lead.get("url"), str) or not lead["url"].startswith("https://"):
            errors.append("Lead requires an HTTPS URL")
        if lead.get("priority") not in ("critical", "supporting") or lead.get("status") not in ("followed", "blocked", "irrelevant", "pending"):
            errors.append("Invalid lead priority/status")
        if not isinstance(lead.get("reason"), str) or not lead["reason"].strip():
            errors.append("Lead requires a disposition reason")
        resolved_id = lead.get("resolved_evidence_id")
        resolved = evidence.get(resolved_id) if isinstance(resolved_id, str) else None
        if lead.get("status") == "followed" and (not resolved or resolved["status"] != "ok"):
            errors.append("Followed leads require successful saved destination evidence")
        if lead.get("status") == "followed" and resolved and isinstance(lead.get("url"), str):
            from .core import safe_url
            from .discovery import lead_url
            destination = lead_url(lead["url"], lead["url"])
            if not destination or safe_url(destination) not in (resolved["url"], resolved.get("final_url")):
                errors.append("Followed lead destination does not match its saved evidence")
        if lead.get("status") == "blocked" and not resolved:
            gaps.append("Blocked lead has no saved fetch attempt")
        if lead.get("priority") == "critical" and lead.get("status") in ("pending", "blocked"):
            gaps.append("Decisive lead remains unexamined: " + str(lead.get("url", "")))

    valuation = coverage.get("valuation")
    if not isinstance(valuation, dict) or valuation.get("kind") not in ("not_applicable", "evidence_based", "illustrative_only", "unavailable"):
        errors.append("Coverage requires an explicit valuation assessment")
    else:
        if not isinstance(valuation.get("basis"), str) or not valuation["basis"].strip():
            errors.append("Valuation requires a basis or explanation of unavailability")
        inputs = references(valuation, "input_claim_ids", by_claim)
        if valuation["kind"] == "evidence_based" and (not inputs or any(by_claim[key].get("category") == "scenario" for key in inputs)):
            errors.append("An evidence-based valuation cannot rest on hypothetical scenario inputs")
        if valuation["kind"] == "evidence_based" and inputs and not any(by_claim[key].get("metric_ids") for key in inputs):
            errors.append("An evidence-based valuation requires registered quantitative input metrics")
        if valuation["kind"] in ("illustrative_only", "unavailable"):
            gaps.append("A supported valuation is unavailable; arbitrary sensitivity is not an answer")

    review = coverage.get("review")
    if not isinstance(review, dict) or review.get("status") not in ("pass", "needs_work", "not_performed"):
        errors.append("Coverage requires a sufficiency review")
    else:
        for name in ("answer_addressed", "assumptions_supported", "counterevidence_checked"):
            if type(review.get(name)) is not bool:
                errors.append("Coverage review requires boolean " + name)
        if not isinstance(review.get("notes"), str) or not review["notes"].strip():
            errors.append("Coverage review requires substantive notes")
        if review["status"] != "pass" or not all(review.get(name) is True for name in ("answer_addressed", "assumptions_supported", "counterevidence_checked")):
            gaps.append("Sufficiency review did not establish a supported answer")
        if review["status"] == "pass" and (not isinstance(verification, dict) or verification.get("status") not in ("same_model_or_unknown", "independent")):
            errors.append("A sufficiency pass requires a recorded executed reviewer")
    if stop != "saturation":
        gaps.append("Research stopped on a resource/access constraint, not evidence saturation")
    if answer == "answered" and gaps:
        errors.append("Coverage cannot claim answered while decisive coverage gaps remain")
    return {"status": "adequate" if not gaps and not errors else "partial", "answer_status": answer,
            "gaps": list(dict.fromkeys(gaps)), "question_count": len(questions),
            "source_group_count": len(groups)}, errors
