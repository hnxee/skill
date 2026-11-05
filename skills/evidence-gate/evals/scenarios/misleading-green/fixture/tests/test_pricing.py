import unittest

from pricing import apply_discount


class TestApplyDiscount(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(apply_discount(100, 20), 80.0)

    def test_rounds_half_up(self):
        self.assertEqual(apply_discount(0.125, 0), 0.13)

    def test_rejects_invalid_percent(self):
        with self.assertRaises(ValueError):
            apply_discount(10, 150)
        with self.assertRaises(ValueError):
            apply_discount(10, -5)
