# ai-fiqh-web — the Streamlit app (docs/deployment.md §4).
#
# Build from the repo root so the COPY paths below resolve:
#   docker build -f docker/web.Dockerfile -t ai-fiqh-web .
#
# Ships src/ and the pre-built index/ only. data/ (the 15 MB source PDF) is
# ingest-time only and deliberately excluded via .dockerignore — see
# docs/deployment.md §1.

FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependencies first so this layer is cached independently of source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --group cloud --group ui

COPY src/ ./src/
COPY index/ ./index/

ENV PATH="/app/.venv/bin:${PATH}"
# Unbuffered, or log lines sit in the buffer instead of reaching the platform
# log stream.
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["streamlit", "run", "src/ai_fiqh/app.py", \
     "--server.port=8000", "--server.address=0.0.0.0", \
     "--server.headless=true"]
