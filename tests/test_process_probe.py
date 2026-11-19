import unittest
from unittest.mock import Mock
from process_reaping_probe import process_state


class ProcfsRaceTests(unittest.TestCase):
    def test_disappearance_is_reaped_but_permission_errors_are_not(self):
        for error in (FileNotFoundError(), ProcessLookupError()):
            self.assertIsNone(process_state(Mock(read_text=Mock(side_effect=error))))
        with self.assertRaises(PermissionError):
            process_state(Mock(read_text=Mock(side_effect=PermissionError())))
        self.assertEqual(process_state(Mock(read_text=Mock(return_value='42 (git helper) Z 1 0'))),'Z')
