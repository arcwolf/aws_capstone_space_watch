"""브리핑 — Bedrock Converse REST를 SDK 없이 httpx로 직접 호출한다.

키의 거처는 환경변수 하나뿐이다. 파일에도, 이미지에도, IaC에도 값을 두지 않는다.
컨테이너에서는 Secrets Manager가 기동 시 AWS_BEARER_TOKEN_BEDROCK 으로 주입한다.

Bearer 인증은 SigV4 IAM 경로를 타지 않는다. 따라서 태스크 롤에 bedrock:* 권한을
붙일 필요가 없고, 실제로 붙이지 않는다(실증: 다른 계정 소속 API 키로도 200이 온다).
"""

from __future__ import annotations

import time

import httpx
from fastapi import HTTPException

from . import config, store

# 같은 10분 버킷 동안 결과를 재사용한다. 최신 버킷만 보관해 무한 증가를 막는다.
_cache: dict[int, dict] = {}


def _bucket() -> int:
    return int(time.time() // config.BRIEF_CACHE_BUCKET_SECONDS)


# ---------------------------------------------------------------------------
# 프롬프트
# ---------------------------------------------------------------------------
def build_prompt(days: int = 90) -> str:
    result = store.query_launches(days=days)
    s = result["summary"]

    top = "\n".join(
        f"  - {p['name']} ({'상업' if p['type'] == 'Commercial' else '정부' if p['type'] == 'Government' else p['type']}"
        f", {p['country']}): {p['count']}회, 성공 {p['success']}회"
        for p in s["top_providers"][:5]
    ) or "  - (없음)"

    notable = "\n".join(
        f"  - {time.strftime('%Y-%m-%d', time.gmtime(x['net'] / 1000))} {x['name']}"
        f" / {x['rocket']} / {x['provider']} / 궤도 {x['orbit']} / {x['status']}"
        for x in result["past"][:8]
    ) or "  - (없음)"

    upcoming = "\n".join(
        f"  - {time.strftime('%Y-%m-%d', time.gmtime(x['net'] / 1000))} {x['name']}"
        f" / {x['rocket']} / {x['provider']}"
        for x in result["upcoming"][:5]
    ) or "  - (없음)"

    stations = []
    for cluster in store.orbital_clusters().values():
        docked = ", ".join(cluster["docked"]) or "없음"
        stations.append(
            f"  - {cluster['name_ko']}({cluster['short']}): 구성 모듈 {len(cluster['modules'])}개, 도킹 중 {docked}"
        )
    station_text = "\n".join(stations) or "  - (없음)"

    reach = "\n".join(
        f"  - {key}: 등급 {info['tier']} {info['label']} — "
        + "; ".join(f"{y} {t}" for y, t in info["milestones"])
        for key, info in config.REACH.items()
    )

    return f"""당신은 우주개발 시황 애널리스트다. 아래 데이터만 근거로 한국어 브리핑을 작성하라.

[최근 {s['window_days']}일 발사 통계]
- 총 발사: {s['count']}회 (성공 {s['success']}, 실패 {s['failure']}, 성공률 {s['success_rate']}%)
- 발사 주체: {s['provider_count']}개
- 상업 {s['commercial_count']}회 / 정부 {s['government_count']}회 (상업 비중 {s['commercial_share']}%)
- 최다 발사사: {s['leader']} (점유율 {s['leader_share']}%)

[발사사 상위 5]
{top}

[최근 발사 8건]
{notable}

[예정 발사 5건]
{upcoming}

[활성 우주정거장]
{station_text}

[태양계 인류 도달 현황 (큐레이션)]
{reach}

작성 규칙:
1. 반드시 "지난 {s['window_days']}일, 인류는" 으로 시작한다.
2. 세 부분으로 쓴다.
   (1) 전체 흐름 한 문단 — 발사 빈도, 성공률, 판도의 요지.
   (2) 주목할 발사나 미션 2~3개 해설 — 왜 의미가 있는지 한두 문장씩.
   (3) 상업 우주 세력 판도 — 누가 얼마나 점유했고 신흥 주체는 누구인지.
3. 각 부분에 소제목을 붙이지 말고 문단으로 구분한다.
4. 위 데이터에 없는 수치나 사건을 만들어내지 않는다. 모르면 언급하지 않는다.
5. 과장 없이, 담백한 시황 브리핑 어조로 쓴다.
6. 길이 제약을 반드시 지킨다. 출력 토큰 상한이 {config.BEDROCK_MAX_TOKENS}이고
   한국어는 대략 글자 하나가 토큰 하나이므로, 넘기면 문장이 통째로 잘린다.
   - 전체 550자 이내
   - 문단별로 첫 문단 170자, 둘째 문단 220자, 셋째 문단 160자 이내
   - 각 문단 2~3문장
   - 마지막 문단까지 반드시 완결된 문장으로 끝낼 것. 분량이 모자라면 내용을 줄여라."""


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------
async def generate_brief(client: httpx.AsyncClient, days: int = 90) -> dict:
    bucket = _bucket()
    if bucket in _cache:
        return {**_cache[bucket], "cached": True}

    token = config.bedrock_bearer_token()
    if not token:
        raise HTTPException(
            503,
            "AWS_BEARER_TOKEN_BEDROCK 환경변수가 설정되지 않았습니다. "
            "로컬에서는 셸에 export 하고, 컨테이너에서는 Secrets Manager가 주입합니다.",
        )

    payload = {
        "messages": [{"role": "user", "content": [{"text": build_prompt(days)}]}],
        "inferenceConfig": {"maxTokens": config.BEDROCK_MAX_TOKENS},
    }

    try:
        response = await client.post(
            config.BEDROCK_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=config.BEDROCK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bedrock 연결 실패: {type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        # 조용히 빈 문자열을 반환하지 않는다 — 무엇이 왜 실패했는지 노출한다.
        raise HTTPException(
            502,
            f"Bedrock {response.status_code}: {response.text[:300]}",
        )

    try:
        body = response.json()
        text = body["output"]["message"]["content"][0]["text"].strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(502, f"Bedrock 응답 파싱 실패: {type(exc).__name__}: {exc}") from exc

    usage = body.get("usage", {})
    entry = {
        "brief": text,
        "bucket": bucket,
        "generated_at": time.time(),
        "window_days": days,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "latency_ms": (body.get("metrics") or {}).get("latencyMs"),
    }

    _cache.clear()          # 최신 버킷만 보관한다
    _cache[bucket] = entry
    return {**entry, "cached": False}
