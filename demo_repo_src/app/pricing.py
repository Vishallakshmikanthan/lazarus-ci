def total(items):
    """items: list of (price, qty)"""
    return sum(p * q for p, q in items)


def apply_discount(amount, pct):
    return round(amount * (1 - pct / 100), 2)
