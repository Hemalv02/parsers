"""The per-worker concurrency ceiling on heavy parses.

`convert()` gates legacy + isolated-worker formats behind an asyncio.Semaphore
so an inbound burst can't fan out to dozens of soffice / worker subprocesses;
light in-process formats stay ungated.

We drive the ASGI app with httpx.AsyncClient and fire requests with
asyncio.gather on ONE event loop — the real production scenario (uvicorn runs
the app on a single loop per worker). `dispatch` is stubbed to record the peak
number of simultaneous in-flight calls. (TestClient is not used here: it isn't
built for concurrent cross-thread calls and deadlocks.)"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx


def _make_recording_dispatch():
    """A fake dispatch that tracks peak concurrency and sleeps to overlap.

    The sleep runs in run_in_threadpool's worker thread, so it doesn't block
    the event loop — concurrent requests genuinely overlap up to the gate."""
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def fake_dispatch(path, tmpdir, mode):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.15)
        with lock:
            state["cur"] -= 1
        return {
            "parser": "fake",
            "markdown": "x",
            "structured": None,
            "stats": {},
            "metadata": {},
        }

    return fake_dispatch, state


async def _fire(n: int, filename: str) -> list[int]:
    from app import main

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        results = await asyncio.gather(
            *(
                ac.post(
                    "/convert",
                    files={"file": (filename, b"stub-bytes", "application/octet-stream")},
                )
                for _ in range(n)
            )
        )
    return [r.status_code for r in results]


def test_heavy_parses_are_bounded(monkeypatch):
    from app import main

    # Gate at 2; .pdf is an isolated-worker (heavy) format.
    monkeypatch.setattr(main, "_heavy_semaphore", asyncio.Semaphore(2))
    fake_dispatch, state = _make_recording_dispatch()
    monkeypatch.setattr(main, "dispatch", fake_dispatch)

    codes = asyncio.run(_fire(6, "f.pdf"))
    assert codes == [200] * 6
    # The semaphore is a hard ceiling — peak can never exceed the limit.
    assert state["peak"] <= 2


def test_light_parses_not_bounded(monkeypatch):
    from app import main

    # Even with the heavy gate at 1, light .txt parses must run concurrently.
    monkeypatch.setattr(main, "_heavy_semaphore", asyncio.Semaphore(1))
    fake_dispatch, state = _make_recording_dispatch()
    monkeypatch.setattr(main, "dispatch", fake_dispatch)

    codes = asyncio.run(_fire(4, "f.txt"))
    assert codes == [200] * 4
    # Not gated by the heavy semaphore → more than one in flight at once.
    assert state["peak"] >= 2


def test_is_heavy_classification():
    from app.main import _is_heavy

    assert _is_heavy(".pdf") is True  # isolated worker
    assert _is_heavy(".xlsx") is True  # isolated worker
    assert _is_heavy(".doc") is True  # legacy soffice round-trip
    assert _is_heavy(".txt") is False  # light, in-process
    assert _is_heavy(".json") is False
