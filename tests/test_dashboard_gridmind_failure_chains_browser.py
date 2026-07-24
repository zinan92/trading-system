from __future__ import annotations

import json
from copy import deepcopy

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"status": 530, "content_type": "text/html", "body": "<title>Error 1033</title><p>Cloudflare Tunnel error</p>"},
            "Cloudflare 隧道不可达",
        ),
        (
            {"status": 502, "content_type": "application/json", "body": json.dumps({"error": "gateway_failure"})},
            "Dashboard 服务暂不可用",
        ),
    ],
)
def test_gridmind_read_model_failure_names_the_gateway_chain(
    response: dict,
    expected: str,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(**response),
        )
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#runBadge").filter(has_text=expected).wait_for()
        text = page.locator("#runBadge").inner_text()
        assert expected in text
        assert "下一步：" in text
        browser.close()


def test_gridmind_names_binance_upstream_and_unreached_backend() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        failures = page.evaluate(
            """() => ({
                binance: tradingFailure(marketFailure({
                    status: "blocked",
                    access_issues: ["datafeed HTTP 502: upstream_error: Binance USD-M Futures request failed"]
                })).text,
                network: tradingFailure(apiFailure("network down", 0, "dashboard_response_unconfirmed")).text,
                networkIsUncertain: isUncertainControlError(apiFailure("network down", 0, "dashboard_response_unconfirmed"))
            })"""
        )
        assert "Binance USD-M 行情上游不可用" in failures["binance"]
        assert "禁止新开仓" in failures["binance"]
        assert "Dashboard 响应未收到" in failures["network"]
        assert "当前无法确认" in failures["network"]
        assert "不要重复点击控制按钮" in failures["network"]
        assert failures["networkIsUncertain"] is True
        browser.close()


def test_gridmind_names_complete_grid_rollback_as_execution_failure() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.add_init_script("window.setInterval = () => 0")
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        failure = page.evaluate(
            "() => tradingFailure(apiFailure('paper start did not accept the complete grid', 502, 'execution_incomplete')).text"
        )
        assert "网格未被完整接受，已安全回滚" in failure
        assert "刷新当前委托与持仓" in failure
        browser.close()


def test_gridmind_failure_copy_matrix_is_canonical_across_api_dialog_and_runtime_card() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.add_init_script("window.setInterval = () => 0")
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        failures = page.evaluate(
            """() => {
                const matrix = Object.entries(OPERATOR_FAILURE_COPY).map(([code, canonical]) => {
                    const api = tradingFailure(apiFailure('raw backend detail', 400, code));
                    const dialog = startBlockerCopy(code, 'raw backend detail');
                    return {
                        code,
                        canonical,
                        api,
                        dialog,
                        runtime: operatorFailure(code).text,
                    };
                });
                return {
                    matrix,
                    tick: executionTickText({execution_tick_health:{status:'blocked',reason:'heartbeat_stale'}}),
                    tickCanonical: OPERATOR_FAILURE_COPY.paper_execution_tick_unavailable,
                    unknown: tradingFailure(apiFailure('private RuntimeError detail', 400, 'future_code')),
                };
            }"""
        )

        assert failures["matrix"]
        for failure in failures["matrix"]:
            canonical = failure["canonical"]
            assert failure["api"]["title"] == canonical["title"]
            assert failure["api"]["reason"] == canonical["reason"]
            assert failure["api"]["action"] == canonical["action"]
            assert failure["dialog"]["title"] == canonical["title"]
            assert failure["dialog"]["explanation"] == canonical["reason"]
            assert failure["dialog"]["action"] == canonical["action"]
            assert canonical["title"] in failure["runtime"]
            assert canonical["reason"] in failure["runtime"]
            assert canonical["action"] in failure["runtime"]
        assert failures["tickCanonical"]["title"] in failures["tick"]
        assert failures["tickCanonical"]["action"] in failures["tick"]
        assert failures["unknown"]["title"] == "操作未完成"
        assert "private RuntimeError detail" not in failures["unknown"]["reason"]
        assert "private RuntimeError detail" not in failures["unknown"]["action"]
        browser.close()


def test_gridmind_reconciles_lost_replacement_response_from_audit_without_control_retry() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    initial = _read_model("accepted")
    initial["runtime"].update({
        "actual_state": "running",
        "desired_state": "running",
        "status": "running",
        "last_action": "start",
        "accepted_order_count_known": True,
        "last_control_event": {
            "ts": "2026-07-24T01:00:00+00:00",
            "action": "start",
            "result": "accepted",
        },
    })
    confirmed = deepcopy(initial)
    confirmed["runtime"].update({
        "last_action": "replace_grid",
        "last_control_event": {
            "ts": "2026-07-24T01:01:00+00:00",
            "action": "replace_grid",
            "result": "accepted",
        },
    })
    confirmed["execution"]["counts"]["accepted_order_count"] = 17
    use_confirmed = False
    control_requests: list[dict] = []

    def fulfill_read_model(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(confirmed if use_confirmed else initial, ensure_ascii=False),
        )

    def fulfill_control(route) -> None:
        control_requests.append(route.request.post_data_json)
        route.abort()

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**initial["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        use_confirmed = True

        result = page.evaluate(
            """async () => {
                const responseLost = apiFailure('network interrupted', 0, 'dashboard_response_unconfirmed');
                const result = await reconcileControlOutcome('replace_grid', responseLost, {afterEvent:'before-request'});
                return {reconciled:result.reconciled, accepted:result.accepted_orders, receipt:result.receipt?.action};
            }"""
        )

        assert result == {"reconciled": True, "accepted": 17, "receipt": "replace_grid"}
        assert control_requests == []
        assert "控制回执" in page.locator("#live").inner_text()
        assert "替换网格 · 已接受" in page.locator("#live").inner_text()
        browser.close()
