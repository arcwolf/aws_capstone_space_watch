# Space Watch — 설계 문서

작성일: 2026-09-02
상태: 승인됨 (구현 진행 중)

## 1. 목적

전 세계 우주개발 시황을 한 화면에서 보여주는 실시간 모니터. 태양계를 주된 UI로 삼아
(a) 인류가 각 천체까지 어디까지 갔는지, (b) 지구 저궤도에 무엇이 돌고 있는지,
(c) SpaceX 같은 상업 우주기업이 발사 시장을 얼마나 점유했는지를 표현한다.

FastAPI 하나가 API와 정적 SPA를 함께 서빙해 `:8000` 단일 포트로 완결하고,
Dockerfile 한 장으로 컨테이너화하며, CDK로 CloudFront → ALB → ECS Fargate에 배포한다.

## 2. 데이터 소스 (모두 실측 검증 완료, 인증 불필요)

| # | 엔드포인트 | 얻는 것 |
|---|---|---|
| 1 | `GET https://ll.thespacedevs.com/2.2.0/launch/previous/?limit=100` | 최근 약 110일 100발. 발사사(`launch_service_provider.name`, `.type` = Commercial/Government), 로켓명, 미션명·종류·궤도, 발사장 위경도, 성공/실패 |
| 2 | `GET https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=20` | 예정 발사 |
| 3 | `GET https://ll.thespacedevs.com/2.2.0/spacestation/?limit=20` | 정거장 15개 + `status`(Active/De-Orbited) + 소유기관 + 설립일 + 궤도 |
| 4 | `GET https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=json` | 22개 객체의 궤도요소(평균운동, 경사각, 이심률, 평균근점이각, epoch 등) |
| 5 | `POST https://bedrock-runtime.ap-northeast-2.amazonaws.com/model/global.anthropic.claude-sonnet-4-6/converse` | 한국어 브리핑. `Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK` |

실측 확인된 사실:
- LL2 `mode` 기본값 응답은 발사 1건당 약 4KB. `limit=100`이면 약 400KB, 단일 요청으로 충분.
- 최근 110일 100발의 발사사 분포: SpaceX 49, CASC 23, Rocket Lab 6, LandSpace 3, CAS Space 3, Arianespace 2 …
- LL2 `agencies` 목록 응답의 `total_launch_count` 등은 `null`이다. 따라서 회사별 집계는
  **발사 목록을 provider로 집계**해서 구한다. (agencies 상세 조회는 레이트리밋 낭비)
- CelesTrak `stations` 22개는 궤도요소가 3개 클러스터로 뭉친다:
  `mm≈15.49 / inc≈51.6°` 8개(ISS 계열, `CREW DRAGON 12` 포함),
  `mm≈15.60 / inc≈41.5°` 5개(CSS 계열, `TIANZHOU-10` 포함), 그 외 개별 객체.
- Bedrock Bearer 호출은 `HTTP 200`, 지연 약 950ms, 파싱 경로는 `output.message.content[0].text`.
  API 키는 계정 `<bedrock-key-account-id>` 소속이며 인스턴스 STS 신원(`<account-id>`)과 무관하게 동작한다
  → Bearer 인증은 IAM 경로를 타지 않으므로 태스크 롤에 `bedrock:*`를 부여하지 않는다.

### 레이트리밋이 아키텍처를 결정한다

LL2 익명 한도는 **15 req/hour**. 사이클당 4요청이므로 **폴링 주기 20분**(3사이클/시간 = 12 req/hr).
추가로 **디스크 캐시**: 각 응답을 `$SW_CACHE_DIR`(기본 `/tmp/space-watch-cache`)에 저장하고,
기동 시 캐시가 `CACHE_MAX_AGE_SECONDS`(3600초) 미만이면 네트워크를 건드리지 않는다.
개발 중 서버 재시작과 Fargate 태스크 재기동이 쿼터를 태우지 않는다.

### 3층 데이터 구조

| 층 | 내용 | 성격 |
|---|---|---|
| 라이브 피드 | 발사 시황, 회사 점유율, 정거장 상태, 궤도요소 | 20분마다 갱신 |
| 계산 | 8행성 + 명왕성 위치, ISS/CSS 궤도 상태 | 요청마다 실시간 계산 |
| 큐레이션(정적) | 천체별 인류 도달 등급 + 대표 마일스톤 | 역사적 사실이라 불변 |

## 3. 파일 구조

```
capstone-5/
├── backend/
│   ├── requirements.txt        fastapi, uvicorn[standard], httpx
│   └── app/
│       ├── __init__.py
│       ├── config.py           엔드포인트·주기·상한 + 큐레이션(도달등급, 행성 궤도요소)
│       ├── orbits.py           순수 계산 (I/O 없음): 행성 위치, 정거장 궤도 상태
│       ├── store.py            인메모리 스냅샷 (id 멱등 upsert)
│       ├── collector.py        LL2 3종 + CelesTrak 1종 수집·정규화·폴링·디스크캐시
│       ├── llm.py              Bedrock Converse REST + 10분 버킷 캐시
│       ├── api.py              APIRouter
│       └── main.py             FastAPI 앱 + lifespan + StaticFiles(가장 마지막에 마운트)
├── frontend/index.html         빌드 없는 단일 정적 SPA
├── infra/                      CDK TypeScript
│   ├── bin/space-watch.ts
│   ├── lib/space-watch-stack.ts
│   ├── cdk.json / package.json / tsconfig.json
├── Dockerfile                  python:3.11-slim, arm64
└── docs/superpowers/{specs,plans}/
```

`orbits.py`를 별도 모듈로 두는 이유: httpx도 store도 모르는 **순수 함수 모듈**이다.
입력은 숫자, 출력은 숫자. 분리해 두면 "화성 위치가 틀렸다"는 문제를 네트워크·상태·프레임워크
없이 파이썬 한 줄로 검증할 수 있다. `collector.py`는 I/O만, `orbits.py`는 수학만 담당한다.

## 4. `orbits.py` — 계산 두 종류

### (a) 행성 위치 — 케플러 궤도요소

J2000 평균요소 + 세기당 변화율(`a`, `e`, `L`, `ϖ`)을 사용한다.

1. 세기 단위 시간 `T = (JD - 2451545.0) / 36525`
2. 각 요소를 `elem = elem0 + rate * T`로 갱신
3. 평균근점이각 `M = L - ϖ`, `[-180, 180)`로 정규화
4. 케플러 방정식 `E - e·sinE = M`을 뉴턴 반복 3회로 해석
5. 진근점이각 `ν = atan2(√(1-e²)·sinE, cosE)`, 동경 `r = a(1 - e·cosE)`
6. 황도면 투영 `x = r·cos(ν + ϖ)`, `y = r·sin(ν + ϖ)`

**궤도경사각 `I`와 승교점 경도 `Ω`는 의도적으로 무시한다.** 위에서 내려다보는 뷰에서
최대 7°(수성) 기울기는 시각적으로 구분되지 않는다. 상수가 절반으로 줄고 코드가 짧아진다.
화면에 "황도면 투영, 경사각 무시"를 명시한다.

**궤도선은 원이 아니라 60점 폴리라인.** `ν`를 0~360° 샘플링해 실제 타원을 뽑는다.
원으로 근사하면 명왕성의 이심률 0.249가 사라진다. 폴리라인은 5줄 더 쓰고 그걸 살린다.

**반지름 스케일은 √.** `a`가 0.39 AU(수성)~39.5 AU(명왕성)로 100배 차이라 선형 스케일은
내행성을 한 점에 뭉친다. `r_px = R_max·√(a / a_pluto)`면 10배로 압축돼 내행성에 공간이 생기고,
로그처럼 외행성을 과하게 짓누르지도 않는다.

부수 효과: `?at=<iso>` 쿼리로 임의 시점 행성 배치를 얻는다. 계산식이 시간의 함수이므로 추가 코드 0.

### (b) 정거장 궤도 상태 — GP 요소에서 유도

`μ = 398600.4418 km³/s²`, `R_earth = 6371 km`.

1. `n_rad = MEAN_MOTION · 2π / 86400` (rev/day → rad/s)
2. 반장축 `a = (μ / n_rad²)^(1/3)`
3. `M(t) = M₀ + n_rad · (t - epoch)`, 케플러 해석 → `E`, `ν`
4. `r = a(1 - e·cosE)` → **고도 = r - 6371**
5. **속도 `v = √(μ(2/r - 1/a))`** (km/s → km/h)
6. **주기 = 1440 / MEAN_MOTION** 분

**SGP4가 아니다.** 대기항력·J2 섭동을 무시하므로 몇 시간 뒤엔 수백 km 오차가 난다.
화면에 "표시용 근사 — 추적용 아님"을 명시한다. 대신 외부 의존성 0, 약 50줄.

LEO 패널은 위경도 지상궤적이 아니라 **궤도 측면뷰**로 그린다: 지구 원 + 정거장마다
경사각만큼 기울인 타원 + 진근점이각 위치의 점. ISS 51.6° vs 톈궁 41.5°라 두 링이 실제로
구분되고, GMST·좌표변환 코드가 전부 불필요해진다.

## 5. `store.py` — 인메모리 스냅샷

```python
_launches: dict[str, dict]   # LL2 id → 정규화 발사 (past+upcoming 한 딕셔너리, kind 필드로 구분)
_stations: dict[str, dict]   # LL2 id → 정규화 정거장
_orbital:  dict[str, dict]   # 클러스터키 → {anchor, elements, modules[], docked[]}
_last_new_ids: list[str]
_last_collected_at: float | None
_last_error: str | None
_cycle_count: int
```

- **`id`가 멱등 키.** 기존 id는 덮어쓰기만 하고 중복 삽입하지 않는다. 신규 id는 사이클마다
  `_last_new_ids`에 기록한다.
- 발사 상한 **300건**. 초과 시 `net`(발사 시각) 오래된 것부터 제거한다. 삽입 순서가 아니라
  발사 시각 기준이어야 "최근 300발"이라는 의미가 맞다.
- **락 없음.** 폴링 태스크와 요청 핸들러가 같은 이벤트 루프의 코루틴이고, upsert/query 내부에
  `await`가 하나도 없어 원자적이다. asyncio는 협력적 스케줄링이므로 await 없는 동기 블록은
  중간에 끼어들 수 없다. 이 보장이 깨지는 조건(내부에 `await` 추가)을 코드 주석으로 남긴다.

### 정규화 규칙 (실패한 항목만 버리고 계속 — 부분 실패 격리)

`id`가 없거나 비면 그 항목만 스킵. 나머지는:

| 필드 | 실패 시 |
|---|---|
| `name`, `provider`, `provider_type`, `rocket`, `mission`, `orbit`, `pad`, `pad_location`, `status` | `"unknown"` |
| `net` (ISO8601 → epoch ms) | `0` |
| `pad_lat`, `pad_lon` | `None` — `0`으로 두면 기니 만 앞바다에 발사장이 생긴다 |
| `provider_type` | `Commercial` / `Government` / `unknown` 셋 중 하나로 정규화 |

### 궤도 클러스터링 (도킹 추론)

`(round(MEAN_MOTION, 2), round(INCLINATION, 1))`로 그룹화한다. 같은 궤도를 돈다는 것은
도킹돼 있다는 뜻이다. 대표(anchor)는 화이트리스트 `ISS (ZARYA)` / `CSS (TIANHE)`, 없으면
그룹 최다. 모듈 키워드
(`ZARYA|NAUKA|POISK|RASSVET|ZVEZDA|HARMONY|COLUMBUS|KIBO|UNITY|TRANQUILITY|DESTINY|QUEST|TIANHE|WENTIAN|MENGTIAN`)
에 매칭되면 모듈, 나머지는 **도킹 중인 우주선**으로 분류한다. 대표만 궤도에 그리고 나머지는
"모듈 n개 · 도킹 중 X" 라벨로 쓴다.

## 6. API 계약

| 엔드포인트 | 응답 |
|---|---|
| `GET /healthz` | `200 {status, launches, stations, orbital_clusters, last_collected_at, last_error, cycles}` |
| `GET /api/solar-system?at=<iso 옵션>` | `{epoch, note, bodies:[{key, name, name_ko, a_au, e, x_au, y_au, r_au, lon_deg, radius_km, reach:{tier,label,milestones[]}, orbit_path:[[x,y] × 60]}]}` |
| `GET /api/launches?days=90&provider=&commercial_only=false` | `{past:[…최신순], upcoming:[…임박순], summary:{count, success, failure, success_rate, commercial_count, government_count, top_providers:[{name,type,country,count,success}], window_days, last_collected_at}}` |
| `GET /api/leo` | `{epoch, note, stations:[{key, name, name_ko, status, owner, founded, inclination_deg, altitude_km, speed_kmh, period_min, true_anomaly_deg, module_count, docked:[]}]}` |
| `POST /api/brief` | `{brief, cached, bucket, generated_at}` |

`/healthz`는 **수집 실패 중에도 항상 200**을 반환한다. ALB 헬스체크가 이 경로를 보므로
여기서 503을 내면 태스크가 영구 unhealthy가 되어 배포가 실패한다.

검증: `days`는 `1~110`(피드가 110일치), `provider`는 부분 문자열 매칭(대소문자 무시).
FastAPI `Query(ge=, le=)`로 범위 밖은 자동 422.

**엔드포인트를 하나로 합치지 않는 이유:** 세 개는 데이터 성격과 갱신 주기가 다르다
(태양계 = 순수 계산, 발사 = 필터 있음, LEO = 근사 경고 동반). SPA는 `Promise.all`로 병렬
호출 3줄이면 되고, 대신 계약이 깔끔해져 "발사 필터를 바꿨는데 태양계를 다시 계산"하는 낭비가
사라진다.

## 7. 브리핑 (`llm.py`)

- `POST .../global.anthropic.claude-sonnet-4-6/converse`
- 헤더 `Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK`, `Content-Type: application/json`
- 바디 `{"messages":[{"role":"user","content":[{"text": prompt}]}], "inferenceConfig":{"maxTokens":700}}`
- 입력: 90일 요약(총 발사·성공률·상업/정부 비율·톱5 발사사와 건수) + 예정 발사 5건 +
  활성 정거장과 도킹 현황 + 태양계 도달 요약
- 출력: 한국어, `"지난 90일, 인류는"`으로 시작. ① 전체 흐름 한 문단 ② 주목 발사·미션 2~3개
  해설 ③ 상업 우주 세력 판도
- 캐시: `bucket = int(time.time() // 600)`. 딕셔너리에 **최신 버킷만** 보관해 무한 증가를 막는다.
- 파싱: `output.message.content[0].text`
- 실패: 토큰 없음 → **503** + 명시적 메시지. Bedrock 4xx/5xx → **502** + 상태코드와 본문 앞
  300자. 조용히 빈 문자열을 반환하지 않는다.

## 8. 화면 (`frontend/index.html`)

승인된 레이아웃: 위→아래 스토리 스택 (우주 → 지구권 → 기업 → 개별 발사 → 브리핑).

- **테마**: 다크 기본 + 라이트 토글, `localStorage['sw-theme']`.
  CSS 변수 `--bg / --fg / --panel / --border / --accent / --dim`.
- **통계 바**: 90일 발사수 · 성공률 · SpaceX 점유율 · 활성 정거장 수 · 마지막 수집(KST) · 테마 토글.
- **태양계 SVG** `viewBox="0 0 1000 560"`, 태양 중심 `(500, 280)`:
  60점 폴리라인 궤도 + 행성 원(반지름 = 실제 크기 로그 스케일, `clamp(3, 14)`) +
  도달등급 배지(0 회색 → 4 청록, 숫자 표시) + 호버 시 마일스톤 툴팁.
  명왕성은 점선 궤도(왜행성).
- **LEO 패널** 300×300: 지구 원 + 경사각만큼 기운 타원 궤도 + 위치 점 +
  고도/속도/주기/도킹 현황 리드아웃 + "표시용 근사" 각주.
- **발사사 점유율**: HTML div 바 차트(SVG 불필요).
  **상업 = 강조색, 정부 = 중성색** — "상업 세력이 얼마나 진출했나"가 색으로 즉시 읽힌다.
- **테이블**: KST 변환, 과거는 성공/실패 배지, 예정은 D-day. 로켓·발사사·궤도·발사장.
- **필터**: 기간 슬라이더(30/60/90/110일) + "상업만" 토글 → 서버 집계가 바뀌므로 API 재호출.
- **폴링**: 60초마다 3개 GET 병렬. 서버 데이터는 20분마다 갱신되지만 행성·위성 위치는
  요청마다 재계산되므로 점이 실제로 움직인다(LEO 주기 92분 → 60초당 3.9°).

## 9. 배포 (`infra/`, CDK TypeScript)

```
Viewer ─HTTPS→ CloudFront ─HTTP + X-Origin-Verify→ ALB ─→ Fargate(:8000)
```

- **VPC**: 2 AZ, 퍼블릭 서브넷만, `natGateways: 0`. Fargate는 `assignPublicIp: true`로
  아웃바운드(LL2·CelesTrak·Bedrock)를 확보하되 인바운드는 SG로 차단한다.
- **ALB SG 인바운드**: CloudFront 관리형 prefix list
  `com.amazonaws.global.cloudfront.origin-facing`에서 80 포트만.
- **리스너**: 기본 액션 = **403 고정 응답**. 우선순위 10에 `X-Origin-Verify` 헤더 일치 규칙만
  타깃 그룹으로 포워드. 프리픽스 리스트(누가) + 헤더(무엇을 아는가) 2중 방어.
- **Fargate SG 인바운드**: ALB SG에서 8000 포트만.
- **태스크**: `cpuArchitecture: ARM64`(빌드 호스트가 aarch64), 256 CPU / 512 MB,
  **`desiredCount: 1`** — 스토어가 프로세스 메모리이므로 태스크가 2개면 두 화면이 서로 다른
  데이터를 본다.
- **비밀**: Secrets Manager `space-watch/bedrock-bearer`를 CDK가 생성하고 사용자가 값을
  넣는다. 태스크 정의는 `ecs.Secret.fromSecretsManager(...)`로 `AWS_BEARER_TOKEN_BEDROCK`
  환경변수에 기동 시 주입한다. **이미지에도 CDK 코드에도 값이 없다.**
- **태스크 롤에 `bedrock:*` 권한 없음** — Bearer 인증은 SigV4 IAM 경로를 타지 않는다.
  실행 롤만 시크릿 읽기 권한을 받는다(`ecs.Secret`이 자동 부여).
- **CloudFront**: 오리진 = ALB(HTTP only) + 커스텀 헤더. 캐시 정책 `CACHING_DISABLED`,
  오리진 요청 정책 `ALL_VIEWER_EXCEPT_HOST_HEADER`, `allowedMethods: ALLOW_ALL`
  (POST `/api/brief` 때문), viewer는 HTTPS 리다이렉트.
- 헬스체크: `/healthz`, 인터벌 30초, 임계 2회.

### 알려진 트레이드오프

1. **CloudFront → ALB 구간이 평문 HTTP.** ACM 인증서와 커스텀 도메인이 없으므로 이 패턴에서는
   표준적인 절충이다.
2. **`X-Origin-Verify` 값은 `infra/cdk.json` context에 평문으로 들어간다.** CloudFront 커스텀
   헤더는 synth 시점에 리터럴 값이 필요해서 Secrets Manager 참조가 해석되지 않는다. 이미지에는
   없지만 IaC에는 남는다. Bearer 토큰과 달리 이것은 AWS 자격증명이 아니라 "ALB 직격을 막는
   자물쇠"다.

## 10. 정직하게 밝히는 한계 (화면에도 표시)

1. **위성 위치는 SGP4가 아닌 간이 케플러** — 몇 시간 뒤 수백 km 오차. 표시용, 추적용 아님.
2. **행성 위치는 황도면 투영** — 궤도경사각 무시(최대 7°).
3. **도달등급·마일스톤은 큐레이션 하드코딩** — 라이브 API가 존재하지 않는 영역.
4. **회사 점유율은 "최근 110일 100발" 기준** — LL2 익명 한도 때문에 전체 7,604발을 페이징할 수
   없다. 역대 누적을 원하면 LL2 유료 키가 필요하다.
5. **"도킹 중"은 궤도요소 클러스터링 추론** — 공식 도킹 데이터가 아니다. 방금 분리한 우주선이
   몇 시간 오탐될 수 있다.

## 11. 테스트 전략

시간 제약(약 2시간 40분)에 따라 **자동화 테스트는 생략**한다. 검증은 각 Phase 말미의
`curl` + `jq`와 브라우저(Playwright 스크린샷)로 수행한다. 구체적인 검증 명령은 구현 계획
문서에 단계별로 명시한다.
