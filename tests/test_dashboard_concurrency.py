from __future__ import annotations

import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen

import pytest

import pipelines.dashboard_server as dashboard_server


def test_identical_read_model_requests_share_one_in_flight_build(monkeypatch) -> None:
    worker_count = 8
    flight = dashboard_server.KeyedSingleFlight()
    workers_ready = threading.Barrier(worker_count + 1)
    build_started = threading.Event()
    release_build = threading.Event()
    build_count = 0
    build_count_lock = threading.Lock()

    def fake_build(*, as_of=None):
        nonlocal build_count
        with build_count_lock:
            build_count += 1
        build_started.set()
        assert release_build.wait(timeout=3)
        return {"as_of": as_of, "runtime": {"state": "running"}}

    def request_read_model():
        workers_ready.wait(timeout=3)
        return dashboard_server.build_trading_system_read_model_response_singleflight(
            "as_of=2026-08-03T00%3A00%3A00Z",
            single_flight=flight,
        )

    monkeypatch.setattr(
        dashboard_server,
        "build_trading_system_read_model_response",
        fake_build,
    )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(request_read_model) for _ in range(worker_count)]
        workers_ready.wait(timeout=3)
        assert build_started.wait(timeout=3)
        time.sleep(0.05)
        release_build.set()
        results = [future.result(timeout=3) for future in futures]

    assert build_count == 1
    assert results == [results[0]] * worker_count


def test_completed_read_model_flight_does_not_hide_control_state_change(monkeypatch) -> None:
    flight = dashboard_server.KeyedSingleFlight()
    authoritative_state = {"runtime": "stopped"}
    build_count = 0

    def fake_build(*, as_of=None):
        nonlocal build_count
        build_count += 1
        return {"runtime": authoritative_state["runtime"], "as_of": as_of}

    monkeypatch.setattr(
        dashboard_server,
        "build_trading_system_read_model_response",
        fake_build,
    )

    before_control = dashboard_server.build_trading_system_read_model_response_singleflight(
        "",
        single_flight=flight,
    )
    authoritative_state["runtime"] = "running"
    after_control = dashboard_server.build_trading_system_read_model_response_singleflight(
        "",
        single_flight=flight,
    )

    assert before_control["runtime"] == "stopped"
    assert after_control["runtime"] == "running"
    assert build_count == 2


def test_control_generations_isolate_pre_control_and_during_control_flights(monkeypatch) -> None:
    flight = dashboard_server.KeyedSingleFlight()
    authoritative_state = {"runtime": "stopped"}
    pre_control_started = threading.Event()
    during_control_started = threading.Event()
    release_old_flights = threading.Event()
    build_count = 0

    def fake_build(*, as_of=None):
        nonlocal build_count
        build_count += 1
        observed = authoritative_state["runtime"]
        if observed == "stopped":
            pre_control_started.set()
            assert release_old_flights.wait(timeout=3)
        elif observed == "starting":
            during_control_started.set()
            assert release_old_flights.wait(timeout=3)
        return {"runtime": observed, "as_of": as_of}

    def load():
        return dashboard_server.build_trading_system_read_model_response_singleflight(
            "",
            single_flight=flight,
        )

    monkeypatch.setattr(
        dashboard_server,
        "build_trading_system_read_model_response",
        fake_build,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        pre_control = executor.submit(load)
        assert pre_control_started.wait(timeout=3)

        flight.advance_generation()
        authoritative_state["runtime"] = "starting"
        during_control = executor.submit(load)
        assert during_control_started.wait(timeout=3)

        authoritative_state["runtime"] = "running"
        flight.advance_generation()
        post_control = load()
        release_old_flights.set()

        assert pre_control.result(timeout=3)["runtime"] == "stopped"
        assert during_control.result(timeout=3)["runtime"] == "starting"

    assert post_control["runtime"] == "running"
    assert build_count == 3


def test_strategy_control_advances_read_model_generation_before_and_after(monkeypatch) -> None:
    events = []

    class GenerationSpy:
        def advance_generation(self):
            events.append("advance")

    monkeypatch.setattr(
        dashboard_server,
        "_TRADING_SYSTEM_READ_MODEL_SINGLE_FLIGHT",
        GenerationSpy(),
    )
    monkeypatch.setattr(
        dashboard_server,
        "_dualtrack_mutation_request_allowed",
        lambda _host, _origin: True,
    )

    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.path = "/api/strategy-console/control"
    handler.headers = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}
    handler._handle_strategy_console_control = types.MethodType(
        lambda _self: events.append("control"),
        handler,
    )

    handler.do_POST()

    assert events == ["advance", "control", "advance"]


def test_failed_flight_is_removed_for_a_later_request() -> None:
    flight = dashboard_server.KeyedSingleFlight()
    attempts = 0

    def compute():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("temporary read-model failure")
        return {"ok": True}

    with pytest.raises(ValueError, match="temporary read-model failure"):
        flight.run("same-request", compute)

    assert flight.run("same-request", compute) == {"ok": True}
    assert attempts == 2


def test_bounded_server_queues_requests_before_spawning_more_threads() -> None:
    concurrency_limit = 2
    request_count = 6
    release_handlers = threading.Event()
    saturated = threading.Event()
    state_lock = threading.Lock()
    active_handlers = 0
    maximum_active_handlers = 0

    class BlockingHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            nonlocal active_handlers, maximum_active_handlers
            with state_lock:
                active_handlers += 1
                maximum_active_handlers = max(maximum_active_handlers, active_handlers)
                if active_handlers == concurrency_limit:
                    saturated.set()
            try:
                assert release_handlers.wait(timeout=5)
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with state_lock:
                    active_handlers -= 1

        def log_message(self, _format, *_args):
            return

    server = dashboard_server.BoundedThreadingHTTPServer(
        ("127.0.0.1", 0),
        BlockingHandler,
        max_concurrent_requests=concurrency_limit,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"

    def fetch() -> bytes:
        with urlopen(url, timeout=5) as response:  # noqa: S310 - loopback test server
            return response.read()

    try:
        with ThreadPoolExecutor(max_workers=request_count) as executor:
            futures = [executor.submit(fetch) for _ in range(request_count)]
            assert saturated.wait(timeout=3)
            time.sleep(0.1)
            with state_lock:
                assert active_handlers == concurrency_limit
                assert maximum_active_handlers == concurrency_limit
            release_handlers.set()
            assert [future.result(timeout=5) for future in futures] == [b"ok"] * request_count
    finally:
        release_handlers.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)


def test_bounded_server_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        dashboard_server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            BaseHTTPRequestHandler,
            max_concurrent_requests=0,
        )
