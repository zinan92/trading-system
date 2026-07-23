from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gridmind_exposes_dca_as_a_distinct_paper_strategy() -> None:
    html = (ROOT / "dashboard-gridmind.html").read_text()

    assert 'data-strategy-type="grid"' in html
    assert 'data-strategy-type="dca"' in html
    assert 'aria-pressed="true"' in html
    assert 'aria-pressed="false"' in html
    assert '✓ 当前选择' in html
    assert 'id="dcaTarget"' in html
    assert 'id="dcaStop"' in html
    assert 'loop_enabled:false' in html
    assert 'schema_version:"dca-risk-ack-v1"' not in html
    assert 'manual.schema_version||"grid-range-risk-ack-v1"' in html
    assert "新增成交后整轮止盈单会更新为累计数量" in html
    assert "DCA 使用右侧加仓区间参数，不支持拖动网格" in html


def test_gridmind_shows_position_first_ai_framework_before_strategy_adoption() -> None:
    html = (ROOT / "dashboard-gridmind.html").read_text()

    assert "① 长期位置" in html
    assert "② 趋势阶段" in html
    assert "③ 策略与参数" in html
    assert "recommended_strategy_type" in html
    assert "确定性预览" in html
    assert "不会下单" in html
