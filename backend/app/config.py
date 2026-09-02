"""Space Watch 설정 — 엔드포인트, 주기, 상한, 그리고 큐레이션 데이터.

여기 있는 값은 두 종류다.
  - 운영 파라미터: 환경변수로 덮어쓸 수 있다.
  - 큐레이션/천문 상수: 코드에 박아둔다. 라이브 API가 없거나(도달 마일스톤),
    있어도 값이 변하지 않기 때문(J2000 궤도요소).
"""

import os

# ---------------------------------------------------------------------------
# 수집 대상 (모두 인증 불필요)
# ---------------------------------------------------------------------------
LL2_BASE = "https://ll.thespacedevs.com/2.2.0"
LL2_PREVIOUS_URL = f"{LL2_BASE}/launch/previous/?limit=100"
LL2_UPCOMING_URL = f"{LL2_BASE}/launch/upcoming/?limit=20"
LL2_STATIONS_URL = f"{LL2_BASE}/spacestation/?limit=20"
CELESTRAK_STATIONS_URL = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=json"
)

# LL2 익명 한도는 15 req/hour. 사이클당 4요청이므로 20분 주기 = 12 req/hr.
POLL_INTERVAL_SECONDS = int(os.environ.get("SW_POLL_INTERVAL", "1200"))
HTTP_TIMEOUT_SECONDS = 30.0

# 기동 시 이 나이보다 어린 디스크 캐시가 있으면 네트워크를 건드리지 않는다.
# 개발 중 서버 재시작과 Fargate 태스크 재기동이 레이트리밋 쿼터를 태우지 않게 한다.
CACHE_DIR = os.environ.get("SW_CACHE_DIR", "/tmp/space-watch-cache")
CACHE_MAX_AGE_SECONDS = int(os.environ.get("SW_CACHE_MAX_AGE", "3600"))

MAX_LAUNCHES = 300
MAX_WINDOW_DAYS = 110  # /launch/previous/?limit=100 이 실제로 덮는 기간

# ---------------------------------------------------------------------------
# Bedrock (Converse REST, SDK 없이 httpx 직접 호출)
# ---------------------------------------------------------------------------
BEDROCK_REGION = os.environ.get("SW_BEDROCK_REGION", "ap-northeast-2")
BEDROCK_MODEL_ID = os.environ.get(
    "SW_BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
)
BEDROCK_URL = (
    f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
    f"/model/{BEDROCK_MODEL_ID}/converse"
)
BEDROCK_MAX_TOKENS = 700
BEDROCK_TIMEOUT_SECONDS = 60.0

# 키의 거처: 환경변수만. 파일에도, 이미지에도, IaC에도 값을 두지 않는다.
def bedrock_bearer_token() -> str:
    """호출 시점에 읽는다 — 모듈 로드 시점에 캐시하면 컨테이너에서 주입이 늦을 때 빈 값이 굳는다."""
    return os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")


BRIEF_CACHE_BUCKET_SECONDS = 600  # 같은 10분 버킷 동안 결과 재사용

# ---------------------------------------------------------------------------
# 행성 궤도요소 (J2000 평균요소 + 세기당 변화율)
#
# 형식: (a[AU], e, L[deg], peri[deg]) 와 각각의 세기당 변화율.
# 궤도경사각(I)과 승교점 경도(Node)는 담지 않는다 — 황도면을 위에서 내려다보는
# 뷰에서 최대 7도(수성) 기울기는 시각적으로 구분되지 않으므로, 상수를 절반으로
# 줄이는 편이 낫다. 이 근사는 화면에 명시한다.
# ---------------------------------------------------------------------------
PLANETS = [
    {
        "key": "mercury", "name": "Mercury", "name_ko": "수성",
        "radius_km": 2440, "is_dwarf": False,
        "a": (0.38709927, 0.00000037),
        "e": (0.20563593, 0.00001906),
        "L": (252.25032350, 149472.67411175),
        "peri": (77.45779628, 0.16047689),
    },
    {
        "key": "venus", "name": "Venus", "name_ko": "금성",
        "radius_km": 6052, "is_dwarf": False,
        "a": (0.72333566, 0.00000390),
        "e": (0.00677672, -0.00004107),
        "L": (181.97909950, 58517.81538729),
        "peri": (131.60246718, 0.00268329),
    },
    {
        "key": "earth", "name": "Earth", "name_ko": "지구",
        "radius_km": 6371, "is_dwarf": False,
        "a": (1.00000261, 0.00000562),
        "e": (0.01671123, -0.00004392),
        "L": (100.46457166, 35999.37244981),
        "peri": (102.93768193, 0.32327364),
    },
    {
        "key": "mars", "name": "Mars", "name_ko": "화성",
        "radius_km": 3390, "is_dwarf": False,
        "a": (1.52371034, 0.00001847),
        "e": (0.09339410, 0.00007882),
        "L": (-4.55343205, 19140.30268499),
        "peri": (-23.94362959, 0.44441088),
    },
    {
        "key": "jupiter", "name": "Jupiter", "name_ko": "목성",
        "radius_km": 69911, "is_dwarf": False,
        "a": (5.20288700, -0.00011607),
        "e": (0.04838624, -0.00013253),
        "L": (34.39644051, 3034.74612775),
        "peri": (14.72847983, 0.21252668),
    },
    {
        "key": "saturn", "name": "Saturn", "name_ko": "토성",
        "radius_km": 58232, "is_dwarf": False,
        "a": (9.53667594, -0.00125060),
        "e": (0.05386179, -0.00050991),
        "L": (49.95424423, 1222.49362201),
        "peri": (92.59887831, -0.41897216),
    },
    {
        "key": "uranus", "name": "Uranus", "name_ko": "천왕성",
        "radius_km": 25362, "is_dwarf": False,
        "a": (19.18916464, -0.00196176),
        "e": (0.04725744, -0.00004397),
        "L": (313.23810451, 428.48202785),
        "peri": (170.95427630, 0.40805281),
    },
    {
        "key": "neptune", "name": "Neptune", "name_ko": "해왕성",
        "radius_km": 24622, "is_dwarf": False,
        "a": (30.06992276, 0.00026291),
        "e": (0.00859048, 0.00005105),
        "L": (-55.12002969, 218.45945325),
        "peri": (44.96476227, -0.32241464),
    },
    {
        "key": "pluto", "name": "Pluto", "name_ko": "명왕성",
        "radius_km": 1188, "is_dwarf": True,
        "a": (39.48211675, -0.00031596),
        "e": (0.24882730, 0.00005170),
        "L": (238.92903833, 145.20780515),
        "peri": (224.06891629, -0.04062942),
    },
]

# 스케일 기준: 가장 먼 궤도(명왕성 반장축)
OUTERMOST_A_AU = 39.48211675

# ---------------------------------------------------------------------------
# 큐레이션: 인류 도달 등급
#
# 라이브 API가 존재하지 않는 영역이다. 대신 역사적 사실이라 변하지 않으므로
# 하드코딩이 오히려 정확하다. 화면에 "큐레이션 데이터" 라벨을 붙인다.
#
# tier 0 미도달 / 1 플라이바이 / 2 궤도선 / 3 착륙 / 4 유인
# ---------------------------------------------------------------------------
TIER_LABELS = {
    0: "미도달",
    1: "플라이바이",
    2: "궤도선",
    3: "착륙",
    4: "유인",
}

REACH = {
    "mercury": {
        "tier": 2, "label": "궤도선",
        "milestones": [
            ("1974", "마리너 10, 최초 플라이바이"),
            ("2011", "메신저, 최초 궤도 진입"),
        ],
    },
    "venus": {
        "tier": 3, "label": "무인 착륙",
        "milestones": [
            ("1970", "베네라 7, 타 행성 최초 연착륙"),
            ("1982", "베네라 13, 지표 컬러 영상 전송"),
        ],
    },
    "earth": {
        "tier": 4, "label": "유인 · 상주",
        "milestones": [
            ("1961", "가가린, 최초 유인 우주비행"),
            ("1998", "ISS 조립 개시 — 이후 무중단 상주"),
        ],
    },
    "moon": {
        "tier": 4, "label": "유인 착륙",
        "milestones": [
            ("1969", "아폴로 11, 최초 유인 착륙"),
            ("2019", "창어 4, 최초 달 뒷면 착륙"),
        ],
    },
    "mars": {
        "tier": 3, "label": "착륙 · 로버",
        "milestones": [
            ("1976", "바이킹 1, 최초 착륙 후 임무 수행"),
            ("1997", "소저너, 최초 로버 주행"),
            ("2021", "인지뉴이티, 타 행성 최초 동력 비행"),
        ],
    },
    "jupiter": {
        "tier": 2, "label": "궤도선 · 대기 진입",
        "milestones": [
            ("1973", "파이어니어 10, 최초 플라이바이"),
            ("1995", "갈릴레오 프로브, 대기 진입"),
            ("2016", "주노, 극궤도 진입"),
        ],
    },
    "saturn": {
        "tier": 3, "label": "위성 착륙 (타이탄)",
        "milestones": [
            ("1979", "파이어니어 11, 최초 플라이바이"),
            ("2004", "카시니, 최초 궤도 진입"),
            ("2005", "하위헌스, 타이탄 착륙"),
        ],
    },
    "uranus": {
        "tier": 1, "label": "플라이바이",
        "milestones": [("1986", "보이저 2, 유일한 근접 통과")],
    },
    "neptune": {
        "tier": 1, "label": "플라이바이",
        "milestones": [("1989", "보이저 2, 유일한 근접 통과")],
    },
    "pluto": {
        "tier": 1, "label": "플라이바이",
        "milestones": [("2015", "뉴호라이즌스, 최초 근접 통과")],
    },
}

# 달은 지구 궤도요소를 따로 계산하지 않고, 지구 위치 옆에 고정 오프셋으로 그린다.
# 태양계 스케일에서 달-지구 거리(0.0026 AU)는 어차피 1픽셀 미만이다.
MOON = {
    "key": "moon", "name": "Moon", "name_ko": "달",
    "radius_km": 1737, "parent": "earth",
}

# ---------------------------------------------------------------------------
# 반경 스케일 — 구간 스케일이다(축을 끊는다). 화면에 경계를 명시할 것.
#
# 보이저 1 의 168 AU 를 하나의 연속 스케일로 덮으면 명왕성이 반경 60% 지점으로
# 내려오고 내행성이 다시 한 점에 뭉친다. 그래서 두 구간으로 나눈다.
#   0 ~ 40 AU  : 세제곱근 (행성계)
#   40 ~ 180 AU: 로그      (탈출 항적)
# 정직성 장치 3개: 경계에 굵은 링 + 라벨, 경계 밖 배경 톤 변경, 우주선마다 실제 AU 표기.
# ---------------------------------------------------------------------------
SCALE_BREAK_AU = 40.0
SCALE_OUTER_MAX_AU = 180.0

# ---------------------------------------------------------------------------
# 큐레이션: 태양계를 탈출 중인 우주선
#
# 거리와 속도는 확실한 값이다(쌍곡선 궤도라 감속이 사실상 없어 선형 외삽이 유효).
# 황경(방향)은 근사값이며 화면에 명시한다. 황위는 황도면 투영이므로 버린다
# (보이저 2 는 황위 -59°, 보이저 1 은 +35° 로 실제로는 크게 벗어나 있다).
# ---------------------------------------------------------------------------
ESCAPING = [
    {
        "key": "voyager1", "name": "Voyager 1", "name_ko": "보이저 1",
        "epoch": "2026-01-01", "distance_au": 167.5, "speed_au_per_year": 3.58,
        "lon_deg": 254.5, "lat_deg": 35.0,
        "status": "성간공간 · 운용 중",
        "milestone": ("2012", "태양권계면 통과 — 인류 최초 성간 진입"),
    },
    {
        "key": "pioneer10", "name": "Pioneer 10", "name_ko": "파이어니어 10",
        "epoch": "2026-01-01", "distance_au": 143.0, "speed_au_per_year": 2.54,
        "lon_deg": 78.0, "lat_deg": 3.0,
        "status": "통신 종료 (2003)",
        "milestone": ("1973", "목성 최초 플라이바이"),
    },
    {
        "key": "voyager2", "name": "Voyager 2", "name_ko": "보이저 2",
        "epoch": "2026-01-01", "distance_au": 140.0, "speed_au_per_year": 3.24,
        "lon_deg": 290.0, "lat_deg": -59.0,
        "status": "성간공간 · 운용 중",
        "milestone": ("1989", "해왕성 — 유일한 근접 통과"),
    },
    {
        "key": "pioneer11", "name": "Pioneer 11", "name_ko": "파이어니어 11",
        "epoch": "2026-01-01", "distance_au": 120.0, "speed_au_per_year": 2.32,
        "lon_deg": 291.0, "lat_deg": 17.0,
        "status": "통신 종료 (1995)",
        "milestone": ("1979", "토성 최초 플라이바이"),
    },
    {
        "key": "newhorizons", "name": "New Horizons", "name_ko": "뉴호라이즌스",
        "epoch": "2026-01-01", "distance_au": 62.5, "speed_au_per_year": 2.86,
        "lon_deg": 293.0, "lat_deg": -2.0,
        "status": "카이퍼 벨트 · 운용 중",
        "milestone": ("2015", "명왕성 최초 근접 통과"),
    },
]

# ---------------------------------------------------------------------------
# 큐레이션: 순항 중인 심우주 미션
#
# 양 끝점(발사일 지구 위치, 도착일 목표 위치)은 케플러로 계산한 실제값이다.
# 마커는 그 사이를 임무 경과율로 선형 보간한 것이며 **실제 궤적이 아니다** —
# 실제 항로는 중력도움을 여러 번 쓰는 나선이다. 화면에 반드시 명시할 것.
#
# 목표가 행성이 아닌 미션(프시케, 오시리스-아펙스 등)은 도착점을 정직하게
# 계산할 수 없어 도표에서 생략하고 각주로만 언급한다.
# ---------------------------------------------------------------------------
CRUISING = [
    {
        "key": "bepicolombo", "name": "BepiColombo", "name_ko": "베피콜롬보",
        "origin": "earth", "target": "mercury",
        "launch": "2018-10-20", "arrival": "2026-11-21",
        "agency": "ESA · JAXA", "note": "수성 궤도 진입 예정",
    },
    {
        "key": "lucy", "name": "Lucy", "name_ko": "루시",
        "origin": "earth", "target": "jupiter",
        "launch": "2021-10-16", "arrival": "2027-08-12",
        "agency": "NASA", "note": "목성 트로이 소행성군 첫 조우",
    },
    {
        "key": "clipper", "name": "Europa Clipper", "name_ko": "유로파 클리퍼",
        "origin": "earth", "target": "jupiter",
        "launch": "2024-10-14", "arrival": "2030-04-11",
        "agency": "NASA", "note": "목성 도착 후 유로파 근접 통과 반복",
    },
    {
        "key": "juice", "name": "JUICE", "name_ko": "주스",
        "origin": "earth", "target": "jupiter",
        "launch": "2023-04-14", "arrival": "2031-07-01",
        "agency": "ESA", "note": "가니메데 궤도 진입 목표",
    },
]

OMITTED_MISSIONS = (
    "프시케(2029 소행성 16 프시케 도착), 오시리스-아펙스(2029 아포피스), "
    "하야부사2 확장 미션 — 목표가 행성이 아니어서 도착점을 계산할 수 없다"
)

# ---------------------------------------------------------------------------
# 큐레이션: 라그랑주점 상주 관측소 — 지구 위치에서 계산하는 실제 기하
# ---------------------------------------------------------------------------
STATIONED = [
    {
        "key": "jwst", "name": "James Webb Space Telescope", "name_ko": "제임스 웹 우주망원경",
        "short": "JWST", "anchor": "earth", "offset_au": 0.01,
        "note": "지구-태양 L2 · 2021 발사 · 태양 반대편 150만 km",
    },
]

# 성간공간 경계 표식 — 보이저 1의 도달을 태양계 외곽 점선으로 표현한다.
INTERSTELLAR = {
    "label_ko": "성간공간",
    "milestone": ("2012", "보이저 1, 태양권계면 통과"),
    "radius_au": 123.0,  # 태양권계면 대략 거리 (보이저 1 통과 지점)
}

# ---------------------------------------------------------------------------
# 궤도 클러스터링 — CelesTrak stations 그룹 22개를 정거장 단위로 묶기
#
# 같은 평균운동/경사각을 가진 객체는 같은 궤도를 돈다 = 도킹돼 있다는 뜻이다.
# 대표 객체(anchor)만 궤도에 그리고, 나머지는 모듈/도킹 우주선으로 분류한다.
# ---------------------------------------------------------------------------
# ll2_name 은 LL2 /spacestation/ 응답의 name 과 정확히 일치해야 한다.
# 실측 확인: Active 상태인 정거장은 이 두 개뿐이다(나머지 13개는 De-Orbited/Decommissioned).
STATION_ANCHORS = {
    "ISS (ZARYA)": {
        "key": "iss",
        "name_ko": "국제우주정거장",
        "short": "ISS",
        "ll2_name": "International Space Station",
    },
    "CSS (TIANHE)": {
        "key": "css",
        "name_ko": "톈궁 우주정거장",
        "short": "CSS",
        "ll2_name": "Tiangong space station",
    },
}

# anchor와 같은 궤도에 있으면서 이 키워드에 걸리면 '구성 모듈',
# 걸리지 않으면 '도킹 중인 우주선'으로 본다.
MODULE_KEYWORDS = (
    "ZARYA", "NAUKA", "POISK", "RASSVET", "ZVEZDA", "PIRS", "PRICHAL",
    "HARMONY", "COLUMBUS", "KIBO", "UNITY", "TRANQUILITY", "DESTINY",
    "QUEST", "LEONARDO", "BEAM", "IDA", "NANORACKS", "BISHOP",
    "TIANHE", "WENTIAN", "MENGTIAN",
    "ISS (", "CSS (",
)

# 궤도 클러스터로 인식할 최소 객체 수 (파편/개별 큐브샛 제외)
MIN_CLUSTER_SIZE = 3
