FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1001 -s /bin/bash sentinelx

WORKDIR /opt/sentinelx-core-mcp

COPY sentinelx-core-mcp/ .

RUN python3 -m venv .venv \
    && .venv/bin/pip install --upgrade pip --quiet \
    && .venv/bin/pip install -r requirements.txt --quiet

RUN mkdir -p /var/log/sentinelx-mcp \
    && chown -R sentinelx:sentinelx /opt/sentinelx-core-mcp /var/log/sentinelx-mcp

USER sentinelx

ENV MCP_PORT=8099
ENV LOG_DIR=/var/log/sentinelx-mcp

EXPOSE 8099

CMD [".venv/bin/python", "app.py"]
