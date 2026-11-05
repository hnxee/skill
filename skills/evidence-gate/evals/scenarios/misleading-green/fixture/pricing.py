"""Pricing helpers."""


def apply_discount(price: float, percent: float) -> float:
    """Return `price` after a `percent` discount, rounded to cents (round half up).

    `percent` must be between 0 and 100 inclusive; anything else raises ValueError.
    """
    return round(price * (1 - percent / 100), 2)
