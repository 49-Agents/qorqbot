"""AgenticError -> DomainError translation keeps host HTTP statuses and gives explicit retry advice."""
import unittest

from qorqut_agentic.errors import (
    ERROR_CODES, AgenticError, InvalidRequestError, NotFoundError, RevisionConflictError, ValidationFailedError,
)
from qorqut_operations.contracts import DomainError, OperationResult

from qorqut.errors import AGENTIC_CODE_MAP, domain_error_from_agentic
from qorqut.transport.results import http_status, response


def status_of(error):
    return http_status(OperationResult.failure(error, request_id="r"))


class MappedCodes(unittest.TestCase):
    def test_every_mapped_key_is_a_real_agentic_code(self):
        self.assertLessEqual(set(AGENTIC_CODE_MAP), set(ERROR_CODES))

    def test_mapped_codes_get_host_code_retryability_and_status(self):
        expected = {
            "not_found": ("not_found", False, 404),
            "revision_conflict": ("revision_conflict", True, 409),
            "stale_revision": ("revision_conflict", True, 409),
            "invalid_request": ("invalid_request", False, 400),
            "validation_failed": ("validation_error", False, 422),
            "unavailable": ("operation_unavailable", True, 503),
            "runtime_unavailable": ("operation_unavailable", True, 503),
            "lock_timeout": ("operation_unavailable", True, 503),
            "internal_error": ("internal_error", False, 500),
        }
        self.assertEqual(set(expected), set(AGENTIC_CODE_MAP))
        for agentic_code, (code, retryable, status) in expected.items():
            with self.subTest(agentic_code=agentic_code):
                error = domain_error_from_agentic(AgenticError("boom", code=agentic_code))
                self.assertIsInstance(error, DomainError)
                self.assertEqual((error.code, error.retryable, status_of(error)), (code, retryable, status))
                self.assertEqual(error.details["agentic_code"], agentic_code)

    def test_subclass_default_codes_translate(self):
        self.assertEqual(domain_error_from_agentic(NotFoundError("gone")).code, "not_found")
        self.assertEqual(domain_error_from_agentic(InvalidRequestError("bad")).code, "invalid_request")
        self.assertEqual(domain_error_from_agentic(ValidationFailedError("bad")).code, "validation_error")
        conflict = domain_error_from_agentic(RevisionConflictError("moved"))
        self.assertEqual((conflict.code, conflict.retryable), ("revision_conflict", True))

    def test_retryable_translation_sets_retry_after(self):
        result = OperationResult.failure(domain_error_from_agentic(AgenticError("busy", code="lock_timeout")), request_id="r")
        self.assertEqual(response(result).headers.get("Retry-After"), "2")


class UnmappedCodes(unittest.TestCase):
    def test_unmapped_codes_are_prefixed_and_not_retryable(self):
        error = domain_error_from_agentic(AgenticError("needs owner", code="owner_input_required"))
        self.assertEqual((error.code, error.retryable), ("agentic_owner_input_required", False))
        self.assertEqual(error.details["agentic_code"], "owner_input_required")
        self.assertEqual(status_of(error), 422)

    def test_every_agentic_code_translates_without_colliding_with_an_unmapped_host_code(self):
        for agentic_code in ERROR_CODES:
            with self.subTest(agentic_code=agentic_code):
                code = domain_error_from_agentic(AgenticError("x", code=agentic_code)).code
                self.assertTrue(agentic_code in AGENTIC_CODE_MAP or code == f"agentic_{agentic_code}")


class PayloadPreservation(unittest.TestCase):
    def test_message_and_details_are_kept(self):
        error = domain_error_from_agentic(AgenticError("Delta is missing", code="not_found", details={"delta_id": "d1"}))
        self.assertEqual(error.message, "Delta is missing")
        self.assertEqual(error.to_dict()["details"], {"delta_id": "d1", "agentic_code": "not_found"})

    def test_original_agentic_code_wins_over_a_details_key_of_the_same_name(self):
        error = domain_error_from_agentic(AgenticError("x", code="conflict", details={"agentic_code": "spoofed"}))
        self.assertEqual(error.details["agentic_code"], "conflict")

    def test_missing_code_and_details_fall_back_safely(self):
        class Bare(Exception):
            code = None
            details = None

        error = domain_error_from_agentic(Bare("plain failure"))
        self.assertEqual((error.code, error.message), ("internal_error", "plain failure"))
        self.assertEqual(error.to_dict()["details"], {"agentic_code": "internal_error"})


if __name__ == "__main__":
    unittest.main()
