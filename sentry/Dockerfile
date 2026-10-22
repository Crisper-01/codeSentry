FROM python:3.14-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENTRY_STATE_FILE=/state/repos.json

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY *.py ./
COPY prompts/ ./prompts/

RUN pip install --no-cache-dir . && mkdir -p /state

EXPOSE 8080

# Interactive shell + background webhook. Requires -t for the prompt.
# Tunnel tip: smee/cloudflared -> http://localhost:8080/webhook/github
CMD ["python", "main.py"]
