FROM python:3.12-slim-bookworm

# git + node (Agent SDK requires Node.js; arm64-native for the MacBook Air)
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN pip install --no-cache-dir \
      claude-agent-sdk \
      fastapi \
      "uvicorn[standard]" \
      apscheduler \
      jinja2 \
      pyyaml

# Claude config/home live on the persistent volume
ENV HOME=/data/home
ENV CLAUDE_CONFIG_DIR=/data/home/.claude

WORKDIR /app
COPY app/ /app/

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
