"""수집기 — 외부 피드를 긁어 정규화한 뒤 스토어에 넣는다. I/O만 담당한다.

부분 실패 격리가 이 모듈의 핵심 성질이다.
  - 항목 수준: feature 하나가 깨져 있으면 그 항목만 버리고 계속한다.
  - 소스 수준: 4개 요청을 병렬로 던지고 실패한 소스는 직전 스냅샷을 유지한다.
    CelesTrak이 죽어도 발사 시황은 살아 있다.
  - 사이클 수준: 폴링 루프는 어떤 예외에도 죽지 않는다.

레이트리밋:
  LL2 익명 한도는 15 req/hour. 사이클당 4요청 × 3사이클/시간 = 12 req/hr.
  여기에 디스크 캐시를 더해, 기동 시 캐시가 CACHE_MAX_AGE_SECONDS 보다 어리면
  네트워크를 아예 건드리지 않는다. 개발 중 서버 재시작 스무 번이 쿼터를 태우지
  않고, Fargate 태스크 재기동도 안전하다.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config, store

# ---------------------------------------------------------------------------
# 안전한 필드 접근
# ---------------------------------------------------------------------------
_UNKNOWN = "unknown"


def _dig(obj: Any, *keys: str, default: Any = None) -> Any:
    """중첩 딕셔너리를 파고든다. 중간에 None이나 비-딕셔너리를 만나면 default."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _text(obj: Any, *keys: str) -> str:
    value = _dig(obj, *keys)
    if value is None:
        return _UNKNOWN
    text = str(value).strip()
    return text or _UNKNOWN


def _epoch_ms(iso: Any) -> int:
    """ISO8601 → epoch ms. 파싱 실패 시 0 (스토어의 정렬/필터가 알아서 밀어낸다)."""
    if not iso:
        return 0
    try:
        text = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _coord(value: Any) -> float | None:
    """위경도. 실패 시 None을 쓴다 — 0으로 두면 기니 만 앞바다에 발사장이 생긴다."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _country_from_location(location: str) -> str:
    """'Jiuquan Satellite Launch Center, People's Republic of China' → 소재국.

    LL2 기본 모드 응답의 launch_service_provider 에는 country_code 가 없다.
    대신 발사장 소재국을 쓴다 — 발사사 국적과 뜻이 다르지만 실제 데이터이고,
    '어디서 쏘는가'는 그 자체로 읽을 만한 정보다.
    """
    if not location or location == _UNKNOWN:
        return _UNKNOWN
    return location.rsplit(",", 1)[-1].strip() or _UNKNOWN


# ---------------------------------------------------------------------------
# 정규화
# ---------------------------------------------------------------------------
_PROVIDER_TYPES = {"Commercial", "Government", "Multinational", "Private"}


def normalize_launch(raw: dict, kind: str) -> dict | None:
    """LL2 발사 하나를 정규화한다. id가 없으면 이 항목만 버린다."""
    launch_id = raw.get("id")
    if not launch_id:
        return None

    pad_location = _text(raw, "pad", "location", "name")
    provider_type = _text(raw, "launch_service_provider", "type")
    if provider_type not in _PROVIDER_TYPES:
        provider_type = _UNKNOWN

    return {
        "id": str(launch_id),
        "kind": kind,  # "past" | "upcoming"
        "name": _text(raw, "name"),
        "net": _epoch_ms(raw.get("net")),
        "status": _text(raw, "status", "abbrev"),
        "status_full": _text(raw, "status", "name"),
        "provider": _text(raw, "launch_service_provider", "name"),
        "provider_type": provider_type,
        "provider_country": _country_from_location(pad_location),
        "rocket": _text(raw, "rocket", "configuration", "full_name"),
        "mission": _text(raw, "mission", "name"),
        "mission_type": _text(raw, "mission", "type"),
        "orbit": _text(raw, "mission", "orbit", "name"),
        "pad": _text(raw, "pad", "name"),
        "pad_location": pad_location,
        "pad_lat": _coord(_dig(raw, "pad", "latitude")),
        "pad_lon": _coord(_dig(raw, "pad", "longitude")),
    }


def normalize_station(raw: dict) -> dict | None:
    station_id = raw.get("id")
    if station_id is None:
        return None

    owners = [o.get("name") for o in (raw.get("owners") or []) if o.get("name")]
    return {
        "id": str(station_id),
        "name": _text(raw, "name"),
        "status": _text(raw, "status", "name"),
        "owner": ", ".join(owners) if owners else _UNKNOWN,
        "owner_count": len(owners),
        "founded": _text(raw, "founded"),
        "orbit": _text(raw, "orbit"),
        "active_expeditions": len(raw.get("active_expeditions") or []),
    }


def cluster_orbital(gp_list: list[dict]) -> dict[str, dict]:
    """CelesTrak stations 그룹을 정거장 단위로 묶는다.

    같은 평균운동/경사각을 가진 객체는 같은 궤도를 돈다 — 즉 도킹돼 있다는 뜻이다.
    실측으로 22개가 (15.49, 51.6) 8개 / (15.60, 41.5) 5개 / 나머지 개별로 갈렸다.
    대표 객체만 궤도에 그리고 나머지는 모듈/도킹 우주선으로 분류한다.

    주의: 공식 도킹 데이터가 아니라 궤도요소로부터의 추론이다. 방금 분리한
    우주선이 몇 시간 동안 '도킹 중'으로 오탐될 수 있다.
    """
    groups: dict[tuple[float, float], list[dict]] = {}
    for obj in gp_list:
        try:
            key = (
                round(float(obj["MEAN_MOTION"]), 2),
                round(float(obj["INCLINATION"]), 1),
            )
        except (KeyError, TypeError, ValueError):
            continue  # 요소가 깨진 객체는 이 항목만 버린다
        groups.setdefault(key, []).append(obj)

    clusters: dict[str, dict] = {}
    for members in groups.values():
        if len(members) < config.MIN_CLUSTER_SIZE:
            continue  # 파편이나 개별 큐브샛

        names = [str(m.get("OBJECT_NAME", _UNKNOWN)) for m in members]
        anchor = next((m for m in members if m.get("OBJECT_NAME") in config.STATION_ANCHORS), None)
        if anchor is None:
            continue  # 화이트리스트에 없는 클러스터는 정거장으로 취급하지 않는다

        anchor_name = str(anchor["OBJECT_NAME"])
        info = config.STATION_ANCHORS[anchor_name]

        modules, docked = [], []
        for name in names:
            upper = name.upper()
            if any(kw in upper for kw in config.MODULE_KEYWORDS):
                modules.append(name)
            else:
                docked.append(name)

        clusters[info["key"]] = {
            "key": info["key"],
            "anchor_name": anchor_name,
            "name_ko": info["name_ko"],
            "short": info["short"],
            "elements": anchor,
            "modules": modules,
            "docked": docked,
            "object_count": len(members),
        }
    return clusters


# ---------------------------------------------------------------------------
# 디스크 캐시
# ---------------------------------------------------------------------------
def _cache_path(name: str) -> str:
    return os.path.join(config.CACHE_DIR, f"{name}.json")


def _cache_write(name: str, payload: Any) -> None:
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        tmp = _cache_path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, _cache_path(name))  # 원자적 교체 — 반쯤 쓰인 캐시를 읽지 않는다
    except OSError:
        pass  # 캐시는 최적화일 뿐이므로 실패해도 수집을 막지 않는다


def _cache_read(name: str, max_age: int) -> Any | None:
    path = _cache_path(name)
    try:
        if time.time() - os.path.getmtime(path) > max_age:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
_SOURCES = (
    ("previous", config.LL2_PREVIOUS_URL),
    ("upcoming", config.LL2_UPCOMING_URL),
    ("stations", config.LL2_STATIONS_URL),
    ("orbital", config.CELESTRAK_STATIONS_URL),
)


async def _fetch(client: httpx.AsyncClient, name: str, url: str, use_cache: bool) -> tuple[str, Any, str]:
    """단일 소스를 가져온다. 반환: (소스명, 페이로드, 상태문자열)."""
    if use_cache:
        cached = _cache_read(name, config.CACHE_MAX_AGE_SECONDS)
        if cached is not None:
            return name, cached, "cache"

    response = await client.get(url, headers={"User-Agent": "space-watch/1.0"})
    response.raise_for_status()
    payload = response.json()
    _cache_write(name, payload)
    return name, payload, "ok"


async def collect_once(client: httpx.AsyncClient, use_cache: bool = False) -> dict:
    """한 사이클. 4개 소스를 병렬로 가져오고, 실패한 소스는 직전 스냅샷을 유지한다."""
    results = await asyncio.gather(
        *(_fetch(client, name, url, use_cache) for name, url in _SOURCES),
        return_exceptions=True,
    )

    payloads: dict[str, Any] = {}
    status: dict[str, str] = {}
    for (name, _url), outcome in zip(_SOURCES, results):
        if isinstance(outcome, BaseException):
            status[name] = f"{type(outcome).__name__}: {outcome}"[:160]
            continue
        _, payload, state = outcome
        payloads[name] = payload
        status[name] = state

    # --- 발사: past + upcoming 을 하나의 멱등 딕셔너리에 넣는다 ---
    launches: list[dict] = []
    for source, kind in (("previous", "past"), ("upcoming", "upcoming")):
        for raw in (payloads.get(source) or {}).get("results", []):
            item = normalize_launch(raw, kind)
            if item is not None:
                launches.append(item)
    if launches:
        store.upsert_launches(launches)

    # --- 정거장 ---
    stations = [
        s
        for s in (
            normalize_station(raw)
            for raw in (payloads.get("stations") or {}).get("results", [])
        )
        if s is not None
    ]
    store.replace_stations(stations)

    # --- 궤도요소 클러스터링 ---
    orbital_raw = payloads.get("orbital")
    if isinstance(orbital_raw, list):
        store.replace_orbital(cluster_orbital(orbital_raw))

    failures = [f"{k}={v}" for k, v in status.items() if v not in ("ok", "cache")]
    store.mark_cycle("; ".join(failures) or None, status)
    return status


async def run_poller(client: httpx.AsyncClient) -> None:
    """무한 폴링 루프. 한 사이클의 예외를 삼켜서 루프를 죽이지 않는다."""
    while True:
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        try:
            await collect_once(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 어떤 예외도 폴러를 죽일 수 없다
            store.mark_cycle(f"cycle failed: {type(exc).__name__}: {exc}"[:200])


# ---------------------------------------------------------------------------
# 스모크 실행 — python -m app.collector
# ---------------------------------------------------------------------------
async def _smoke() -> None:  # pragma: no cover
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SECONDS) as client:
        status = await collect_once(client, use_cache=True)
    print("소스 상태:", status)
    print("발사:", len(store.all_launches()), "| 정거장:", len(store.all_stations()))
    for key, cluster in store.orbital_clusters().items():
        print(
            f"  {key}: {cluster['name_ko']} — 객체 {cluster['object_count']}개 "
            f"(모듈 {len(cluster['modules'])}, 도킹 {cluster['docked']})"
        )
    summary = store.query_launches(days=90)["summary"]
    print("요약:", {k: summary[k] for k in ("count", "success_rate", "leader", "leader_share", "commercial_share")})
    print("톱5:", [(p["name"], p["count"]) for p in summary["top_providers"][:5]])


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_smoke())
