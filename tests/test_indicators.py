import pytest

from services.indicators import ema, macd


def test_ema_of_constant_is_constant():
    assert ema([5.0] * 10, 4) == [5.0] * 10


def test_ema_tracks_a_rising_series_monotonically():
    out = ema([1, 2, 3, 4, 5, 6, 7, 8], 3)
    assert out[0] == 1.0
    assert all(b >= a for a, b in zip(out, out[1:]))  # non-decreasing on a rising input
    assert out[-1] < 8.0  # lags the latest value (it's an average)


def test_macd_crosses_sign_when_trend_flips():
    # Rising then falling closes -> histogram goes positive then negative (a cross).
    closes = [100 + i for i in range(40)] + [140 - i for i in range(40)]
    _macd_line, _signal, hist = macd(closes, fast=12, slow=26, signal=9)
    assert max(hist) > 0 and min(hist) < 0  # both a golden and a death cross occur


def test_macd_rejects_bad_periods():
    with pytest.raises(ValueError):
        macd([1, 2, 3], fast=26, slow=12)
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)
