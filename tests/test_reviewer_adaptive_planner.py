"""Independent contract tests, written after Flash's frozen planner handoff."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lecture_materials.adaptive import (
    frame_interval,
    plan_refinement,
    validate_refinement_level,
)


class AdaptivePlannerCase(unittest.TestCase):
    def test_baseline_interval_defaults_and_manual_ranges(self):
        for supplied, expected in ((None, 15), (5, 5), ("10", 10), (30, 30)):
            with self.subTest(supplied=supplied):
                self.assertEqual(frame_interval(supplied), expected)
        for supplied in (True, False, 1, 0, -5, 4.999, "bad", float("nan"), float("inf")):
            with self.subTest(rejected=supplied):
                with self.assertRaises((TypeError, ValueError)):
                    frame_interval(supplied)

    def test_recovery_is_interior_and_reuses_requested_times(self):
        cases = (
            ((0, 15, 10, [0, 15]), [10]),
            ((0, 15, 5, [0, 10, 15]), [5]),
            ((40, 55, 10, [40, 55]), [50]),
            ((40, 55, 5, [40, 50, 55]), [45]),
            ((30, 41.25, 5, [30, 40, 41.25]), [35]),
            ((30, 40, 10, [30, 40]), []),
            ((30, 40, 5, [30, 40]), [35]),
            ((30, 34.1, 5, [30, 34.1]), []),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(plan_refinement(*arguments), expected)

    def test_request_reuse_tolerance_stays_absolute_in_long_videos(self):
        # A previous relative-tolerance implementation lost requests after hours.
        for delta, expected in ((0.0000005, []), (0.000003, [12010])):
            with self.subTest(delta=delta):
                self.assertEqual(
                    plan_refinement(12000, 12015, 10, [12010 + delta]), expected
                )

    def test_invalid_recovery_windows_and_existing_values_fail(self):
        cases = (
            (-1, 14, 10, []), (0, 16, 10, []), (15, 15, 10, []),
            (False, 15, 10, []), (0, float("nan"), 10, []),
            (0, 15, 1, []), (0, 15, 10.0000005, []),
            (0, 15, True, []), (0, 15, 5, [float("inf")]),
            (0, 15, 5, [False]), (0, 15, 5, ["bad"]),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    plan_refinement(*arguments)

    def test_completed_levels_allow_retries_and_ordered_progression(self):
        for completed, requested in (
            ([], 10), ([10], 10), ([10], 5), ([10, 5], 5),
            ([10, 5], 10), ([10, 10], 5), ([10, 5, 10], 5),
        ):
            with self.subTest(completed=completed, requested=requested):
                self.assertIsNone(validate_refinement_level(completed, requested))

    def test_invalid_completed_history_cannot_authorize_five_seconds(self):
        for completed, requested in (
            ([], 5), ([5], 10), ([5, 10], 5), ([10, 7], 5),
            ([True], 10), ([10, float("nan")], 5), ([10], 1),
        ):
            with self.subTest(completed=completed, requested=requested):
                with self.assertRaises((TypeError, ValueError)):
                    validate_refinement_level(completed, requested)


if __name__ == "__main__":
    unittest.main()
