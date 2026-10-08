FROM python:3.12-slim-bookworm

# git + node (node only for the v1 agent tasks via the Claude Agent SDK)
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Claude config/home live on the persistent volume
ENV HOME=/data/home
ENV CLAUDE_CONFIG_DIR=/data/home/.claude
ENV DATA_DIR=/data
ENV TASKS_DIR=/tasks
# The container runs as root; tell Claude Code it is a sandbox so the agent
# tasks may use bypassPermissions (otherwise the CLI exits with code 1).
ENV IS_SANDBOX=1

WORKDIR /app
COPY app/ /app/

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
