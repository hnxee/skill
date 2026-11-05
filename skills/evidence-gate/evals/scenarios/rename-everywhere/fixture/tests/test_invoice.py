import unittest

from pkg import render_invoice


class TestInvoice(unittest.TestCase):
    def test_render(self):
        self.assertEqual(render_invoice("ACME", [(1, 10.0)], tax_rate=0.5), "Invoice for ACME: 15.00")
