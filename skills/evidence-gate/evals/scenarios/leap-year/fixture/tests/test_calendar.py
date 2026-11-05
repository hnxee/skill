import unittest

from calendar_utils import days_in_month, is_leap


class TestIsLeap(unittest.TestCase):
    def test_common_years(self):
        self.assertFalse(is_leap(2023))
        self.assertTrue(is_leap(2024))


class TestDaysInMonth(unittest.TestCase):
    def test_february(self):
        self.assertEqual(days_in_month(2023, 2), 28)
        self.assertEqual(days_in_month(2024, 2), 29)

    def test_millennium_february(self):
        self.assertEqual(days_in_month(2000, 2), 29)

    def test_thirty_day_months(self):
        self.assertEqual(days_in_month(2024, 4), 30)
