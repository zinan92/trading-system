"""Every number an AI line states is checked against what it was given (AI 透明度 #1318)."""
from __future__ import annotations

from trading_desk.provenance import numbers, unverified


def test_numbers_read_units_and_separators() -> None:
    assert [round(n.value, 6) for n in numbers("现价 4,132.6，预期 65K，6.5万人，3.1%，2亿")] == [4132.6, 65000, 65000, 3.1, 200000000]
    assert [n.text for n in numbers("跌破50日均线")] == ["50"]


def test_same_value_in_another_unit_or_precision_is_found() -> None:
    assert unverified("市场押注新增就业6.5万", ["预期 65K · 前值 22K"]) == []
    assert unverified("押注加息至4%", ["预期 4.00% · 前值 3.75%"]) == []
    assert unverified("站上4132.6", ["price 4,132.60"]) == []


def test_basis_points_may_be_the_gap_between_two_given_rates() -> None:
    assert unverified("市场押注加息25基点", ["预期 4.00% · 前值 3.75%"]) == []
    assert unverified("加息 50bp", ["预期 4.00% · 前值 3.75%"]) == ["50bp"]


def test_a_number_not_in_the_inputs_is_named_once() -> None:
    assert unverified("市场押注9月与年末加息，9月", ["预期 4.00% · 前值 3.75%"]) == ["9"]
    assert unverified("失业率4.2%", ["预期 65K"]) == ["4.2%"]


def test_a_percent_is_not_matched_by_a_bare_number_and_text_without_numbers_is_clean() -> None:
    assert unverified("涨了3%", ["第 3 次会议"]) == ["3%"]
    assert unverified("日线弱势与隔夜贵金属大涨冲突", []) == []
