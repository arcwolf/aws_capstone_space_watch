"""인메모리 스냅샷 — 이벤트 id를 멱등 키로 쓰는 딕셔너리.

동시성에 관하여:
  폴링 태스크와 요청 핸들러가 같은 딕셔너리를 만지지만 락이 없다. 둘 다 같은
  asyncio 이벤트 루프의 코루틴이고, 아래 함수들 내부에 `await` 지점이 하나도
  없기 때문이다. asyncio는 협력적 스케줄링이므로 await 없는 동기 블록은 중간에
  끼어들 수 없다 — 즉 원자적이다.

  이 보장은 누군가 아래 함수 안에 `await` 를 추가하는 순간 깨진다. 그때는
  asyncio.Lock 을 도입해야 한다. 스레드를 쓰기 시작하면 그때도 깨진다.
"""

from __future__ import annotations

import time
from collections import Counter

from . import config

# ---------------------------------------------------------------------------
# 스냅샷
# ---------------------------------------------------------------------------
_launches: dict[str, dict] = {}   # LL2 발사 id → 정규화 발사
_stations: dict[str, dict] = {}   # LL2 정거장 id → 정규화 정거장
_orbital: dict[str, dict] = {}    # 클러스터 키 → {anchor, elements, modules, docked}

_meta: dict = {
    "last_new_ids": [],
    "last_collected_at": None,
    "last_error": None,
    "cycle_count": 0,
    "source_status": {},  # 소스명 → "ok" | "cache" | 오류 문자열
}


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------
def upsert_launches(items: list[dict]) -> list[str]:
    """발사 목록을 id 멱등으로 반영하고 신규 진입 id를 돌려준다.

    같은 id는 갱신만 한다 — 중복 삽입하지 않는다. LL2는 예정 발사가 실제
    발사되면 같은 id로 status 만 바뀌므로, 이 동작이 곧 상태 추적이 된다.
    """
    new_ids: list[str] = []
    for item in items:
        key = item["id"]
        if key not in _launches:
            new_ids.append(key)
        _launches[key] = item

    # 상한 초과 시 발사 시각이 오래된 것부터 제거한다. 삽입 순서가 아니라
    # net 기준이어야 "최근 N발"이라는 의미가 맞다.
    if len(_launches) > config.MAX_LAUNCHES:
        ordered = sorted(_launches.values(), key=lambda x: x["net"])
        for stale in ordered[: len(_launches) - config.MAX_LAUNCHES]:
            _launches.pop(stale["id"], None)

    _meta["last_new_ids"] = new_ids
    return new_ids


def replace_stations(items: list[dict]) -> None:
    """정거장 목록은 전량 교체한다 — 15개 고정 집합이고 부분 갱신할 이유가 없다."""
    if not items:
        return
    _stations.clear()
    for item in items:
        _stations[item["id"]] = item


def replace_orbital(clusters: dict[str, dict]) -> None:
    if not clusters:
        return
    _orbital.clear()
    _orbital.update(clusters)


def mark_cycle(error: str | None, source_status: dict | None = None) -> None:
    _meta["cycle_count"] += 1
    _meta["last_error"] = error
    if source_status:
        _meta["source_status"] = source_status
    if error is None or source_status:
        _meta["last_collected_at"] = time.time()


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------
def all_launches() -> list[dict]:
    return list(_launches.values())


def all_stations() -> list[dict]:
    return list(_stations.values())


def orbital_clusters() -> dict[str, dict]:
    return dict(_orbital)


def meta() -> dict:
    return dict(_meta)


def query_launches(
    days: int = 90,
    provider: str = "",
    commercial_only: bool = False,
) -> dict:
    """시간 창 + 발사사 필터를 적용한 목록과 요약 통계.

    과거(past)와 예정(upcoming)을 분리해 돌려준다. 요약 통계는 과거 발사만으로
    낸다 — 예정 발사를 성공률에 섞으면 분모가 오염된다.
    """
    now_ms = time.time() * 1000.0
    window_start = now_ms - days * 86400_000.0
    needle = provider.strip().lower()

    past: list[dict] = []
    upcoming: list[dict] = []
    for item in _launches.values():
        if needle and needle not in item["provider"].lower():
            continue
        if commercial_only and item["provider_type"] != "Commercial":
            continue

        if item["kind"] == "upcoming" or item["net"] > now_ms:
            upcoming.append(item)
        elif item["net"] >= window_start:
            past.append(item)

    past.sort(key=lambda x: x["net"], reverse=True)
    upcoming.sort(key=lambda x: x["net"])

    return {
        "past": past,
        "upcoming": upcoming,
        "summary": _summarize(past, days),
    }


def _summarize(past: list[dict], days: int) -> dict:
    success = sum(1 for x in past if x["status"] == "Success")
    failure = sum(1 for x in past if x["status"] in ("Failure", "Partial Failure"))
    commercial = sum(1 for x in past if x["provider_type"] == "Commercial")
    government = sum(1 for x in past if x["provider_type"] == "Government")

    # 발사사별 집계. LL2 agencies 목록 응답의 total_launch_count 는 null 이므로
    # 발사 목록을 직접 집계하는 것이 유일하게 신뢰할 수 있는 경로다.
    counts: Counter[str] = Counter(x["provider"] for x in past)
    detail: dict[str, dict] = {}
    for item in past:
        row = detail.setdefault(
            item["provider"],
            {
                "name": item["provider"],
                "type": item["provider_type"],
                "country": item["provider_country"],
                "count": 0,
                "success": 0,
            },
        )
        row["count"] += 1
        if item["status"] == "Success":
            row["success"] += 1

    top = sorted(detail.values(), key=lambda r: r["count"], reverse=True)
    total = len(past)
    leader = top[0] if top else None

    return {
        "count": total,
        "success": success,
        "failure": failure,
        "success_rate": round(100.0 * success / total, 1) if total else 0.0,
        "commercial_count": commercial,
        "government_count": government,
        "commercial_share": round(100.0 * commercial / total, 1) if total else 0.0,
        "provider_count": len(counts),
        "top_providers": top[:10],
        "leader": leader["name"] if leader else "unknown",
        "leader_share": round(100.0 * leader["count"] / total, 1) if leader and total else 0.0,
        "window_days": days,
        "last_collected_at": _meta["last_collected_at"],
    }


def health() -> dict:
    """ALB 헬스체크가 보는 경로. 수집이 실패 중이어도 절대 예외를 던지지 않는다."""
    return {
        "status": "ok",
        "launches": len(_launches),
        "stations": len(_stations),
        "orbital_clusters": len(_orbital),
        "cycles": _meta["cycle_count"],
        "last_collected_at": _meta["last_collected_at"],
        "last_error": _meta["last_error"],
        "source_status": _meta["source_status"],
        "new_ids_last_cycle": len(_meta["last_new_ids"]),
    }
