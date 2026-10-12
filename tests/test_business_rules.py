import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.main import (
    EmployeeIn,
    PunchInIn,
    RegularizeIn,
    attendance_day,
    late_minutes,
    overtime_minutes,
    trend_pipeline,
    work_hours,
)


UTC = timezone.utc


class BusinessRuleTests(unittest.TestCase):
    def test_grace_boundary_and_whole_minutes(self):
        day = "2026-07-06"
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC), day, "09:30"), 0)
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC), day, "09:30"), 10)
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 45, 59, tzinfo=UTC), day, "09:30"), 45)

    def test_overnight_attendance_day(self):
        self.assertEqual(attendance_day(datetime(2026, 7, 7, 0, 0, tzinfo=UTC), "22:00", "06:00"), "2026-07-06")

    def test_overtime_threshold_and_overnight_end(self):
        self.assertEqual(overtime_minutes(datetime(2026, 7, 6, 13, 29, tzinfo=UTC), "2026-07-06", "09:30", "18:30"), 0)
        self.assertEqual(overtime_minutes(datetime(2026, 7, 6, 13, 30, tzinfo=UTC), "2026-07-06", "09:30", "18:30"), 30)
        self.assertEqual(overtime_minutes(datetime(2026, 7, 7, 1, 10, tzinfo=UTC), "2026-07-06", "22:00", "06:00"), 40)

    def test_work_hours_round_half_up(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(work_hours(start, datetime(2026, 1, 1, 4, 29, 42, tzinfo=UTC)), 4.5)

    def test_seconds_looking_timestamp_is_rejected(self):
        with self.assertRaises(ValidationError):
            PunchInIn(emp_code="EMP0001", punched_at=1_783_312_500)

    def test_explicit_null_timestamp_is_rejected(self):
        with self.assertRaises(ValidationError):
            PunchInIn(emp_code="EMP0001", punched_at=None)
        with self.assertRaises(ValidationError):
            RegularizeIn(reason="valid reason", regularized_by="hr", punch_in=None)

    def test_equal_shift_times_are_rejected(self):
        with self.assertRaises(ValidationError):
            EmployeeIn(emp_code="EMP0001", name="A", email="a@example.com", department="Engineering",
                       shift_start="09:30", shift_end="09:30", joined_on="2026-01-01")

    def test_trend_is_generated_inside_aggregation(self):
        pipeline = trend_pipeline("Engineering", datetime(2026, 7, 1).date(), datetime(2026, 7, 7).date())
        self.assertTrue(any("$range" in str(stage) for stage in pipeline))
        self.assertTrue(any("$setWindowFields" in stage for stage in pipeline))


if __name__ == "__main__":
    unittest.main()
