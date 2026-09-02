# Space Watch — 전 세계 우주개발 시황

태양계를 주된 UI로 삼아 세 가지를 한 화면에서 보여주는 실시간 모니터.

1. **인류가 각 천체까지 어디까지 갔는가** — 도달등급 0~4 + 대표 마일스톤
2. **지금 무엇이 어디를 날고 있는가** — 우주정거장 2기, 탈출 항적 5기, 순항 미션 4기, L2 관측소 1기
3. **상업 우주기업이 발사 시장을 얼마나 점유했는가** — 최근 110일 실측 집계

FastAPI 하나가 API와 정적 SPA를 함께 서빙해 `:8000` 단일 포트로 완결하고, Dockerfile 한 장으로
컨테이너화하며, CDK로 CloudFront → ALB → ECS Fargate에 배포한다.

## 데이터 소스 (전부 인증 불필요)

| 소스 | 얻는 것 |
|---|---|
| [The Space Devs Launch Library 2](https://ll.thespacedevs.com/2.2.0/) | 발사 기록·예정, 발사사와 종류(상업/정부), 로켓, 미션 궤도, 발사장 위경도, 성공/실패, 우주정거장 목록과 상태 |
| [CelesTrak GP](https://celestrak.org/NORAD/elements/) | 우주정거장 그룹 22개 객체의 궤도요소 |
| Amazon Bedrock Converse | 한국어 시황 브리핑 (`AWS_BEARER_TOKEN_BEDROCK`) |
| — | 행성 위치는 **외부 의존성 없이** J2000 평균 궤도요소로 계산 |

**LL2 익명 한도는 15 req/hour.** 사이클당 4요청이므로 폴링 주기는 20분(12 req/hr)이고, 응답을
`$SW_CACHE_DIR`에 디스크 캐시해 서버 재시작이나 태스크 재기동이 쿼터를 태우지 않는다.

## 구조

```
backend/app/
  config.py      엔드포인트·주기·상한 + 큐레이션(도달등급, 행성 궤도요소, 우주선)
  orbits.py      순수 계산만 (I/O 없음): 행성 위치, 정거장 궤도 상태, 우주선
  store.py       인메모리 스냅샷 (id 멱등 upsert, 최대 300건)
  collector.py   LL2 3종 + CelesTrak 1종 수집·정규화·폴링·디스크캐시
  llm.py         Bedrock Converse REST (httpx 직접) + 10분 버킷 캐시
  api.py         라우트
  main.py        앱 + lifespan + StaticFiles (라우터 include 후에 마운트할 것)
frontend/index.html   빌드 없는 단일 정적 SPA (외부 라이브러리 0)
infra/                CDK TypeScript 단일 스택
```

`orbits.py`는 httpx도 store도 모르는 순수 함수 모듈이다. "화성 위치가 틀렸다"는 문제를
서버·상태·프레임워크 없이 파이썬 한 줄로 검증할 수 있다.

## 로컬 실행

```bash
uv venv --python 3.11 .venv
VIRTUAL_ENV=$PWD/.venv uv pip install -r backend/requirements.txt   # uv venv 에는 pip 가 없다
export AWS_BEARER_TOKEN_BEDROCK=...        # 브리핑용. 없으면 /api/brief 가 503
cd backend && ../.venv/bin/python -m uvicorn app.main:app --port 8000
```

```bash
curl -s localhost:8000/healthz | jq
curl -s 'localhost:8000/api/launches?days=90' | jq .summary
curl -s localhost:8000/api/solar-system | jq '.spacecraft.escaping[] | {name_ko, distance_au}'
curl -s localhost:8000/api/leo | jq '.stations[] | {short, altitude_km, docked}'
curl -sX POST localhost:8000/api/brief | jq -r .brief
```

### URL 파라미터 (공유·디버깅용, 저장하지 않음)

- `?theme=light|dark` — 테마 일회성 오버라이드
- `?t=<일수>` — 태양계 시점 딥링크 (예: `?t=7300` → 약 20년 후)

## API

| 엔드포인트 | 내용 |
|---|---|
| `GET /healthz` | **수집 실패 중에도 항상 200.** ALB 헬스체크 경로이므로 여기서 503을 내면 태스크가 영구 unhealthy가 된다. 수집 상태는 본문의 `last_error` / `source_status`로 노출 |
| `GET /api/solar-system?at=<iso>` | 행성 위치·궤도 경로·도달등급 + 우주선 3종. `at`으로 임의 시점 계산 |
| `GET /api/launches?days=&provider=&commercial_only=` | 발사 목록 + 요약 통계 |
| `GET /api/leo` | 활성 정거장의 실시간 궤도 상태 + 역대 정거장 |
| `POST /api/brief` | 한국어 시황 브리핑 (10분 버킷 캐시) |

## 배포

```
Viewer ─HTTPS→ CloudFront ─HTTP + X-Origin-Verify→ ALB ─→ Fargate(:8000, ARM64)
```

```bash
cd infra && npm install

# X-Origin-Verify 값은 버전관리에 없다. 최초 1회 생성한다.
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > .origin-verify

npm run deploy                                    # 값을 주입하고 arm64 이미지를 빌드해 배포
aws secretsmanager put-secret-value \
  --secret-id space-watch/bedrock-bearer --secret-string "$AWS_BEARER_TOKEN_BEDROCK"
aws ecs update-service --cluster <출력> --service <출력> --force-new-deployment
```

`.origin-verify` 없이 `cdk deploy` 를 직접 부르면 **명확한 에러로 실패한다**(조용히 빈 값이
들어가 헤더 검사가 무력화되는 것보다 낫다). 값을 바꾸려면 파일을 새로 쓰고 재배포하면
ALB 리스너 규칙과 CloudFront 커스텀 헤더가 **함께** 갱신된다.

- VPC는 **퍼블릭 서브넷만, NAT Gateway 0개**. Fargate는 `assignPublicIp`로 아웃바운드를 얻고
  인바운드는 SG가 차단한다.
- ALB SG 인바운드는 CloudFront 관리형 prefix list `com.amazonaws.global.cloudfront.origin-facing`
  에서만. `addListener({ open: false })`가 **필수**다 — 기본값 `true`면 CDK가 `0.0.0.0/0`
  규칙을 자동 추가하고, 보안 그룹 규칙은 합집합이라 넓은 쪽이 이겨 prefix list 제한이 무력화된다.
- 리스너 기본 액션은 **403 고정 응답**이고 `X-Origin-Verify` 헤더가 맞는 요청만 포워드한다.
- `desiredCount: 1` 고정 — 스토어가 프로세스 메모리이므로 태스크가 2개면 두 화면이 서로 다른
  스냅샷을 본다.
- **태스크 롤에 `bedrock:*` 권한 없음.** Bearer 인증은 SigV4 IAM 경로를 타지 않는다.
- 이미지에 비밀이 없다. Secrets Manager가 기동 시 환경변수로 주입한다.
- SG `GroupDescription`은 ASCII만 허용된다(한국어를 넣으면 배포가 실패한다).

정리: `cd infra && npx cdk destroy`. 상시 비용은 ALB(~$18/월) + Fargate 256/512(~$8/월) +
CloudFront이며 NAT Gateway는 없다.

### 버전관리에 두지 않는 값

CloudFront 커스텀 헤더는 synth 시점에 **리터럴** 값을 요구해서 Secrets Manager 참조가
해석되지 않는다. 따라서 `X-Origin-Verify` 값만은 IaC 입력으로 들어올 수밖에 없다.

이 값이 ALB DNS 이름과 함께 공개되면 **2중 방어 중 헤더 층이 무력화된다** — 누구나 자기
CloudFront 배포를 이 ALB에 오리진으로 붙이고 그 헤더를 실어 통과할 수 있다. prefix list는
"CloudFront 엣지에서만"을 강제하지만 그건 *모든 CloudFront 고객*을 포함한다.

그래서 값은 `infra/.origin-verify`(gitignore)에 두고 `npm run deploy` 가 `SW_ORIGIN_VERIFY`
환경변수로 주입한다. 같은 이유로 `infra/cdk.context.json`도 추적하지 않는다(AZ 조회 키에
계정 ID가 박힌다 — 자격증명만 있으면 재생성된다). 문서의 배포 산출물(ALB DNS, CloudFront
도메인, 클러스터·서비스 이름, 계정 ID)은 자리표시자로 치환했다.

## 정직하게 밝히는 한계 (모두 화면에도 표시된다)

1. **위성 위치는 SGP4가 아닌 간이 케플러** — 대기항력·J2 섭동을 무시하므로 몇 시간 뒤 수백 km
   오차. 표시용이며 추적용이 아니다.
2. **행성 위치는 황도면 투영** — 궤도경사각과 승교점 경도를 쓰지 않는다(최대 7°, 수성).
3. **반경은 구간 스케일** — 40 AU까지 세제곱근, 그 밖은 로그. 축을 끊는다. 경계에 링과 라벨을
   두고 우주선마다 실제 AU를 표기한다.
4. **탈출 항적의 방향(황경)은 근사**이고 **황위는 투영에서 버렸다**(보이저 2는 실제 황위 −59°).
   거리와 속도는 실측값이며 쌍곡선 궤도라 시간의 1차 함수로 외삽한다.
5. **순항 미션의 선과 마커는 진행률 표시이며 실제 궤적이 아니다** — 실제 항로는 중력도움을 쓰는
   나선이다. 양 끝점(발사일 지구 위치, 도착일 목표 위치)만 계산한 실제 값이다.
6. **목표가 행성이 아닌 미션은 도표에서 생략**했다(프시케, 오시리스-아펙스, 하야부사2 확장).
   도착점을 정직하게 계산할 수 없다.
7. **도달등급과 마일스톤은 큐레이션 하드코딩** — 라이브 API가 존재하지 않는 영역이다.
8. **"도킹 중"은 궤도요소 클러스터링 추론** — 공식 도킹 데이터가 아니다. 같은 평균운동·경사각을
   가진 객체를 같은 궤도로 보므로, 방금 분리한 우주선이 몇 시간 오탐될 수 있다.
9. **발사 통계는 최근 110일 100발 표본** — LL2 익명 한도 때문에 전체 7,600여 발을 페이징할 수
   없다. 역대 누적에는 유료 키가 필요하다.
10. **CloudFront → ALB 구간은 평문 HTTP** — ACM 인증서와 커스텀 도메인이 없다.

## 테스트

시간 제약에 따라 자동화 테스트는 **의도적으로 생략**했다. 검증은 `curl` + `jq`와 브라우저
스크린샷으로 했고, 단계별 검증 결과와 계획 대비 차이는
`docs/superpowers/plans/2026-09-02-space-watch-plan.md`에 기록돼 있다. 설계 근거는
`docs/superpowers/specs/2026-09-02-space-watch-design.md`.
