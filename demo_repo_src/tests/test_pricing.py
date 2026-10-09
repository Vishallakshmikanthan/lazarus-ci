from app.pricing import total, apply_discount


def test_total():
    assert total([(10, 2), (5, 1)]) == 25


def test_discount():
    assert apply_discount(200, 10) == 180.0
