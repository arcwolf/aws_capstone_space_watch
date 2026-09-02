# Space Watch — API와 정적 SPA를 한 프로세스에서 :8000 하나로 서빙한다.
#
# 빌드 호스트가 aarch64이므로 이 이미지는 arm64로 만들어지고, Fargate 태스크도
# cpuArchitecture: ARM64 로 맞춘다(CDK 스택 참조). 아키텍처가 어긋나면 태스크가
# "exec format error" 로 즉사한다.
#
# 이미지에는 비밀이 없다. AWS_BEARER_TOKEN_BEDROCK 은 런타임에 Secrets Manager 가
# 환경변수로 주입한다.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SW_FRONTEND_DIR=/app/frontend \
    SW_CACHE_DIR=/tmp/space-watch-cache

WORKDIR /app

# 의존성을 먼저 설치해 레이어 캐시를 살린다 — 앱 코드가 바뀌어도 재설치하지 않는다.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend ./frontend

# 루트로 돌리지 않는다.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /tmp/space-watch-cache \
 && chown -R appuser:appuser /app /tmp/space-watch-cache
USER appuser

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
