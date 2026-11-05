"""Invoice rendering."""

from .billing import calc_total


def render_invoice(customer, items, tax_rate=0.0):
    total = calc_total(items, tax_rate=tax_rate)
    return "Invoice for {}: {:.2f}".format(customer, total)
