# Space Watch — 구현 계획

설계 문서: `../specs/2026-09-02-space-watch-design.md`
작성일: 2026-09-02

자동화 테스트는 생략한다(설계 §11). 각 단계는 `curl` + `jq` 또는 브라우저로 검증한다.

---

## Phase 0 — 환경 (완료)

- [x] `backend/app`, `frontend`, `infra/{bin,lib}`, `docs/superpowers/{specs,plans}` 생성
- [x] `uv venv --python 3.11 .venv` → CPython 3.11.16
- [x] `backend/requirements.txt` 작성, `uv pip install` → fastapi 0.115.6 / httpx 0.28.1 / uvicorn 0.34.0
- [x] `sudo dnf install -y docker` + `systemctl enable --now docker` + `usermod -aG docker ec2-user`
- [x] `~/.bashrc`에 `AWS_BEARER_TOKEN_BEDROCK` 등록, Bedrock Converse 200 실증

---

## Phase 1 — 백엔드 (목표 45분)

### 1.1 `backend/app/config.py`

- [x] 엔드포인트 상수 4개 + 폴링 주기 1200초 + 발사 상한 300 + 캐시 디렉터리/최대수명
- [x] Bedrock 상수: 리전 `ap-northeast-2`, 모델 `global.anthropic.claude-sonnet-4-6`,
      URL 조립, `maxTokens 700`, 캐시 버킷 600초, `AWS_BEARER_TOKEN_BEDROCK` 읽기
- [x] `PLANETS`: J2000 평균 궤도요소 `(a, e, L, ϖ)` + 세기당 변화율. 수성~해왕성 + 명왕성.
      각 항목에 `name_ko`, `radius_km`, `is_dwarf`
- [x] `REACH`: 천체별 `(tier, label, milestones[])` 큐레이션
- [x] `STATION_ANCHORS`: `{"ISS (ZARYA)": (...), "CSS (TIANHE)": (...)}` 화이트리스트
- [x] `MODULE_KEYWORDS`: 도킹/모듈 구분용 키워드 집합

**검증**: `python -c "from app import config; print(len(config.PLANETS), len(config.REACH))"`

### 1.2 `backend/app/orbits.py` (순수 함수, I/O 없음)

- [x] `julian_day(dt)`, `centuries_since_j2000(dt)`
- [x] `solve_kepler(M_rad, e, iters=3)` — 뉴턴 반복
- [x] `planet_state(elements, T)` → `{x_au, y_au, r_au, lon_deg, e, a_au}`
- [x] `orbit_path(elements, T, n=60)` → 실제 타원 폴리라인 `[[x, y], …]`
- [x] `station_state(gp_elements, now)` → `{altitude_km, speed_kmh, period_min, true_anomaly_deg, semi_major_km}`

**검증**: 지구의 `r_au`가 0.98~1.02, ISS 고도가 380~430km, 속도가 27,000~28,000km/h 범위인지
파이썬 한 줄로 확인

### 1.3 `backend/app/store.py`

- [x] 모듈 전역 스냅샷 딕셔너리 + 메타(`_last_new_ids`, `_last_collected_at`, `_last_error`, `_cycle_count`)
- [x] `upsert_launches(items) -> list[str]` — id 멱등, 300건 초과 시 `net` 오래된 것부터 제거
- [x] `replace_stations(items)`, `replace_orbital(clusters)`
- [x] `query_launches(days, provider, commercial_only)` → `(past, upcoming, summary)`
- [x] `health()` — 항상 200용 딕셔너리
- [x] 락을 쓰지 않는 근거와 깨지는 조건을 주석으로 명시

**검증**: 같은 id를 두 번 upsert해도 건수가 늘지 않는지 확인

### 1.4 `backend/app/collector.py`

- [x] `normalize_launch(raw, kind)` — 실패 항목만 스킵, 나머지는 설계 §5 표대로 다듬기
- [x] `normalize_station(raw)`
- [x] `cluster_orbital(gp_list)` — `(round(mm,2), round(inc,1))` 그룹 → anchor/modules/docked
- [x] `_cache_read/_cache_write` — `$SW_CACHE_DIR`, 1시간 미만이면 네트워크 생략
- [x] `collect_once(client)` — `asyncio.gather(..., return_exceptions=True)`,
      실패 소스는 직전 스냅샷 유지 + `last_error` 기록
- [x] `run_poller(client)` — 20분 루프, 사이클 예외를 삼켜 루프를 죽이지 않음

**검증**: `python -m app.collector` 스모크 실행으로 4개 소스 건수 출력

### 1.5 `backend/app/api.py` + `main.py`

- [x] `GET /healthz` (항상 200)
- [x] `GET /api/solar-system?at=`
- [x] `GET /api/launches?days=&provider=&commercial_only=`
- [x] `GET /api/leo`
- [x] `main.py`: lifespan(httpx 클라이언트 생성 → 즉시 1회 수집 → 폴러 태스크 → 종료 시 취소),
      라우터 include **후에** `StaticFiles(html=True)`를 `/`에 마운트

**검증 (Phase 1 관문)**:
```bash
uvicorn app.main:app --port 8000 &
curl -s localhost:8000/healthz | jq
curl -s 'localhost:8000/api/solar-system' | jq '.bodies[] | {name_ko, r_au, tier: .reach.tier}'
curl -s 'localhost:8000/api/launches?days=90' | jq '.summary'
curl -s 'localhost:8000/api/leo' | jq '.stations[] | {name_ko, altitude_km, speed_kmh, docked}'
```
기대: SpaceX가 top_providers 1위, ISS 고도 약 400km, 도킹 목록에 Crew Dragon 계열 등장

---

## Phase 2 — 화면 (목표 50분)

- [x] CSS 변수 기반 다크/라이트 듀얼 테마 + 토글 + `localStorage['sw-theme']`
- [x] 상단 통계 바 5칸
- [x] 태양계 SVG: 폴리라인 궤도, 행성 원(로그 스케일 반지름), 도달등급 배지, 호버 툴팁,
      명왕성 점선
- [x] LEO 패널: 지구 원 + 경사각 기운 타원 + 위치 점 + 리드아웃 + 근사 각주
- [x] 발사사 점유율 div 바 차트 (상업/정부 색 구분)
- [x] 발사 테이블 (KST, 상태 배지, D-day)
- [x] 기간 슬라이더 + 상업만 토글 → API 재호출
- [x] 60초 폴링 (`Promise.all` 3개 GET)

**검증 (Phase 2 관문)**: Playwright로 `localhost:8000` 스크린샷 — 다크/라이트 각 1장.
태양계에 행성 9개, LEO에 정거장 2개, 차트에 SpaceX 최상단, 테이블에 행이 채워졌는지 육안 확인

---

## Phase 3 — 브리핑 (목표 20분)

- [x] `llm.py`: 프롬프트 조립(90일 요약 + 예정 5건 + 정거장/도킹 + 도달 요약)
- [x] httpx POST + Bearer 헤더 + `maxTokens 700`
- [x] 10분 버킷 캐시, 최신 버킷만 보관
- [x] 토큰 없음 → 503, Bedrock 오류 → 502 + 본문 앞 300자
- [x] `POST /api/brief` 라우트 + 프런트 브리핑 패널/버튼

**검증 (Phase 3 관문)**:
```bash
curl -sX POST localhost:8000/api/brief | jq -r '.brief'   # 한국어 브리핑 출력
curl -sX POST localhost:8000/api/brief | jq '.cached'      # true
```

---

## Phase 4 — 배포 (목표 45분)

- [x] `Dockerfile` — `python:3.11-slim`, backend + frontend 복사, uvicorn 실행
- [x] `docker build` + 로컬 컨테이너에서 `/healthz` 200 확인
- [x] `infra/` CDK TypeScript 초기화 (`aws-cdk-lib`, `constructs`)
- [x] `cdk.json` context에 `originVerifyValue` 생성값 기록
- [x] 스택: VPC(퍼블릭만, NAT 0) → Secret → ECS 클러스터/태스크(ARM64, desiredCount 1) →
      ALB(prefix list SG, 기본 403 + 헤더 규칙) → CloudFront(CACHING_DISABLED, ALLOW_ALL)
- [x] `cdk bootstrap` (필요 시) → `cdk deploy`
- [x] Secrets Manager에 Bearer 값 주입 → 서비스 재배포

**검증 (Phase 4 관문)**:
```bash
curl -s https://<cloudfront>/healthz | jq          # 200
curl -s http://<alb-dns>/healthz -o /dev/null -w '%{http_code}\n'   # 403 (직격 차단)
curl -sX POST https://<cloudfront>/api/brief | jq -r '.brief'        # Secrets Manager 주입 확인
```

---

## 중단 기준

다음 상황에서는 진행을 멈추고 사용자에게 묻는다:
- LL2 또는 CelesTrak이 지속적으로 4xx/5xx (레이트리밋 초과 포함)
- `cdk bootstrap`이 권한 부족으로 실패
- prefix list lookup이 리전에서 실패
- Phase 3 종료 시점에 누적 소요가 2시간 20분을 넘으면 Phase 4 진행 여부를 확인


---

# 실행 결과 (2026-09-02, 총 소요 약 72분)

## 배포된 엔드포인트

| 항목 | 값 |
|---|---|
| 공개 URL | https://<cloudfront-domain> |
| ALB DNS | <alb-dns-name> |
| 시크릿 | `space-watch/bedrock-bearer` (AWSCURRENT 주입 완료) |
| ECS 클러스터 / 서비스 | `<cluster-name>` / `<service-name>` |
| CloudFormation 스택 | `SpaceWatch` (리소스 30개, NAT Gateway 0개) |
| 로컬 개발 서버 | `http://localhost:8100` (`:8000`은 다른 프로젝트가 점유 중) |

## 관문 검증 결과

**Phase 1** — `orbits.py` 자체 점검으로 물리 검산:
- 지구 동경 1.0090 AU (원일점 1.0167 ~ 근일점 0.9833 사이), 황경 339.43° — 손 계산 339.45°와 일치
- ISS 고도 430km / 27,554 km/h / 92.97분 / 경사각 51.63° — 실제값
- 진근점이각 변화율 3.900°/분 (이론 3.872°, 차이는 이심률 효과), 반주기 뒤 +180.1°, 1주기 뒤 제자리
- 멱등성: 같은 100개 id 재수집 시 건수 120 유지(220이 아님)

**Phase 2** — 두 테마 스크린샷 육안 확인. 팔레트는 `dataviz` 검증기로 6개 검사 통과:
- 범주형 2계열(상업/정부): 두 모드 모두 ALL PASS
- LEO 2계열(ISS/CSS): 라이트 모드 대비 3:1 미만 WARN → relief 규칙에 따라 직접 라벨 상시 표시
- 도달등급 순서형 5단: 단일 색상(색상각 3°/4° 확산), 명도 단조, 인접 ΔL ≥ 0.06, 표면 대비 2:1 통과

**Phase 3** — `POST /api/brief`: 539/700 토큰, 523자, 완결 문장, 3문단. 2차 호출 `cached: true` (1.1ms).

**Phase 4**
- ALB 직격: `http=000`, curl exit 28 — SG가 패킷을 드롭(403보다 앞선 방어층)
- CloudFront 경유: `/healthz` 200, `/` 200(35,334 bytes), API 3종 정상, `POST /api/brief` 200
- 타깃 헬스: `<task-private-ip>:8000 healthy`
- 이미지: arm64, 196MB, `BEARER` 환경변수 0개

## 계획과 달랐던 점

1. **`:8000`이 점유돼 있었다** — 다른 프로젝트(`Market Desk on Web`)가 쓰고 있어 로컬 개발만 `:8100`으로 옮겼다.
   컨테이너·Fargate는 설계대로 8000이다.
2. **`uv venv`에는 pip가 없다** — `VIRTUAL_ENV=... uv pip install`로 설치했다. Dockerfile은 표준 pip를 쓰므로 무관.
3. **정찰용 캐시 파일이 `mode=list` 형식이었다** — 발사사가 전부 `unknown`으로 떨어졌다.
   정규화 코드에 두 스키마 분기를 넣는 대신 캐시를 올바른 형식으로 다시 채웠다.
4. **CDK `addListener`의 `open: true` 기본값이 prefix list 제한을 무력화했다** — `0.0.0.0/0:80`
   인바운드가 자동 추가돼 있었다. 보안 그룹 규칙은 합집합이라 넓은 쪽이 이긴다. `open: false`로 고쳤고,
   합성 템플릿 전수 검사로 `0.0.0.0/0` 부재를 확인했다.
5. **SG `GroupDescription`은 ASCII만 허용된다** — 한국어 설명은 배포 시 실패할 값이었다.
   AWS로 나가는 설명 문자열을 ASCII로 바꾸고 한국어는 코드 주석에 남겼다.
6. **`maxTokens 700`에 한국어 브리핑이 두 번 잘렸다** — 한국어는 대략 1토큰/글자다.
   토큰 상한은 스펙이므로 유지하고, 프롬프트에 문단별 글자 예산(170/220/160)을 실측 기반으로 넣었다.
7. **√ 스케일로는 내행성이 뭉쳤다** — 반장축 범위 101배를 √는 10배로만 줄인다.
   세제곱근(4.65배)으로 바꾸고, 금성-지구는 본질적으로 가까우므로 내행성계 확대 인셋을 추가했다.
8. **LEO 패널에 기하 오류가 있었다** — 경사각으로 납작하게 만들면서 동시에 회전시켜 경사각을 두 번 셌다.
   납작함은 고정 시점값(0.40)에서 오고 회전각만이 경사각을 나르도록 고쳤다.
9. **Playwright MCP가 채널 `chrome`을 찾지 못했다** — 번들 Chromium(`chromium-1234`)을 직접 실행했다.
   스크린샷용 `?theme=light|dark` 쿼리 파라미터를 추가했다(저장하지 않는 일회성 오버라이드).

## 남은 작업 / 알려진 한계

- 자동화 테스트 없음(시간 제약에 따른 의도적 결정, 설계 §11)
- CloudFront → ALB 구간 평문 HTTP (ACM 인증서·커스텀 도메인 없음)
- `X-Origin-Verify` 값이 `infra/cdk.json` context에 평문 — CloudFront 커스텀 헤더는 synth 시점 리터럴을 요구한다
- 위성 위치는 간이 케플러(SGP4 아님), 행성 위치는 황도면 투영(경사각 무시)
- 발사 통계는 최근 110일 100발 표본 (LL2 익명 15 req/hr 한도)
- '도킹 중'은 궤도요소 클러스터링 추론 (공식 도킹 데이터 아님)
- `git`이 초기화돼 있지 않다 — 커밋은 사용자 요청 시

---

# 후속 변경 — 태양계 패널 동적화 (2026-09-02, 약 17분)

분류: **bounded** (기존 `drawSolar()` / `/api/solar-system` / `config.REACH` 흐름을 확장).
스펙 문서 없이 채팅 설계 + 승인으로 진행.

## 핵심 설계 결정

**`orbit_path` 의 샘플링 기준을 진근점이각 → 평균근점이각으로 바꿨다.** 평균근점이각은 시간에
비례하므로 폴리라인의 점들이 등시간 간격이 된다. 타원 모양은 동일하고 점 밀도만 바뀐다.
그 결과 프런트엔드가 인덱스를 일정 속도로 전진시키기만 하면 케플러 제2법칙이 자동 성립하고,
**JS 에 케플러 솔버를 복제할 필요가 없어진다.** 백엔드 3줄 변경으로 물리적으로 정확한
애니메이션을 얻었다.

검증(근일점 구간 점간격 ÷ 원일점 구간 점간격, 이심률에 비례해야 함):
금성 e=0.007 → 1.014 · 지구 0.017 → 1.034 · 화성 0.093 → 1.206 · 수성 0.206 → 1.517 ·
명왕성 0.249 → 1.661.

## 추가된 것

| 항목 | 내용 |
|---|---|
| 재생 컨트롤 | ▶/⏸ · 배속 4단(1일/30일/1년/5년 per 초) · 시점 스크러버 ±50년 · `지금` 리셋 · 날짜 표시 |
| 행성 잔상 | 등시간 경로 인덱스의 뒤쪽 18점(주기의 10%) — 추가 계산 0 |
| 탈출 항적 5기 | 보이저 1/2, 파이어니어 10/11, 뉴호라이즌스. 거리·속도는 실측, 시간의 1차 함수로 외삽. 운용 중은 채운 원, 통신 종료는 빈 원 |
| 순항 미션 4기 | 베피콜롬보(→수성), 루시·유로파 클리퍼·주스(→목성). 양 끝점은 계산한 실제 위치, 마커는 경과율 보간 |
| L2 상주 | JWST — 지구 위치에서 파생되는 실제 기하 |
| 태양 글로우 | `radialGradient` (원점 강조) |
| 딥링크 | `?t=<일수>` 로 시점 공유 (`?theme=` 과 같은 성격, 저장하지 않음) |

## 구조 리팩터링

`drawSolar()`(매번 전체 재생성) → **`buildSolar()`(정적 기하 1회) + `updateSolar()`(좌표 속성만)**.
매 프레임 재생성은 천체 9개 × 180점 + 우주선 10기 = 프레임당 2000개 넘는 노드 생성이라 60fps 가
불가능하다. 재생 중에는 60초 데이터 폴링과 `S.solar` 교체를 건너뛴다(SVG 재구축 시 깜빡임 +
구 위상과 신 epoch 가 섞이는 문제).

## 렌더링 후 잡은 문제 5개

1. **√ 스케일로는 보이저 1(168 AU)이 화면을 벗어난다.** 하나의 연속 스케일로 0~180 AU 를 덮으면
   명왕성이 반경 60% 로 내려오고 내행성이 다시 뭉친다. **구간 스케일**(40 AU 까지 세제곱근,
   그 밖은 로그)을 쓰고 정직성 장치 3개를 붙였다: 경계 실선 링 + 라벨, 경계 밖 배경 톤,
   우주선마다 실제 AU 표기.
2. **순항 미션 이름 라벨 4개가 내행성 라벨과 정면 충돌했다.** 지도에는 번호 배지(1~4)만 찍고
   이름은 좌측 요약 목록의 같은 번호가 나르게 했다.
3. **경계 링을 점선으로 그렸더니 명왕성의 점선 궤도와 헷갈렸다.** 실선으로 바꿨다.
4. **쌍 기반 라벨 스태거가 3기 군집에서 무너졌다** — 뉴호라이즌스와 파이어니어 11 이 둘 다
   같은 오프셋으로 배정돼 서로 겹쳤다. 좌/우 반원별로 y 정렬 후 최소 간격 13px 를 강제하는
   **탐욕적 분리 패스**로 일반화했다. (황경 290·291·293° 로 뭉치는 이유는 황도면 투영이
   실제 황위 -59°·+17°·-2° 의 차이를 접기 때문이다.)
5. **좌측 도달 요약이 지도와 어긋났다** — 지도는 241 AU·100% 인데 목록은 170 AU·34% 로 굳어
   있었다. 정적/동적 분리 리팩터링에서 이 목록을 정적 쪽에 뒀기 때문. 한 화면에서 같은 값이
   두 개로 갈리는 건 그 자체로 버그이므로 우주선 두 줄을 프레임마다 갱신하게 했다.

## 추가된 한계 (화면에 명시)

- 탈출 항적의 **방향(황경)은 근사**이며 **황위는 투영에서 버렸다**(보이저 2 는 실제 황위 -59°)
- 순항 미션의 선과 마커는 **경과율 표시이며 실제 궤적이 아니다** — 실제 항로는 중력도움 나선
- 목표가 행성이 아닌 미션(프시케, 오시리스-아펙스, 하야부사2 확장)은 도착점을 정직하게 계산할
  수 없어 **도표에서 생략**하고 각주로만 언급
- 반경은 **구간 스케일**이다(40 AU 에서 축을 끊는다)
- 스케일 상한 180 AU 를 넘는 우주선은 경계에 고정되고 라벨에 `⇢` 를 붙인다

---

# 개선 패스 (2026-09-02)

열린 요청("더 개선할 부분이 있다면")이었으므로 추측으로 손대지 않고 **감사 후 실측**으로
결함을 확정하고 고쳤다.

## P1 — 실제 버그 2건

### 1. 애니메이션 루프의 이벤트 리스너 누수 (측정: 프레임당 27개)

`bindTip()` 은 호출마다 `addEventListener` 를 3개 붙이는데, 툴팁 내용에 실시간 수치가
들어가므로 `updateSolar()` 가 **프레임마다** 호출하고 있었다.

같은 오리진 프로브(`frontend/_probe.html`, 검증 후 삭제)로 iframe 내부의
`EventTarget.prototype.addEventListener` 를 계수해 측정:

| | addEventListener / 100프레임 | 프레임당 |
|---|---|---|
| 수정 전 | **2,700** | 27.0 |
| 수정 후 | **0** | 0.0 |

60fps 재생 1분이면 약 97,000개(마커 9기 × 3개 × 3,600프레임)가 쌓인다.
수정: 내용은 요소에 얹고(`el.__tip`) 리스너는 요소당 1회만 등록한다. 호출부는 그대로다.

### 2. 60초 폴링이 조용히 실패했다

`setInterval(() => load().catch(() => {}), 60000)` — "실시간 모니터"가 굳은 데이터를
아무 표시 없이 보여주는 것이 이 앱에서 가장 나쁜 실패 양상이다.

수정: `/healthz` 를 `Promise.all` 에 넣고 헤더에 수집 상태 알약을 추가했다. 세 가지를
구분한다 — 폴링 자체 실패 / 개별 소스 실패 / 성공했지만 데이터가 오래됨(서버는 200 을 내면서도
20분 폴러가 죽어 있을 수 있다). 상태색은 예약 팔레트이고 항상 기호 + 글자를 함께 쓴다.

프로브로 실패·복구 경로까지 검증:

```
PILL_OK        class="pill ok"  text="● 수집 1분 전"
PILL_FAIL      class="pill bad" text="✕ 갱신 실패 2회 — 표시는 마지막 성공 시점"
PILL_RECOVERED class="pill ok"  text="● 수집 1분 전"
```

## P1 — 개선 중에 새로 만들고 잡은 버그 2건

### 3. 스로틀 센티널이 `performance.now()` 원점과 결합됐다

요약 목록 갱신을 8Hz 로 제한할 때 `SUMMARY_LAST = 0` 으로 초기화했는데,
`performance.now()` 는 페이지 로드 후 경과 ms 다. 초기 렌더는 로드 후 60~100ms 에
일어나므로 `60 - 0 = 60` 이 되어 `> 125` 가 거짓이고, "강제 갱신"으로 0 을 다시 넣어도
여전히 거짓 → **요약이 첫 폴링(60초)까지 빈 채로 남았다.**
`-Infinity` 가 올바른 센티널이다(시계 원점과 무관하게 첫 호출이 통과).
**코드를 믿지 않고 렌더를 본 덕에 잡혔다.**

### 4. 도착한 미션들이 한 점에 완전히 겹쳤다

"도착 후에는 마커를 목표 천체의 현재 위치에 붙인다"는 수정(아래 5번)의 부작용으로,
목성행 3기(루시·유로파 클리퍼·주스)가 도착 후 모두 같은 좌표로 스냅됐다.
목표 천체의 궤도 반경을 유지한 채 태양 기준으로 8° 씩 회전시켜 부채꼴로 벌리고,
툴팁에 그 사실을 명시했다.

## P2 — 오독 위험과 정확성 3건

### 5. 도착한 순항 마커가 허공에 떠 있었다

진행률 100% 는 "목표 천체에 도착했다"는 뜻인데 마커는 *도착 예정일의* 목표 위치에
고정돼 있었다. 그 사이 행성은 이동했으므로 마커가 아무것도 없는 지점에 남아 오류처럼
읽혔다. 양 끝(발사 전 / 도착 후)에서는 해당 천체의 **현재** 위치에 붙인다.

### 6. "점유율" 패널의 막대가 최다 발사사 기준으로 정규화돼 있었다

SpaceX 막대가 항상 꽉 차므로 100% 점유로 오독될 수 있었다. 각 행에 **전체 대비 %** 를
병기하고(`40회 49%`) 부제에 "막대 길이는 최다 발사사 대비, 오른쪽 %는 전체 대비"를 명시했다.

### 7. SVG 라벨이 궤도선 위에서 읽히지 않았다

`paint-order: stroke` + 표면색 3px 획으로 후광을 둘렀다. 획이 글자 뒤로 가므로 글자
모양을 깎지 않는다.

## P3 — 사용성·접근성 3건

- **재생 컨트롤을 패널 안에서 sticky** 로 고정했다(태양계 도표가 700px 넘게 높아 스크롤하면
  컨트롤이 사라졌다)
- **요약 목록 갱신을 8Hz 로 제한** 했다(프레임마다 `innerHTML` 2회 = 문자열 조립 + HTML 파싱)
- **표에 스크린리더용 캡션** 추가, `.sr-only` 유틸 도입
- `orbits.py` 모듈 독스트링이 "두 종류의 계산"이라 했지만 우주선이 추가돼 세 종류가 됐다 — 갱신

## 배포 후 검증

```
✓ 상태 알약: pill ok "● 수집 2분 전"      ✓ 막대 % 병기: 49
✓ 요약 탈출 중 / 순항 중 채워짐            ✓ 표 캡션
✓ 라벨 후광 CSS (paint-order)             ✓ sticky 컨트롤
```
