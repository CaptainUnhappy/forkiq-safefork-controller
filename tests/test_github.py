"""GitHub response classification at the transport boundary."""
import unittest
from unittest import mock

from app.github import GitHub, RefNotFastForward, RefObjectNotFound


class UpdateRefTests(unittest.TestCase):
    def test_ref_validation_errors_are_classified_by_cause_not_status_alone(self):
        cases = [
            ("Object does not exist", RefObjectNotFound),
            ("Update is not a fast forward", RefNotFastForward),
            ("Not a fast-forward update.", RefNotFastForward),
            ("Validation failed", RuntimeError),
            ("Reference update failed", RuntimeError),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                client = GitHub()
                payload = {"message": message, "documentation_url": "https://docs.github.com/rest/git/refs"}
                with mock.patch.object(client, "installation_token", return_value="test-token"), \
                     mock.patch.object(client, "_request", return_value=(422, payload, {})) as request:
                    with self.assertRaises(expected) as caught:
                        client.update_ref("me/demo", "main", "a" * 40)
                self.assertIs(type(caught.exception), expected)
                self.assertIn("422", str(caught.exception))
                self.assertIn("me/demo:main", str(caught.exception))
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("documentation_url", str(caught.exception))
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.args[3], {"sha": "a" * 40, "force": False})

    def test_missing_object_is_not_retried_inside_client_even_with_force(self):
        client = GitHub()
        with mock.patch.object(client, "installation_token", return_value="test-token"), \
             mock.patch.object(client, "_request", return_value=(422, {"message": "Object does not exist"}, {})) as request:
            with self.assertRaises(RefObjectNotFound):
                client.update_ref("me/demo", "main", "a" * 40, force=True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[3], {"sha": "a" * 40, "force": True})

    def test_other_validation_error_preserves_structured_diagnostic_details(self):
        client = GitHub()
        payload = {"message": "Validation failed", "errors": [{"resource": "Reference", "code": "invalid"}]}
        with mock.patch.object(client, "installation_token", return_value="test-token"), \
             mock.patch.object(client, "_request", return_value=(422, payload, {})):
            with self.assertRaises(RuntimeError) as caught:
                client.update_ref("me/demo", "main", "a" * 40)
        self.assertIs(type(caught.exception), RuntimeError)
        self.assertIn('"code": "invalid"', str(caught.exception))


if __name__ == "__main__":
    unittest.main()
