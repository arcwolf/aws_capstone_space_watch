"""FastAPI 앱 — API와 정적 SPA를 한 프로세스에서 서빙해 :8000 하나로 완결한다."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, collector, config, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("space-watch")

FRONTEND_DIR = os.environ.get(
    "SW_FRONTEND_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """기동 시 즉시 1회 수집한 뒤 폴러를 백그라운드로 띄운다.

    첫 수집을 동기적으로 기다리는 이유: 그러지 않으면 기동 직후 첫 요청이 빈
    화면을 본다. 디스크 캐시가 살아 있으면 이 1회 수집도 네트워크를 타지 않는다.
    """
    client = httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SECONDS, follow_redirects=True)
    app.state.http = client

    try:
        status = await collector.collect_once(client, use_cache=True)
        log.info("초기 수집 완료: %s", status)
    except Exception as exc:  # noqa: BLE001 — 수집 실패가 기동을 막아선 안 된다
        store.mark_cycle(f"initial collect failed: {type(exc).__name__}: {exc}"[:200])
        log.warning("초기 수집 실패 — 폴러가 %d초 후 재시도한다: %s", config.POLL_INTERVAL_SECONDS, exc)

    poller = asyncio.create_task(collector.run_poller(client), name="space-watch-poller")
    log.info("폴러 시작 — 주기 %d초", config.POLL_INTERVAL_SECONDS)

    try:
        yield
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        await client.aclose()
        log.info("종료 완료")


app = FastAPI(
    title="Space Watch",
    description="전 세계 우주개발 시황 — 태양계 도달 현황, 지구 저궤도, 발사 시장",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(api.router)

# StaticFiles 는 반드시 라우터 include 뒤에 마운트한다.
# "/" 에 먼저 마운트하면 /api/* 요청까지 정적 파일 핸들러가 삼켜버린다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
