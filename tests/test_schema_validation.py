from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from schema_validation import (  # noqa: E402
    SchemaValidationError,
    _pointer,
    validate_payload,
)

REVIEW = "schemas/review.schema.json"
REVIEW_DELTA = "schemas/review-delta.schema.json"
ADVERSARIAL = "schemas/adversarial-review.schema.json"
TRIAGE = "schemas/triage.schema.json"
WORK_RESULT = "schemas/work-result.schema.json"
COMPLETION_DISPOSITION = "schemas/completion-disposition.schema.json"


def _valid_review() -> dict:
    return {
        "verdict": "pass",
        "summary": "s",
        "findings": [],
        "verification_gaps": [],
        "acceptance_criteria_assessment": [],
        "confidence": 1.0,
    }


def _valid_delta() -> dict:
    return {
        "verdict": "pass",
        "summary": "s",
        "resolved_findings": [],
        "new_findings": [],
        "regressions": [],
        "affected_acceptance_criteria": [],
        "confidence": 1.0,
    }


def _valid_finding(**over) -> dict:
    base = {
        "id": "F-1",
        "severity": "high",
        "category": "security",
        "file": "a.py",
        "line_start": 3,
        "description": "d",
        "evidence": "e",
        "recommended_fix": "f",
    }
    base.update(over)
    return base


class ValidatorPositiveTests(unittest.TestCase):
    def test_minimal_review_valid(self) -> None:
        validate_payload(_valid_review(), REVIEW)

    def test_review_with_finding_valid(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding()]
        payload["acceptance_criteria_assessment"] = [
            {"id": "AC-1", "status": "satisfied", "evidence": "e"}
        ]
        validate_payload(payload, REVIEW)

    def test_minimal_delta_valid(self) -> None:
        validate_payload(_valid_delta(), REVIEW_DELTA)

    def test_null_file_and_line_allowed(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding(file=None, line_start=None)]
        validate_payload(payload, REVIEW)

    def test_completion_disposition_valid(self) -> None:
        validate_payload(
            {
                "summary": "defer hardening",
                "findings": [{
                    "source_phase": "adversarial", "source_round": 8,
                    "source_id": "A-8-T-1", "title": "Crash recovery",
                    "severity": "medium", "category": "data_loss",
                    "description": "Hard-crash recovery hardening",
                    "impact": "additional_hardening", "disposition": "FIX_LATER",
                    "rationale": "Outside accepted early scope", "evidence": "tests pass",
                    "relevant_acceptance_criteria": ["AC-1"], "relevant_files": ["db.py"],
                    "suggested_future_scope": "transactional persistence",
                    "recommended_verification": ["crash injection"],
                    "human_input_eventually_required": False,
                    "provenance": "adversarial-08.codex.json#threats/0",
                }],
            },
            COMPLETION_DISPOSITION,
        )


class ValidatorNegativeTests(unittest.TestCase):
    def test_missing_required_top_level_key(self) -> None:
        payload = _valid_delta()
        del payload["new_findings"]
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW_DELTA)
        self.assertIn("new_findings", str(ctx.exception))

    def test_mistyped_array_as_object_rejected(self) -> None:
        payload = _valid_delta()
        payload["new_findings"] = {}
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW_DELTA)
        self.assertIn("new_findings", str(ctx.exception))

    def test_invalid_verdict_enum_rejected(self) -> None:
        payload = _valid_review()
        payload["verdict"] = "looks_fine"
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_invalid_severity_in_nested_finding_rejected(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding(severity="catastrophic")]
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW)
        # Error must point inside the nested finding.
        self.assertIn("/findings/0/severity", str(ctx.exception))

    def test_bad_finding_id_pattern_rejected(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding(id="bug-7")]
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_nested_finding_missing_required_field_rejected(self) -> None:
        bad = _valid_finding()
        del bad["evidence"]
        payload = _valid_review()
        payload["findings"] = [bad]
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW)
        self.assertIn("/findings/0", str(ctx.exception))

    def test_additional_property_rejected(self) -> None:
        payload = _valid_review()
        payload["unexpected"] = True
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_confidence_out_of_range_rejected(self) -> None:
        payload = _valid_review()
        payload["confidence"] = 1.5
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_bool_for_number_rejected(self) -> None:
        payload = _valid_review()
        payload["confidence"] = True
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_unloadable_schema_fails_closed(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_payload({}, "schemas/does-not-exist.schema.json")

    def test_error_lists_all_violations_sorted(self) -> None:
        payload = _valid_review()
        payload["verdict"] = "nope"
        payload["confidence"] = 2
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW)
        msg = str(ctx.exception)
        self.assertIn("/confidence", msg)
        self.assertIn("/verdict", msg)


class TriageSchemaTests(unittest.TestCase):
    def test_valid_ledger_accepted(self) -> None:
        validate_payload(
            [
                {"fingerprint": "a.py:f:bug", "status": "rejected", "reason": "r"},
                {
                    "fingerprint": "b.py:g:bug",
                    "status": "resolved",
                    "finding_id": "F-2",
                    "resolution": "fixed",
                },
            ],
            TRIAGE,
        )

    def test_missing_fingerprint_rejected(self) -> None:
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload([{"status": "rejected", "reason": "r"}], TRIAGE)
        self.assertIn("fingerprint", str(ctx.exception))

    def test_empty_fingerprint_rejected(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_payload([{"fingerprint": "", "status": "rejected"}], TRIAGE)


    def test_unknown_status_rejected(self) -> None:
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(
                [{"fingerprint": "a.py:f", "status": "looks-bad"}], TRIAGE
            )
        self.assertIn("/0/status", str(ctx.exception))

    def test_non_array_rejected(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_payload({"fingerprint": "x"}, TRIAGE)

    def test_canonical_finding_id_accepted(self) -> None:
        validate_payload(
            [{"fingerprint": "a.py:f", "status": "resolved", "finding_id": "F-10"}],
            TRIAGE,
        )

    def test_malformed_finding_id_rejected(self) -> None:
        # A non-canonical finding_id is silently dropped by the merge, so the
        # schema must reject it instead of letting the command misreport it as
        # recorded.
        for bad in ("bug-7", "F7", "f-7", "F-", "F-1a"):
            with self.subTest(finding_id=bad):
                with self.assertRaises(SchemaValidationError) as ctx:
                    validate_payload(
                        [
                            {
                                "fingerprint": "a.py:f",
                                "status": "resolved",
                                "finding_id": bad,
                            }
                        ],
                        TRIAGE,
                    )
                self.assertIn("/0/finding_id", str(ctx.exception))


class WorkResultSchemaTests(unittest.TestCase):
    def test_ui_review_is_optional(self) -> None:
        validate_payload({"summary": "non-visual change"}, WORK_RESULT)
        validate_payload({}, WORK_RESULT)

    def test_scenarios_and_groups_are_validated(self) -> None:
        validate_payload(
            {
                "ui_review": {
                    "scenarios": ["reference.add-range", "reference.dark"],
                    "groups": ["reference-library"],
                }
            },
            WORK_RESULT,
        )
        with self.assertRaises(SchemaValidationError):
            validate_payload(
                {"ui_review": {"scenarios": ["../invented"]}}, WORK_RESULT
            )


class JsonPointerEscapingTests(unittest.TestCase):
    def test_root_pointer(self) -> None:
        self.assertEqual(_pointer([]), "(root)")

    def test_plain_tokens(self) -> None:
        self.assertEqual(_pointer(["findings", 0, "severity"]), "/findings/0/severity")

    def test_slash_is_escaped(self) -> None:
        # RFC 6901: '/' in a token becomes '~1'.
        self.assertEqual(_pointer(["a/b"]), "/a~1b")

    def test_tilde_is_escaped(self) -> None:
        # RFC 6901: '~' becomes '~0'.
        self.assertEqual(_pointer(["a~b"]), "/a~0b")

    def test_tilde_escaped_before_slash(self) -> None:
        # '~1' must denote a literal '/', so an input '~1' must encode to '~01',
        # not be confused with an escaped slash.
        self.assertEqual(_pointer(["~1"]), "/~01")


if __name__ == "__main__":
    unittest.main()
