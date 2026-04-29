FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    util-linux \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/sentinelx

# Código fuente del core
COPY sentinelx-core/ .

# Reemplazar agent.py por la variante Docker (nsenter / container)
COPY core/agent_docker.py agent.py

RUN python3 -m venv venv \
    && venv/bin/pip install --upgrade pip --quiet \
    && venv/bin/pip install -r requirements.txt --quiet

RUN mkdir -p /var/lib/sentinelx/uploads /var/log/sentinelx

# Nota: el agente corre como root.
# En modo host (SENTINEL_EXEC_MODE=host) nsenter necesita root para
# reasociarse a los namespaces del PID 1 del host.
# En modo container podés cambiar esto a USER sentinelx si lo preferís.

ENV AGENT_PORT=8091
ENV LOG_DIR=/var/log/sentinelx
ENV LOG_FILE=/var/log/sentinelx/sentinelx.log
ENV LOG_EXEC_FILE=/var/log/sentinelx/exec.log
ENV SENTINEL_UPLOAD_DIR=/var/lib/sentinelx/uploads

EXPOSE 8091

CMD ["venv/bin/uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8091"]
