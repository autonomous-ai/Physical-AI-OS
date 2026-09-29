"""WebSocket benchmark: concurrent connections with increasing load, per-frame latency.

Requires DL_BACKEND_URL, DL_API_KEY and image fixtures. Example: ``pytest tests/benchmark_api/test_benchmark_ws.py -v -s``
"""

from __future__ import annotations

import base64
import functools
import itertools
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import pytest
from dotenv import load_dotenv
from websockets.sync.client import connect as ws_connect

_ = load_dotenv(override=True)

DL_BACKEND_URL = os.getenv("DL_BACKEND_URL", "").rstrip("/")
DL_API_KEY = os.getenv("DL_API_KEY", "")

IMAGE_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "images"

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL,
    reason="DL_BACKEND_URL not set - skipping WS benchmark tests.",
)


def _ws_url(path: str) -> str:
    return DL_BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://") + path


@functools.cache
def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@functools.cache
def _load_image_b64(name: str = "person_drinking.jpg") -> str:
    path = IMAGE_FIXTURES / name
    if not path.exists():
        return _make_frame_b64()
    img = cv2.imread(str(path))
    if img is None:
        return _make_frame_b64()
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode()


ERROR_TIMEOUT = "timeout"
ERROR_CONNECTION = "connection"
ERROR_SERVER = "server"


@dataclass
class WSResult:
    endpoint: str
    latency_ms: float
    error: str | None = None
    error_type: str | None = None  # timeout | connection | server


@dataclass
class WSEndpointSpec:
    name: str
    path: str
    task: str
    frame_msg_fn: Callable[[], dict[str, Any]]
    frames_per_conn: int = 32


def _ws_frame_msg(task: str) -> dict[str, Any]:
    return {"type": "frame", "task": task, "frame_b64": _make_frame_b64()}


def _pose_frame_msg() -> dict[str, Any]:
    return _ws_frame_msg("pose")


def _fer_frame_msg() -> dict[str, Any]:
    return _ws_frame_msg("emotion")


def _action_frame_msg() -> dict[str, Any]:
    return _ws_frame_msg("action")


ALL_WS_ENDPOINTS: list[WSEndpointSpec] = [
    WSEndpointSpec(
        name="ws_pose",
        path="/hal/api/dl/pose-estimation/ws",
        task="pose",
        frame_msg_fn=_pose_frame_msg,
        frames_per_conn=243,
    ),
    WSEndpointSpec(
        name="ws_fer",
        path="/hal/api/dl/emotion-analysis/ws",
        task="emotion",
        frame_msg_fn=_fer_frame_msg,
    ),
    WSEndpointSpec(
        name="ws_action",
        path="/hal/api/dl/action-analysis/ws",
        task="action",
        frame_msg_fn=_action_frame_msg,
    ),
]

AUTH_WS_HEADERS: dict[str, str] = {"X-API-Key": DL_API_KEY} if DL_API_KEY else {}


def _probe_ws(ep: WSEndpointSpec) -> bool:
    try:
        with ws_connect(
            _ws_url(ep.path),
            additional_headers=AUTH_WS_HEADERS,
            open_timeout=10,
        ) as ws:
            ws.send(json.dumps({"type": "heartbeat", "task": ep.task}))
            resp = json.loads(ws.recv(timeout=10))
            return resp.get("status") == "ok"
    except Exception:
        return False


def _ws_send_n_frames(ep: WSEndpointSpec, n_frames: int) -> list[WSResult]:
    """Send n_frames on a single WS connection in its own thread."""
    results: list[WSResult] = []
    try:
        with ws_connect(
            _ws_url(ep.path),
            additional_headers=AUTH_WS_HEADERS,
            open_timeout=15,
        ) as ws:
            for _ in range(n_frames):
                msg = ep.frame_msg_fn()
                t0 = time.perf_counter()
                ws.send(json.dumps(msg))
                raw = ws.recv(timeout=240)
                latency = (time.perf_counter() - t0) * 1000
                resp = json.loads(raw)
                if "error" in resp:
                    results.append(WSResult(
                        endpoint=ep.name, latency_ms=latency,
                        error=resp.get("error", "unknown"), error_type=ERROR_SERVER,
                    ))
                else:
                    results.append(WSResult(
                        endpoint=ep.name, latency_ms=latency,
                    ))
    except TimeoutError:
        results.append(WSResult(
            endpoint=ep.name, latency_ms=0,
            error="TimeoutError", error_type=ERROR_TIMEOUT,
        ))
    except (ConnectionError, OSError) as exc:
        results.append(WSResult(
            endpoint=ep.name, latency_ms=0,
            error=str(exc) or type(exc).__name__, error_type=ERROR_CONNECTION,
        ))
    except Exception as exc:
        results.append(WSResult(
            endpoint=ep.name, latency_ms=0,
            error=str(exc) or type(exc).__name__, error_type=ERROR_SERVER,
        ))
    return results


MAX_PROCESSES = 16


def _ws_worker_batch(
    tasks: list[tuple[WSEndpointSpec, int]],
) -> list[WSResult]:
    """Run a chunk of WS tasks inside one worker process using threads."""
    from concurrent.futures import ThreadPoolExecutor

    def _do(args: tuple[WSEndpointSpec, int]) -> list[WSResult]:
        return _ws_send_n_frames(args[0], args[1])

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return list(itertools.chain.from_iterable(pool.map(_do, tasks)))


def _ws_concurrent_connections(
    ep: WSEndpointSpec,
    n_connections: int,
    frames_per_conn: int | None = None,
) -> list[WSResult]:
    n_frames = frames_per_conn if frames_per_conn is not None else ep.frames_per_conn
    all_tasks = [(ep, n_frames) for _ in range(n_connections)]
    n_procs = min(len(all_tasks), MAX_PROCESSES)

    chunks: list[list[tuple[WSEndpointSpec, int]]] = [[] for _ in range(n_procs)]
    for i, task in enumerate(all_tasks):
        chunks[i % n_procs].append(task)

    with ProcessPoolExecutor(max_workers=n_procs) as pool:
        chunk_results = list(pool.map(_ws_worker_batch, chunks))
    return [r for batch in chunk_results for r in batch]


def _ws_all_endpoints_concurrent(
    endpoints: list[WSEndpointSpec],
    n_connections: int,
    frames_per_conn: int | None = None,
) -> list[WSResult]:
    all_tasks = [
        (ep, frames_per_conn if frames_per_conn is not None else ep.frames_per_conn)
        for ep in endpoints
        for _ in range(n_connections)
    ]
    n_procs = min(len(all_tasks), MAX_PROCESSES)

    chunks: list[list[tuple[WSEndpointSpec, int]]] = [[] for _ in range(n_procs)]
    for i, task in enumerate(all_tasks):
        chunks[i % n_procs].append(task)

    with ProcessPoolExecutor(max_workers=n_procs) as pool:
        chunk_results = list(pool.map(_ws_worker_batch, chunks))
    return [r for batch in chunk_results for r in batch]


def _print_ws_report(
    results: list[WSResult],
    n_connections: int,
    wall_time_ms: float,
) -> None:
    endpoints = sorted(set(r.endpoint for r in results))
    total_ok = sum(1 for r in results if r.error is None)
    total = len(results)

    print(f"\n{'=' * 72}")
    print(f"  WS Connections: {n_connections} per endpoint | "
          f"Total frames: {total} | "
          f"Wall time: {wall_time_ms:.0f}ms")
    print(f"{'=' * 72}")
    print(f"  {'Endpoint':<16} {'OK':>4} {'Err':>4} "
          f"{'Mean':>8} {'P50':>8} {'P95':>8} {'P99':>8} {'Max':>8}")
    print(f"  {'-' * 68}")

    for ep_name in endpoints:
        ep_results = [r for r in results if r.endpoint == ep_name]
        ok = sum(1 for r in ep_results if r.error is None)
        err = len(ep_results) - ok
        latencies = sorted(r.latency_ms for r in ep_results if r.latency_ms > 0)

        if not latencies:
            print(f"  {ep_name:<16} {ok:>4} {err:>4}   (no latency data)")
            continue

        mean = sum(latencies) / len(latencies)
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        mx = latencies[-1]

        print(f"  {ep_name:<16} {ok:>4} {err:>4} "
              f"{mean:>7.0f}ms {p50:>7.0f}ms {p95:>7.0f}ms {p99:>7.0f}ms {mx:>7.0f}ms")

    print(f"  {'-' * 68}")
    print(f"  {'TOTAL':<16} {total_ok:>4} {total - total_ok:>4}")
    if wall_time_ms > 0:
        print(f"  Throughput: {total_ok / (wall_time_ms / 1000):.1f} frames/s")
    print(f"{'=' * 72}\n")


def _assert_error_rate(results: list[WSResult], label: str) -> None:
    errors = [r for r in results if r.error is not None]
    error_rate = len(errors) / len(results) if results else 0
    assert error_rate < 0.5, (
        f"{label}: error rate {error_rate:.0%}. "
        f"Errors: {[(e.endpoint, e.error) for e in errors[:10]]}"
    )


@pytest.fixture(scope="module")
def available_ws_endpoints() -> list[WSEndpointSpec]:
    eps = [ep for ep in ALL_WS_ENDPOINTS if _probe_ws(ep)]
    if not eps:
        pytest.skip("No WebSocket endpoints available")
    print(f"\nAvailable WS endpoints: {[e.name for e in eps]}")
    return eps


def _make_single_ep_fixture(ep_name: str) -> Callable[..., WSEndpointSpec]:
    @pytest.fixture(scope="module")
    def _fixture(available_ws_endpoints: list[WSEndpointSpec]) -> WSEndpointSpec:
        for ep in available_ws_endpoints:
            if ep.name == ep_name:
                return ep
        pytest.skip(f"{ep_name} WS endpoint not available")
    return _fixture


ws_pose_endpoint = _make_single_ep_fixture("ws_pose")
ws_fer_endpoint = _make_single_ep_fixture("ws_fer")
ws_action_endpoint = _make_single_ep_fixture("ws_action")
ws_object_endpoint = _make_single_ep_fixture("ws_object")


WS_CONN_LEVELS = [1, 2, 4, 8, 16, 32, 64, 128]


class TestWSAllEndpoints:
    """Open multiple WS connections to all endpoints simultaneously."""

    @pytest.mark.parametrize("n_conn", WS_CONN_LEVELS)
    def test_scaling(
        self, available_ws_endpoints: list[WSEndpointSpec], n_conn: int
    ) -> None:
        t0 = time.perf_counter()
        results = _ws_all_endpoints_concurrent(available_ws_endpoints, n_conn)
        wall_ms = (time.perf_counter() - t0) * 1000

        _print_ws_report(results, n_conn, wall_ms)
        _assert_error_rate(results, f"all endpoints @ {n_conn} conn")


class TestWSPoseScaling:
    @pytest.mark.parametrize("n_conn", WS_CONN_LEVELS)
    def test_scaling(self, ws_pose_endpoint: WSEndpointSpec, n_conn: int) -> None:
        t0 = time.perf_counter()
        results = _ws_concurrent_connections(ws_pose_endpoint, n_conn)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_ws_report(results, n_conn, wall_ms)
        _assert_error_rate(results, f"ws_pose @ {n_conn} conn")


class TestWSFERScaling:
    @pytest.mark.parametrize("n_conn", WS_CONN_LEVELS)
    def test_scaling(self, ws_fer_endpoint: WSEndpointSpec, n_conn: int) -> None:
        t0 = time.perf_counter()
        results = _ws_concurrent_connections(ws_fer_endpoint, n_conn)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_ws_report(results, n_conn, wall_ms)
        _assert_error_rate(results, f"ws_fer @ {n_conn} conn")


class TestWSActionScaling:
    @pytest.mark.parametrize("n_conn", WS_CONN_LEVELS)
    def test_scaling(self, ws_action_endpoint: WSEndpointSpec, n_conn: int) -> None:
        t0 = time.perf_counter()
        results = _ws_concurrent_connections(ws_action_endpoint, n_conn)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_ws_report(results, n_conn, wall_ms)
        _assert_error_rate(results, f"ws_action @ {n_conn} conn")


class TestWSObjectScaling:
    @pytest.mark.parametrize("n_conn", WS_CONN_LEVELS)
    def test_scaling(self, ws_object_endpoint: WSEndpointSpec, n_conn: int) -> None:
        t0 = time.perf_counter()
        results = _ws_concurrent_connections(ws_object_endpoint, n_conn)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_ws_report(results, n_conn, wall_ms)
        _assert_error_rate(results, f"ws_object @ {n_conn} conn")
