"""HTTP benchmark: concurrent requests across all endpoints with increasing load.

Requires DL_BACKEND_URL, DL_API_KEY and audio/image fixtures. Example: ``pytest tests/benchmark_api/test_benchmark_api.py -v -s``
"""

from __future__ import annotations

import base64
import functools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import cv2
import httpx
import numpy as np
import pytest
from dotenv import load_dotenv

_ = load_dotenv(override=True)

DL_BACKEND_URL = os.getenv("DL_BACKEND_URL", "").rstrip("/")
DL_API_KEY = os.getenv("DL_API_KEY", "")

AUDIO_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "audio"
IMAGE_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "images"

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL,
    reason="DL_BACKEND_URL not set - skipping benchmark tests.",
)


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if DL_API_KEY:
        h["X-API-Key"] = DL_API_KEY
    return h


def _url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _wav_to_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _load_image_b64(name: str = "person_drinking.jpg") -> str:
    path = IMAGE_FIXTURES / name
    if not path.exists():
        return _make_frame_b64()
    img = cv2.imread(str(path))
    if img is None:
        return _make_frame_b64()
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode()


@dataclass
class EndpointSpec:
    """Defines an API endpoint to benchmark."""

    name: str
    method: str  # "POST" or "GET"
    path: str
    payload_fn: Callable[[], dict[str, Any]]
    ok_codes: set[int] = field(default_factory=lambda: {200})


ERROR_TIMEOUT = "timeout"
ERROR_CONNECTION = "connection"
ERROR_SERVER = "server"


@dataclass
class RequestResult:
    endpoint: str
    latency_ms: float
    error: str | None = None
    error_type: str | None = None  # timeout | connection | server


@functools.cache
def _pose_payload() -> dict[str, Any]:
    return {"image_b64": _make_frame_b64()}


@functools.cache
def _fer_payload() -> dict[str, Any]:
    return {"image_b64": _make_frame_b64(), "return_scores": True}


@functools.cache
def _audio_embed_payload() -> dict[str, Any]:
    wav = next((AUDIO_FIXTURES / "speaker_a").glob("*.wav"), None)
    if wav is None:
        return {"audios_b64": []}
    return {"audios_b64": [_wav_to_b64(wav)]}


@functools.cache
def _ser_payload() -> dict[str, Any]:
    wav = AUDIO_FIXTURES / "happy.wav"
    if not wav.exists():
        return {"audio_b64": ""}
    return {"audio_b64": _wav_to_b64(wav), "return_scores": False}


@functools.cache
def _object_detect_payload() -> dict[str, Any]:
    if IMAGE_FIXTURES.exists() and (IMAGE_FIXTURES / "person_drinking.jpg").exists():
        return {"image_b64": _load_image_b64(), "classes": ["person", "chair"]}
    return {"image_b64": _make_frame_b64(), "classes": ["person"]}


ALL_ENDPOINTS: list[EndpointSpec] = [
    EndpointSpec(
        name="fer",
        method="POST",
        path="/hal/api/dl/emotion-recognize",
        payload_fn=_fer_payload,
    ),
    EndpointSpec(
        name="ser",
        method="POST",
        path="/hal/api/dl/ser/recognize",
        payload_fn=_ser_payload,
    ),
    EndpointSpec(
        name="audio_embed",
        method="POST",
        path="/hal/api/dl/audio-recognizer/embed",
        payload_fn=_audio_embed_payload,
    ),
    EndpointSpec(
        name="object_detect",
        method="POST",
        path="/api/dl/yoloworld",
        payload_fn=_object_detect_payload,
        ok_codes={200, 503},
    ),
    EndpointSpec(
        name="object_detect",
        method="POST",
        path="/api/dl/owlv2",
        payload_fn=_object_detect_payload,
        ok_codes={200, 503},
    ),
    EndpointSpec(
        name="health",
        method="GET",
        path="/hal/api/dl/health",
        payload_fn=dict,
    ),
]


def _probe_endpoint(ep: EndpointSpec) -> bool:
    try:
        with httpx.Client() as client:
            if ep.method == "GET":
                resp = client.get(_url(ep.path), headers=_headers(), timeout=15.0)
            else:
                resp = client.post(
                    _url(ep.path), json=ep.payload_fn(), headers=_headers(), timeout=30.0
                )
            return resp.status_code != 404
    except Exception:
        return False


def _fire_request(ep: EndpointSpec) -> RequestResult:
    """Send a single HTTP request and measure latency in its own thread."""
    t0 = time.perf_counter()
    try:
        with httpx.Client() as client:
            if ep.method == "GET":
                resp = client.get(_url(ep.path), headers=_headers(), timeout=240.0)
            else:
                resp = client.post(
                    _url(ep.path), json=ep.payload_fn(), headers=_headers(), timeout=240.0
                )
        latency = (time.perf_counter() - t0) * 1000
        if resp.status_code in ep.ok_codes:
            return RequestResult(endpoint=ep.name, latency_ms=latency)
        return RequestResult(
            endpoint=ep.name, latency_ms=latency,
            error=f"HTTP {resp.status_code}", error_type=ERROR_SERVER,
        )
    except httpx.TimeoutException as exc:
        latency = (time.perf_counter() - t0) * 1000
        return RequestResult(
            endpoint=ep.name, latency_ms=latency,
            error=str(exc) or type(exc).__name__, error_type=ERROR_TIMEOUT,
        )
    except httpx.ConnectError as exc:
        latency = (time.perf_counter() - t0) * 1000
        return RequestResult(
            endpoint=ep.name, latency_ms=latency,
            error=str(exc) or type(exc).__name__, error_type=ERROR_CONNECTION,
        )
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        return RequestResult(
            endpoint=ep.name, latency_ms=latency,
            error=str(exc) or type(exc).__name__, error_type=ERROR_SERVER,
        )


MAX_PROCESSES = 16


def _worker_batch(tasks: list[EndpointSpec]) -> list[RequestResult]:
    """Run a chunk of tasks inside one worker process using threads."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return list(pool.map(_fire_request, tasks))


def _run_concurrent_batch(
    endpoints: list[EndpointSpec],
    n_per_endpoint: int,
) -> list[RequestResult]:
    """Distribute tasks across up to MAX_PROCESSES processes, each fanning out via threads."""
    from concurrent.futures import ProcessPoolExecutor

    all_tasks = [ep for ep in endpoints for _ in range(n_per_endpoint)]
    n_procs = min(len(all_tasks), MAX_PROCESSES)

    chunks: list[list[EndpointSpec]] = [[] for _ in range(n_procs)]
    for i, task in enumerate(all_tasks):
        chunks[i % n_procs].append(task)

    with ProcessPoolExecutor(max_workers=n_procs) as pool:
        chunk_results = list(pool.map(_worker_batch, chunks))

    return [r for batch in chunk_results for r in batch]


def _print_report(
    results: list[RequestResult],
    n_per_endpoint: int,
    wall_time_ms: float,
) -> None:
    endpoints = sorted(set(r.endpoint for r in results))
    total_ok = sum(1 for r in results if r.error is None)
    total = len(results)
    total_timeout = sum(1 for r in results if r.error_type == ERROR_TIMEOUT)
    total_conn = sum(1 for r in results if r.error_type == ERROR_CONNECTION)
    total_server = sum(1 for r in results if r.error_type == ERROR_SERVER)

    print(f"\n{'=' * 80}")
    print(
        f"  Concurrency: {n_per_endpoint} per endpoint | "
        f"Total requests: {total} | "
        f"Wall time: {wall_time_ms:.0f}ms"
    )
    print(f"{'=' * 80}")
    print(
        f"  {'Endpoint':<16} {'OK':>4} {'Tmo':>4} {'Conn':>4} {'Svr':>4} "
        f"{'Mean':>8} {'P50':>8} {'P95':>8} {'P99':>8} {'Max':>8}"
    )
    print(f"  {'-' * 76}")

    for ep_name in endpoints:
        ep_results = [r for r in results if r.endpoint == ep_name]
        ok = sum(1 for r in ep_results if r.error is None)
        tmo = sum(1 for r in ep_results if r.error_type == ERROR_TIMEOUT)
        conn = sum(1 for r in ep_results if r.error_type == ERROR_CONNECTION)
        svr = sum(1 for r in ep_results if r.error_type == ERROR_SERVER)
        latencies = sorted(r.latency_ms for r in ep_results if r.latency_ms > 0)

        if not latencies:
            print(f"  {ep_name:<16} {ok:>4} {tmo:>4} {conn:>4} {svr:>4}   (no latency data)")
            continue

        mean = sum(latencies) / len(latencies)
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        mx = latencies[-1]

        print(
            f"  {ep_name:<16} {ok:>4} {tmo:>4} {conn:>4} {svr:>4} "
            f"{mean:>7.0f}ms {p50:>7.0f}ms {p95:>7.0f}ms {p99:>7.0f}ms {mx:>7.0f}ms"
        )

    print(f"  {'-' * 76}")
    print(f"  {'TOTAL':<16} {total_ok:>4} {total_timeout:>4} {total_conn:>4} {total_server:>4}")
    if wall_time_ms > 0:
        print(f"  Throughput: {total_ok / (wall_time_ms / 1000):.1f} req/s")
    print(f"{'=' * 80}\n")


def _assert_error_rate(results: list[RequestResult], label: str) -> None:
    timeouts = [r for r in results if r.error_type == ERROR_TIMEOUT]
    server_errors = [r for r in results if r.error_type == ERROR_SERVER]
    conn_errors = [r for r in results if r.error_type == ERROR_CONNECTION]
    total = len(results) if results else 1

    timeout_rate = len(timeouts) / total
    server_rate = len(server_errors) / total
    conn_rate = len(conn_errors) / total

    assert server_rate < MAX_ERROR_RATE, (
        f"{label}: server error rate {server_rate:.0%}. "
        f"Errors: {[(e.endpoint, e.error) for e in server_errors[:10]]}"
    )
    assert conn_rate < MAX_ERROR_RATE, (
        f"{label}: connection error rate {conn_rate:.0%}. "
        f"Errors: {[(e.endpoint, e.error) for e in conn_errors[:10]]}"
    )
    if timeout_rate > 0:
        print(
            f"  WARNING: {label}: {len(timeouts)} timeouts ({timeout_rate:.0%}) — "
            f"endpoints: {set(r.endpoint for r in timeouts)}"
        )


@pytest.fixture(scope="module")
def available_endpoints() -> list[EndpointSpec]:
    eps = [ep for ep in ALL_ENDPOINTS if _probe_endpoint(ep)]
    if not eps:
        pytest.skip("No API endpoints available")
    print(f"\nAvailable endpoints: {[e.name for e in eps]}")
    return eps


def _make_single_ep_fixture(ep_name: str) -> Callable[..., EndpointSpec | None]:
    @pytest.fixture(scope="module")
    def _fixture(available_endpoints: list[EndpointSpec]) -> EndpointSpec | None:
        for ep in available_endpoints:
            if ep.name == ep_name:
                return ep

        pytest.skip(f"{ep_name} endpoint not available")
        return None

    return _fixture


pose_endpoint = _make_single_ep_fixture("pose")
fer_endpoint = _make_single_ep_fixture("fer")
ser_endpoint = _make_single_ep_fixture("ser")
audio_embed_endpoint = _make_single_ep_fixture("audio_embed")
object_detect_endpoint = _make_single_ep_fixture("object_detect")


MAX_ERROR_RATE = 0.05
CONCURRENCY_LEVELS = [1, 2, 4, 8, 16, 32, 64, 128]


class TestAllEndpoints:
    """Fire requests at all available endpoints with increasing concurrency."""

    @pytest.mark.parametrize("n", CONCURRENCY_LEVELS)
    def test_scaling(self, available_endpoints: list[EndpointSpec], n: int) -> None:
        t0 = time.perf_counter()
        results = _run_concurrent_batch(available_endpoints, n)
        wall_ms = (time.perf_counter() - t0) * 1000

        _print_report(results, n, wall_ms)
        _assert_error_rate(results, f"all endpoints @ {n} concurrent")


class TestPoseScaling:
    @pytest.mark.parametrize("n", CONCURRENCY_LEVELS)
    def test_scaling(self, pose_endpoint: EndpointSpec, n: int) -> None:
        t0 = time.perf_counter()
        results = _run_concurrent_batch([pose_endpoint], n)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_report(results, n, wall_ms)
        _assert_error_rate(results, f"pose @ {n} concurrent")


class TestFERScaling:
    @pytest.mark.parametrize("n", CONCURRENCY_LEVELS)
    def test_scaling(self, fer_endpoint: EndpointSpec, n: int) -> None:
        t0 = time.perf_counter()
        results = _run_concurrent_batch([fer_endpoint], n)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_report(results, n, wall_ms)
        _assert_error_rate(results, f"fer @ {n} concurrent")


class TestSERScaling:
    @pytest.mark.parametrize("n", CONCURRENCY_LEVELS)
    def test_scaling(self, ser_endpoint: EndpointSpec, n: int) -> None:
        t0 = time.perf_counter()
        results = _run_concurrent_batch([ser_endpoint], n)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_report(results, n, wall_ms)
        _assert_error_rate(results, f"ser @ {n} concurrent")


class TestAudioEmbedScaling:
    @pytest.mark.parametrize("n", CONCURRENCY_LEVELS)
    def test_scaling(self, audio_embed_endpoint: EndpointSpec, n: int) -> None:
        t0 = time.perf_counter()
        results = _run_concurrent_batch([audio_embed_endpoint], n)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_report(results, n, wall_ms)
        _assert_error_rate(results, f"audio_embed @ {n} concurrent")


class TestObjectDetectScaling:
    @pytest.mark.parametrize("n", CONCURRENCY_LEVELS)
    def test_scaling(self, object_detect_endpoint: EndpointSpec, n: int) -> None:
        t0 = time.perf_counter()
        results = _run_concurrent_batch([object_detect_endpoint], n)
        wall_ms = (time.perf_counter() - t0) * 1000
        _print_report(results, n, wall_ms)
        _assert_error_rate(results, f"object_detect @ {n} concurrent")


