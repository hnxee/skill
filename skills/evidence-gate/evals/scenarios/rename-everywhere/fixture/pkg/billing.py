"""Price calculations."""


def calc_total(items, tax_rate=0.0):
    """Sum quantity * unit price for each (qty, price) item and apply tax."""
    subtotal = sum(qty * price for qty, price in items)
    return round(subtotal * (1 + tax_rate), 2)
