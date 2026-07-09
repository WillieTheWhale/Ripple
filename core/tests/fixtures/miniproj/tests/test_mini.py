from miniproj.math_ops import normalize
from miniproj.service import compute_label, compute_value


def test_compute_value() -> None:
    assert compute_value(3) == normalize(3)


def test_label() -> None:
    assert compute_label(2) == "4"
