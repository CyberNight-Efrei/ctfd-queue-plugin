"""Unit tests for the CTFd-independent queue configuration validator."""
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "validation.py"
SPEC = importlib.util.spec_from_file_location("queue_validation", MODULE_PATH)
validation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation)


def normalize(data, partial=False):
    return validation.normalize_queue_fields(data, ValueError, partial=partial)


class NormalizeQueueFieldsTests(unittest.TestCase):
    def test_create_defaults_are_stable(self):
        result = normalize({})
        self.assertEqual(result["queue_mode"], "manual")
        self.assertEqual(result["function"], "static")
        self.assertEqual(result["batch_size"], 1)
        self.assertEqual(result["interval_seconds"], 900)
        self.assertIs(result["show_position"], True)
        self.assertIs(result["show_eta"], True)
        self.assertEqual(
            result["selected_message"],
            "You have been selected! You may now attempt this challenge.",
        )

    def test_empty_form_keys_are_removed_without_dropping_native_fields(self):
        result = normalize({"": "ignored", "  ": "ignored", "name": "My queue"})
        self.assertNotIn("", result)
        self.assertNotIn("  ", result)
        self.assertEqual(result["name"], "My queue")

    def test_enum_values_are_normalized(self):
        result = normalize({
            "queue_mode": " AUTOMATIC ",
            "function": " LINEAR ",
        })
        self.assertEqual(result["queue_mode"], "automatic")
        self.assertEqual(result["function"], "linear")

    def test_invalid_enum_values_are_rejected(self):
        for payload in (
            {"queue_mode": "random"},
            {"function": "exponential"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize(payload)

    def test_numeric_limits_are_inclusive(self):
        result = normalize({
            "batch_size": "1000",
            "interval_seconds": 86400,
        })
        self.assertEqual(result["batch_size"], 1000)
        self.assertEqual(result["interval_seconds"], 86400)

    def test_numeric_values_outside_limits_are_rejected(self):
        for payload in (
            {"batch_size": 0},
            {"batch_size": 1001},
            {"interval_seconds": 0},
            {"interval_seconds": 86401},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize(payload)

    def test_boolean_and_fractional_numbers_are_rejected_for_integer_fields(self):
        for payload in (
            {"batch_size": True},
            {"batch_size": 1.5},
            {"interval_seconds": False},
            {"interval_seconds": 1.25},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize(payload)

    def test_partial_update_only_normalizes_supplied_fields(self):
        self.assertEqual(normalize({"name": "Renamed"}, partial=True), {"name": "Renamed"})
        result = normalize({"queue_mode": "AUTOMATIC"}, partial=True)
        self.assertEqual(result, {"queue_mode": "automatic"})

    def test_boolean_strings_are_normalized(self):
        result = normalize({
            "show_position": "false",
            "show_eta": "yes",
        })
        self.assertIs(result["show_position"], False)
        self.assertIs(result["show_eta"], True)

    def test_selected_message_accepts_text_and_enforces_length(self):
        self.assertEqual(normalize({"selected_message": "Ready"})["selected_message"], "Ready")
        self.assertEqual(normalize({"selected_message": None})["selected_message"], "")
        with self.assertRaises(ValueError):
            normalize({"selected_message": "x" * 4001})
        with self.assertRaises(ValueError):
            normalize({"selected_message": {"html": "<script>"}})


if __name__ == "__main__":
    unittest.main()
