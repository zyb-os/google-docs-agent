FROM python:3.11-slim
# Node.js is required for the Google Drive MCP server (npx @modelcontextprotocol/server-gdrive)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV ORCHESTRATOR_URL=http://agent-orchestrator:8000
CMD ["python", "main.py"]
