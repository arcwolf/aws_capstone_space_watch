"""궤도 계산 — 순수 함수만. 네트워크도, 전역 상태도, 프레임워크도 모른다.

세 종류의 계산이 있다.

  (a) 행성 위치: J2000 평균 궤도요소로 케플러 방정식을 풀어 황도면 좌표를 얻는다.
      궤도경사각과 승교점 경도는 쓰지 않는다. 위에서 내려다보는 뷰에서 최대 7도
      (수성) 기울기는 구분되지 않으므로, 상수를 절반으로 줄이는 편이 낫다.

  (b) 정거장 궤도 상태: CelesTrak GP 요소에서 고도/속도/주기/궤도상 위치를 유도한다.
      SGP4가 아니다. 대기항력과 J2 섭동을 무시하므로 epoch에서 몇 시간 멀어지면
      수백 km 오차가 난다. 화면에 '표시용 근사'로 명시할 것.

  (c) 우주선: 탈출 항적은 쌍곡선 궤도라 감속이 없어 거리가 시간의 1차 함수다.
      순항 미션은 발사일/도착일의 실제 천체 위치를 양 끝점으로 삼고 경과율만 낸다
      (마커 보간은 프런트엔드가 한다).

이 모듈이 분리돼 있으면 "화성 위치가 틀렸다"는 문제를 서버도, 스토어도 띄우지 않고
파이썬 한 줄로 확인할 수 있다.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# 지구 중력상수와 평균 반지름
MU_EARTH_KM3_S2 = 398600.4418
R_EARTH_KM = 6371.0

J2000_JD = 2451545.0
DAYS_PER_CENTURY = 36525.0
SECONDS_PER_DAY = 86400.0


# ---------------------------------------------------------------------------
# 시간
# ---------------------------------------------------------------------------
def julian_day(dt: datetime) -> float:
    """UTC datetime → 율리우스일. tz가 없으면 UTC로 간주한다."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)

    y, m = dt.year, dt.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    day_fraction = (
        dt.day
        + (dt.hour + (dt.minute + (dt.second + dt.microsecond / 1e6) / 60.0) / 60.0)
        / 24.0
    )
    return (
        math.floor(365.25 * (y + 4716))
        + math.floor(30.6001 * (m + 1))
        + day_fraction
        + b
        - 1524.5
    )


def centuries_since_j2000(dt: datetime) -> float:
    return (julian_day(dt) - J2000_JD) / DAYS_PER_CENTURY


# ---------------------------------------------------------------------------
# 케플러 방정식
# ---------------------------------------------------------------------------
def _norm_deg(deg: float) -> float:
    """각도를 [-180, 180) 으로 정규화. 케플러 반복의 수렴을 위해 필요하다."""
    return (deg + 180.0) % 360.0 - 180.0


def solve_kepler(mean_anomaly_rad: float, e: float, iterations: int = 5) -> float:
    """E - e*sin(E) = M 을 뉴턴 반복으로 푼다.

    e < 0.3 인 태양계 천체와 원에 가까운 LEO 위성 모두 3회면 배정도 한계에
    도달한다. 여유로 5회를 돈다 — 비용은 sin/cos 다섯 쌍이다.
    """
    E = mean_anomaly_rad
    for _ in range(iterations):
        denom = 1.0 - e * math.cos(E)
        if abs(denom) < 1e-12:  # e -> 1 인 극단적 궤도. 여기 오는 입력은 없다.
            break
        E -= (E - e * math.sin(E) - mean_anomaly_rad) / denom
    return E


def true_anomaly_from_E(E: float, e: float) -> float:
    """이심근점이각 → 진근점이각 (rad)."""
    return math.atan2(math.sqrt(max(0.0, 1.0 - e * e)) * math.sin(E), math.cos(E) - e)


# ---------------------------------------------------------------------------
# (a) 행성
# ---------------------------------------------------------------------------
def _element(pair: tuple[float, float], T: float) -> float:
    """(J2000 값, 세기당 변화율) → T 시점 값."""
    return pair[0] + pair[1] * T


def planet_state(planet: dict, T: float) -> dict:
    """행성 하나의 황도면 좌표와 동경.

    반환 단위: 거리 AU, 각도 도(degree).
    """
    a = _element(planet["a"], T)
    e = _element(planet["e"], T)
    L = _element(planet["L"], T)
    peri = _element(planet["peri"], T)

    M = math.radians(_norm_deg(L - peri))
    E = solve_kepler(M, e)
    nu = true_anomaly_from_E(E, e)
    r = a * (1.0 - e * math.cos(E))

    lon = nu + math.radians(peri)  # 황도 경도 (경사각 무시하므로 곧 진경도)
    return {
        "a_au": a,
        "e": e,
        "r_au": r,
        "lon_deg": math.degrees(lon) % 360.0,
        "x_au": r * math.cos(lon),
        "y_au": r * math.sin(lon),
    }


def orbit_path(planet: dict, T: float, samples: int = 180) -> list[list[float]]:
    """궤도를 실제 타원 폴리라인으로 뽑는다. **평균근점이각을 균등 샘플링한다.**

    진근점이각을 균등 샘플링하는 편이 점 간격은 고르지만, 그러면 이 배열은
    '모양'만 담고 '시간'을 담지 못한다. 평균근점이각은 시간에 비례하므로,
    이렇게 뽑은 점들은 **등시간 간격**이 된다. 그 결과:

      - 타원 모양은 완전히 동일하다(점 밀도만 바뀐다 — 원일점에 촘촘, 근일점에 성기게).
      - 프런트엔드가 인덱스를 일정 속도로 전진시키기만 하면 근일점에서 빨라지고
        원일점에서 느려지는 케플러 제2법칙이 자동으로 성립한다.
      - 따라서 애니메이션을 위해 JS 에 케플러 솔버를 복제할 필요가 없다.

    현재 위치가 이 배열의 어디인지는 mean_anomaly_phase() 가 알려준다.
    """
    a = _element(planet["a"], T)
    e = _element(planet["e"], T)
    peri = math.radians(_element(planet["peri"], T))

    path: list[list[float]] = []
    for i in range(samples):
        M = 2.0 * math.pi * i / samples
        E = solve_kepler(M, e)
        nu = true_anomaly_from_E(E, e)
        r = a * (1.0 - e * math.cos(E))
        lon = nu + peri
        path.append([round(r * math.cos(lon), 5), round(r * math.sin(lon), 5)])
    return path


def orbital_period_days(a_au: float) -> float:
    """케플러 제3법칙: P[년] = a[AU]^1.5. 수성 87.97일, 명왕성 90,560일."""
    return 365.25 * (a_au ** 1.5)


def mean_anomaly_phase(planet: dict, T: float) -> float:
    """지금이 궤도 주기의 몇 번째 지점인가 (0~1).

    orbit_path() 가 등시간 간격이므로 이 값에 samples 를 곱하면 곧 배열 인덱스다.
    """
    L = _element(planet["L"], T)
    peri = _element(planet["peri"], T)
    return ((L - peri) % 360.0) / 360.0


# ---------------------------------------------------------------------------
# (c) 우주선
# ---------------------------------------------------------------------------
def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def years_between(start: str, end: datetime) -> float:
    return (end - parse_utc(start)).total_seconds() / (365.25 * SECONDS_PER_DAY)


def escaping_state(sc: dict, now: datetime) -> dict:
    """탈출 항적 위 위치. 거리는 시간의 1차 함수다.

    태양 중력에서 이미 벗어난 쌍곡선 궤도이므로 감속이 사실상 없다 — 연 단위로
    선형 외삽해도 오차가 무시할 만하다(보이저 1 은 연 3.58 AU 로 거의 등속).
    황경(방향)은 근사값이며 화면에 명시한다.
    """
    distance = sc["distance_au"] + sc["speed_au_per_year"] * years_between(sc["epoch"], now)
    lon = math.radians(sc["lon_deg"])
    return {
        "distance_au": round(distance, 2),
        "x_au": round(distance * math.cos(lon), 3),
        "y_au": round(distance * math.sin(lon), 3),
    }


def cruise_progress(sc: dict, now: datetime) -> float:
    """임무 경과율 0~1. 발사 전이면 0, 도착 후면 1."""
    launch = parse_utc(sc["launch"])
    arrival = parse_utc(sc["arrival"])
    span = (arrival - launch).total_seconds()
    if span <= 0:
        return 1.0
    return max(0.0, min(1.0, (now - launch).total_seconds() / span))


# ---------------------------------------------------------------------------
# (b) 정거장 / 위성
# ---------------------------------------------------------------------------
def parse_gp_epoch(epoch: str) -> datetime:
    """CelesTrak GP epoch 문자열 → UTC datetime.

    관측된 형식은 '2026-09-01T19:42:22.677120' 로 타임존 표기가 없다. UTC다.
    """
    text = epoch.replace("Z", "")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def station_state(gp: dict, now: datetime | None = None) -> dict:
    """GP 요소 → 고도/속도/주기/궤도상 위치.

    SGP4가 아니다. 평균운동을 그대로 케플러 평균운동으로 취급하고 평균근점이각을
    선형 전파한다. 표시 목적으로는 충분하지만 추적에는 쓸 수 없다.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    n_rev_day = float(gp["MEAN_MOTION"])
    e = float(gp.get("ECCENTRICITY", 0.0))
    inc = float(gp.get("INCLINATION", 0.0))
    m0_deg = float(gp.get("MEAN_ANOMALY", 0.0))

    n_rad_s = n_rev_day * 2.0 * math.pi / SECONDS_PER_DAY
    a_km = (MU_EARTH_KM3_S2 / (n_rad_s * n_rad_s)) ** (1.0 / 3.0)

    dt_s = (now - parse_gp_epoch(gp["EPOCH"])).total_seconds()
    M = math.radians(m0_deg) + n_rad_s * dt_s
    M = math.atan2(math.sin(M), math.cos(M))  # [-pi, pi) 로 되감기

    E = solve_kepler(M, e)
    nu = true_anomaly_from_E(E, e)
    r_km = a_km * (1.0 - e * math.cos(E))
    v_km_s = math.sqrt(MU_EARTH_KM3_S2 * (2.0 / r_km - 1.0 / a_km))

    return {
        "semi_major_km": round(a_km, 1),
        "altitude_km": round(r_km - R_EARTH_KM, 1),
        "speed_kmh": round(v_km_s * 3600.0, 0),
        "period_min": round(1440.0 / n_rev_day, 2),
        "inclination_deg": round(inc, 2),
        "eccentricity": e,
        "true_anomaly_deg": round(math.degrees(nu) % 360.0, 1),
        "epoch": gp["EPOCH"],
        "age_hours": round(dt_s / 3600.0, 1),
    }


# ---------------------------------------------------------------------------
# 자체 점검 — python -m app.orbits 로 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    from . import config

    T = centuries_since_j2000(datetime.now(timezone.utc))
    print(f"T = {T:.6f} centuries since J2000\n")
    print(f"{'천체':<8} {'r (AU)':>9} {'황경 (deg)':>11}")
    for p in config.PLANETS:
        s = planet_state(p, T)
        print(f"{p['name_ko']:<8} {s['r_au']:>9.4f} {s['lon_deg']:>11.2f}")

    print("\n지구 동경은 0.983(근일점)~1.017(원일점) 사이여야 한다.")
