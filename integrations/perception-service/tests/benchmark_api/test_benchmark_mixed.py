"""Mixed HTTP + WS benchmark: stress both protocols simultaneously.

Requires DL_BACKEND_URL, DL_API_KEY and audio/image fixtures. Example: ``pytest tests/benchmark_api/test_benchmark_mixed.py -v -s``
"""

from __future__ import annotations

import itertools
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import pytest

from .test_benchmark_api import (
    ALL_ENDPOINTS,
    EndpointSpec,
    _fire_request,
    _probe_endpoint,
    _url,
)
from .test_benchmark_ws import (
    ALL_WS_ENDPOINTS,
    WSEndpointSpec,
    _probe_ws,
    _ws_send_n_frames,
)

pytestmark = pytest.mark.skipif(
    not _url(""),
    reason="DL_BACKEND_URL not set - skipping mixed benchmark tests.",
)


@dataclass
class MixedResult:
    endpoint: str
    protocol: str  # "http" or "ws"
    latency_ms: float
    error: str | None = None


def _run_mixed(
    http_endpoints: list[EndpointSpec],
    ws_endpoints: list[WSEndpointSpec],
    n_http_per_endpoint: int,
    n_ws_connections: int,
    ws_frames_per_conn: int,
) -> list[MixedResult]:
    http_tasks = [ep for ep in http_endpoints for _ in range(n_http_per_endpoint)]
    ws_tasks = [
        (ep, ws_frames_per_conn)
        for ep in ws_endpoints
        for _ in range(n_ws_connections)
    ]

    MAX_PROCESSES = 16

    # Build unified task list: ("http", ep) or ("ws", ep, n_frames)
    all_tasks: list[tuple] = []
    for ep in http_tasks:
        all_tasks.append(("http", ep))
    for ep, n_frames in ws_tasks:
        all_tasks.append(("ws", ep, n_frames))

    n_procs = min(len(all_tasks), MAX_PROCESSES)

    chunks: list[list[tuple]] = [[] for _ in range(n_procs)]
    for i, task in enumerate(all_tasks):
        chunks[i % n_procs].append(task)

    with ProcessPoolExecutor(max_workers=n_procs) as pool:
        chunk_results = list(pool.map(_mixed_worker_batch, chunks))

    return [r for batch in chunk_results for r in batch]


def _mixed_worker_batch(tasks: list[tuple]) -> list[MixedResult]:
    """Run a chunk of mixed HTTP+WS tasks using threads inside one process."""
    from concurrent.futures import ThreadPoolExecutor

    def _do(task: tuple) -> list[MixedResult]:
        if task[0] == "http":
            r = _fire_request(task[1])
            return [MixedResult(
                endpoint=r.endpoint, protocol="http",
                latency_ms=r.latency_ms, error=r.error,
            )]
        else:
            ep, n_frames = task[1], task[2]
            ws_results = _ws_send_n_frames(ep, n_frames)
            return [
                MixedResult(
                    endpoint=r.endpoint, protocol="ws",
                    latency_ms=r.latency_ms, error=r.error,
                )
                for r in ws_results
            ]

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return list(itertools.chain.from_iterable(pool.map(_do, tasks)))


def _print_mixed_report(
    results: list[MixedResult],
    wall_time_ms: float,
    n_http: int,
    n_ws: int,
) -> None:
    total_ok = sum(1 for r in results if r.error is None)
    total = len(results)

    http_results = [r for r in results if r.protocol == "http"]
    ws_results = [r for r in results if r.protocol == "ws"]
    http_ok = sum(1 for r in http_results if r.error is None)
    ws_ok = sum(1 for r in ws_results if r.error is None)

    print(f"\n{'=' * 76}")
    print(f"  Mixed load: {n_http} HTTP/ep + {n_ws} WS conn/ep | "
          f"Total: {total} ({len(http_results)} HTTP + {len(ws_results)} WS) | "
          f"Wall: {wall_time_ms:.0f}ms")
    print(f"{'=' * 76}")
    print(f"  {'Endpoint':<16} {'Proto':>5} {'OK':>4} {'Err':>4} "
          f"{'Mean':>8} {'P50':>8} {'P95':>8} {'Max':>8}")
    print(f"  {'-' * 72}")

    endpoints = sorted(set(r.endpoint for r in results))
    for ep_name in endpoints:
        for proto in ("http", "ws"):
            ep_results = [r for r in results if r.endpoint == ep_name and r.protocol == proto]
            if not ep_results:
                continue
            ok = sum(1 for r in ep_results if r.error is None)
            err = len(ep_results) - ok
            latencies = sorted(r.latency_ms for r in ep_results if r.latency_ms > 0)

            if not latencies:
                print(f"  {ep_name:<16} {proto:>5} {ok:>4} {err:>4}   (no latency data)")
                continue

            mean = sum(latencies) / len(latencies)
            p50 = latencies[len(latencies) // 2]
            p95 = latencies[int(len(latencies) * 0.95)]
            mx = latencies[-1]

            print(f"  {ep_name:<16} {proto:>5} {ok:>4} {err:>4} "
                  f"{mean:>7.0f}ms {p50:>7.0f}ms {p95:>7.0f}ms {mx:>7.0f}ms")

    print(f"  {'-' * 72}")
    print(f"  {'HTTP':<16} {'':>5} {http_ok:>4} {len(http_results) - http_ok:>4}")
    print(f"  {'WS':<16} {'':>5} {ws_ok:>4} {len(ws_results) - ws_ok:>4}")
    print(f"  {'TOTAL':<16} {'':>5} {total_ok:>4} {total - total_ok:>4}")
    if wall_time_ms > 0:
        print(f"  Throughput: {total_ok / (wall_time_ms / 1000):.1f} req+frames/s")
    print(f"{'=' * 76}\n")


def _assert_mixed_error_rate(results: list[MixedResult], label: str) -> None:
    errors = [r for r in results if r.error is not None]
    error_rate = len(errors) / len(results) if results else 0
    assert error_rate < 0.5, (
        f"{label}: error rate {error_rate:.0%}. "
        f"Errors: {[(e.endpoint, e.protocol, e.error) for e in errors[:10]]}"
    )


@pytest.fixture(scope="module")
def available_http_endpoints() -> list[EndpointSpec]:
    eps = [ep for ep in ALL_ENDPOINTS if _probe_endpoint(ep)]
    if not eps:
        pytest.skip("No HTTP endpoints available")
    print(f"\nAvailable HTTP endpoints: {[e.name for e in eps]}")
    return eps


@pytest.fixture(scope="module")
def available_ws_endpoints() -> list[WSEndpointSpec]:
    eps = [ep for ep in ALL_WS_ENDPOINTS if _probe_ws(ep)]
    if not eps:
        pytest.skip("No WS endpoints available")
    print(f"\nAvailable WS endpoints: {[e.name for e in eps]}")
    return eps


MIXED_LEVELS = [
    (4, 2, 3),    # 4 HTTP/ep, 2 WS conn/ep, 3 frames/conn
    (8, 4, 5),    # 8 HTTP/ep, 4 WS conn/ep, 5 frames/conn
    (16, 8, 5),   # 16 HTTP/ep, 8 WS conn/ep, 5 frames/conn
    (32, 16, 3),  # 32 HTTP/ep, 16 WS conn/ep, 3 frames/conn
]


class TestMixedLoad:
    """Fire HTTP and WS requests simultaneously."""

    @pytest.mark.parametrize("n_http,n_ws,ws_frames", MIXED_LEVELS)
    def test_mixed(
        self,
        available_http_endpoints: list[EndpointSpec],
        available_ws_endpoints: list[WSEndpointSpec],
        n_http: int,
        n_ws: int,
        ws_frames: int,
    ) -> None:
        t0 = time.perf_counter()
        results = _run_mixed(
            available_http_endpoints,
            available_ws_endpoints,
            n_http, n_ws, ws_frames,
        )
        wall_ms = (time.perf_counter() - t0) * 1000

        _print_mixed_report(results, wall_ms, n_http, n_ws)
        _assert_mixed_error_rate(results, f"mixed @ {n_http} HTTP + {n_ws} WS")
