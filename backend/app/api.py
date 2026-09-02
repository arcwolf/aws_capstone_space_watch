"""API 라우트 — 스토어의 스냅샷과 orbits 의 계산을 조합해 SPA에 먹인다.

세 개의 GET을 하나로 합치지 않는 이유: 데이터 성격과 갱신 주기가 다르다.
태양계는 순수 계산이라 필터가 없고, 발사는 필터가 있고, LEO는 근사 경고를
동반한다. 프런트는 Promise.all 세 줄로 병렬 호출하면 되고, 대신 "발사 필터를
바꿨는데 태양계를 다시 계산"하는 낭비가 사라진다.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from . import config, llm, orbits, store

router = APIRouter()

SOLAR_NOTE = (
    "황도면 투영 · 궤도경사각 무시(최대 7°, 수성) · "
    "행성 위치는 J2000 평균요소 기반 계산값 · 도달등급은 큐레이션 데이터 · "
    "반경은 40 AU 까지 세제곱근, 그 밖은 로그 압축(구간 스케일)"
)
SPACECRAFT_NOTE = (
    "탈출 항적: 거리·속도는 실측값이고 시간의 1차 함수로 외삽한다. 방향(황경)은 근사이며 "
    "황위는 투영에서 버렸다(보이저 2 는 실제로 황위 -59°). "
    "순항 미션: 발사점과 도착점은 계산한 실제 위치지만 이어진 선과 마커는 "
    "임무 경과율 표시이며 실제 궤적이 아니다 — 실제 항로는 중력도움을 쓰는 나선이다."
)
LEO_NOTE = (
    "표시용 근사 — 추적용 아님. SGP4 미적용으로 대기항력·J2 섭동을 무시하며, "
    "'도킹 중'은 궤도요소 클러스터링 추론이다(공식 도킹 데이터 아님)."
)


# ---------------------------------------------------------------------------
@router.get("/healthz")
def healthz() -> dict:
    """ALB 헬스체크 경로. 수집이 실패 중이어도 항상 200을 낸다.

    여기서 503을 내면 태스크가 영구 unhealthy가 되어 배포 자체가 실패한다.
    수집 상태는 본문의 last_error / source_status 로 노출한다.
    """
    return store.health()


# ---------------------------------------------------------------------------
@router.get("/api/solar-system")
def solar_system(
    at: str | None = Query(None, description="ISO8601. 생략하면 현재 시각"),
) -> dict:
    """행성 위치와 인류 도달 등급.

    계산식이 시간의 함수이므로 `at` 으로 임의 시점 배치를 공짜로 얻는다.
    """
    if at:
        try:
            epoch = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, f"at 파싱 실패: {exc}") from exc
        if epoch.tzinfo is None:
            epoch = epoch.replace(tzinfo=timezone.utc)
    else:
        epoch = datetime.now(timezone.utc)

    T = orbits.centuries_since_j2000(epoch)

    bodies = []
    earth_state = None
    by_key: dict[str, dict] = {}
    for planet in config.PLANETS:
        state = orbits.planet_state(planet, T)
        if planet["key"] == "earth":
            earth_state = state

        reach = config.REACH.get(planet["key"], {"tier": 0, "label": config.TIER_LABELS[0], "milestones": []})
        entry = {
            "key": planet["key"],
            "name": planet["name"],
            "name_ko": planet["name_ko"],
            "radius_km": planet["radius_km"],
            "is_dwarf": planet["is_dwarf"],
            "a_au": round(state["a_au"], 5),
            "e": round(state["e"], 5),
            "r_au": round(state["r_au"], 5),
            "lon_deg": round(state["lon_deg"], 2),
            "x_au": round(state["x_au"], 5),
            "y_au": round(state["y_au"], 5),
            "reach": reach,
            # 등시간 간격 경로 + 주기 + 현재 위상. 이 셋이면 프런트가 API 재호출 없이
            # 케플러 정확한 애니메이션을 돌린다.
            "orbit_path": orbits.orbit_path(planet, T),
            "period_days": round(orbits.orbital_period_days(state["a_au"]), 2),
            "phase": round(orbits.mean_anomaly_phase(planet, T), 6),
        }
        bodies.append(entry)
        by_key[planet["key"]] = entry

    # 달은 궤도요소를 따로 풀지 않는다. 태양계 스케일에서 달-지구 거리는
    # 0.0026 AU 로 1픽셀 미만이므로, 지구 위치에서 바깥쪽으로 고정 오프셋을 준다.
    moon = None
    if earth_state is not None:
        lon = math.radians(earth_state["lon_deg"])
        moon = {
            **config.MOON,
            "x_au": round(earth_state["x_au"] + 0.055 * math.cos(lon), 5),
            "y_au": round(earth_state["y_au"] + 0.055 * math.sin(lon), 5),
            "reach": config.REACH["moon"],
        }

    return {
        "epoch": epoch.isoformat(),
        "epoch_ms": int(epoch.timestamp() * 1000),
        "centuries_since_j2000": round(T, 6),
        "note": SOLAR_NOTE,
        "outermost_a_au": config.OUTERMOST_A_AU,
        "scale_break_au": config.SCALE_BREAK_AU,
        "scale_outer_max_au": config.SCALE_OUTER_MAX_AU,
        "orbit_samples": len(bodies[0]["orbit_path"]) if bodies else 0,
        "tier_labels": config.TIER_LABELS,
        "bodies": bodies,
        "moon": moon,
        "interstellar": config.INTERSTELLAR,
        "spacecraft": _spacecraft(epoch, T, by_key),
    }


def _spacecraft(epoch: datetime, T: float, by_key: dict[str, dict]) -> dict:
    """우주선 3종. 프런트가 프레임마다 재계산할 수 있도록 원재료를 함께 보낸다.

    - escaping: epoch/거리/속도/황경만 있으면 위치는 1차식이다.
    - cruising: 발사·도착 시점 좌표는 고정이므로 경과율만 다시 계산하면 된다.
    - stationed: 기준 천체(지구) 위치에서 파생되므로 프런트가 지구 위치로 계산한다.
    """
    escaping = []
    for sc in config.ESCAPING:
        state = orbits.escaping_state(sc, epoch)
        escaping.append({
            "key": sc["key"], "name": sc["name"], "name_ko": sc["name_ko"],
            "status": sc["status"], "milestone": sc["milestone"],
            "lon_deg": sc["lon_deg"], "lat_deg": sc["lat_deg"],
            "epoch": sc["epoch"], "epoch_distance_au": sc["distance_au"],
            "speed_au_per_year": sc["speed_au_per_year"],
            **state,
        })
    escaping.sort(key=lambda s: s["distance_au"], reverse=True)

    cruising = []
    for sc in config.CRUISING:
        origin = by_key.get(sc["origin"])
        target = by_key.get(sc["target"])
        if origin is None or target is None:
            continue  # 목표가 행성 목록에 없으면 이 미션만 건너뛴다

        # 발사점·도착점은 해당 날짜의 실제 계산 위치다.
        t_launch = orbits.centuries_since_j2000(orbits.parse_utc(sc["launch"]))
        t_arrival = orbits.centuries_since_j2000(orbits.parse_utc(sc["arrival"]))
        p_origin = orbits.planet_state(_planet_by_key(sc["origin"]), t_launch)
        p_target = orbits.planet_state(_planet_by_key(sc["target"]), t_arrival)

        progress = orbits.cruise_progress(sc, epoch)
        cruising.append({
            "key": sc["key"], "name": sc["name"], "name_ko": sc["name_ko"],
            "agency": sc["agency"], "note": sc["note"],
            "origin": sc["origin"], "origin_ko": origin["name_ko"],
            "target": sc["target"], "target_ko": target["name_ko"],
            "launch": sc["launch"], "arrival": sc["arrival"],
            "from_xy": [round(p_origin["x_au"], 4), round(p_origin["y_au"], 4)],
            "to_xy": [round(p_target["x_au"], 4), round(p_target["y_au"], 4)],
            "progress": round(progress, 4),
        })
    cruising.sort(key=lambda s: s["progress"], reverse=True)

    stationed = []
    for sc in config.STATIONED:
        anchor = by_key.get(sc["anchor"])
        if anchor is None:
            continue
        # L2 는 태양-지구 연장선상 바깥쪽. 지구 방향 단위벡터에 offset 을 더한다.
        r = anchor["r_au"] or 1.0
        scale = (r + sc["offset_au"]) / r
        stationed.append({
            "key": sc["key"], "name": sc["name"], "name_ko": sc["name_ko"],
            "short": sc["short"], "note": sc["note"],
            "anchor": sc["anchor"], "offset_au": sc["offset_au"],
            "x_au": round(anchor["x_au"] * scale, 5),
            "y_au": round(anchor["y_au"] * scale, 5),
        })

    return {
        "note": SPACECRAFT_NOTE,
        "omitted": config.OMITTED_MISSIONS,
        "escaping": escaping,
        "cruising": cruising,
        "stationed": stationed,
    }


_PLANETS_BY_KEY = {p["key"]: p for p in config.PLANETS}


def _planet_by_key(key: str) -> dict:
    return _PLANETS_BY_KEY[key]


# ---------------------------------------------------------------------------
@router.get("/api/launches")
def launches(
    days: int = Query(90, ge=1, le=config.MAX_WINDOW_DAYS),
    provider: str = Query("", max_length=80),
    commercial_only: bool = Query(False),
) -> dict:
    """시간 창 + 발사사 필터를 적용한 발사 목록과 요약 통계."""
    result = store.query_launches(days=days, provider=provider, commercial_only=commercial_only)
    return {
        "past": result["past"],
        "upcoming": result["upcoming"][:12],
        "summary": result["summary"],
        "filters": {"days": days, "provider": provider, "commercial_only": commercial_only},
        "max_window_days": config.MAX_WINDOW_DAYS,
    }


# ---------------------------------------------------------------------------
@router.get("/api/leo")
def leo() -> dict:
    """활성 우주정거장의 실시간 궤도 상태 + 역대 정거장 요약."""
    now = datetime.now(timezone.utc)
    ll2_by_name = {s["name"]: s for s in store.all_stations()}

    stations = []
    for cluster in store.orbital_clusters().values():
        anchor_info = config.STATION_ANCHORS.get(cluster["anchor_name"], {})
        ll2 = ll2_by_name.get(anchor_info.get("ll2_name", ""), {})

        try:
            state = orbits.station_state(cluster["elements"], now)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue  # 요소가 깨진 클러스터만 건너뛴다

        stations.append(
            {
                "key": cluster["key"],
                "name": cluster["anchor_name"],
                "name_ko": cluster["name_ko"],
                "short": cluster["short"],
                "status": ll2.get("status", "unknown"),
                "owner": ll2.get("owner", "unknown"),
                "founded": ll2.get("founded", "unknown"),
                "orbit": ll2.get("orbit", "unknown"),
                "active_expeditions": ll2.get("active_expeditions", 0),
                "module_count": len(cluster["modules"]),
                "modules": cluster["modules"],
                "docked": cluster["docked"],
                "object_count": cluster["object_count"],
                **state,
            }
        )

    stations.sort(key=lambda s: s["key"])

    history = sorted(
        (
            {
                "name": s["name"],
                "status": s["status"],
                "founded": s["founded"],
                "owner": s["owner"],
            }
            for s in store.all_stations()
        ),
        key=lambda s: s["founded"],
    )
    active = [h for h in history if h["status"] == "Active"]

    return {
        "epoch": now.isoformat(),
        "note": LEO_NOTE,
        "stations": stations,
        "history": history,
        "station_total": len(history),
        "station_active": len(active),
    }


# ---------------------------------------------------------------------------
@router.post("/api/brief")
async def brief(
    request: Request,
    days: int = Query(90, ge=1, le=config.MAX_WINDOW_DAYS),
) -> dict:
    """한국어 우주개발 시황 브리핑. 같은 10분 버킷 동안 결과를 재사용한다.

    lifespan 이 만든 httpx 클라이언트를 재사용한다 — 요청마다 새 클라이언트를
    만들면 커넥션 풀과 TLS 핸드셰이크를 매번 버리게 된다.
    """
    return await llm.generate_brief(request.app.state.http, days=days)
