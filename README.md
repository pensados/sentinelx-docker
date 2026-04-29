# sentinelx-docker

Docker deployment stack for [SentinelX Core](https://github.com/pensados/sentinelx-core) + [SentinelX Core MCP](https://github.com/pensados/sentinelx-core-mcp).

Runs both services as containers while giving the agent full access to the host system via **nsenter** — no SSH keys, no extra configuration.

## Architecture

```
Claude (MCP client)
  │
  │  MCP/HTTP  :8099
  ▼
sentinelx-mcp  🐳  (OIDC validation, tool proxy)
  │
  │  HTTP internal  :8091
  ▼
sentinelx-core  🐳  (pid=host, privileged=true)
  │
  │  nsenter --target 1 --mount --uts --ipc --net --pid
  ▼
Host system  (filesystem, systemd, docker, network — everything)
```

The `sentinelx-core` container runs with `pid: host` and `privileged: true`. Every command is executed via `nsenter` entering the PID 1 namespaces of the host — same filesystem, same network, same services. From the agent's perspective it is identical to running natively on the host.

## Requirements

- Docker Engine 20.10+
- Docker Compose v2
- Linux host (nsenter requires Linux PID namespaces)

## Setup

### 1. Clone this repo and the source repos

```bash
git clone https://github.com/pensados/sentinelx-docker
cd sentinelx-docker
git clone https://github.com/pensados/sentinelx-core
git clone https://github.com/pensados/sentinelx-core-mcp
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env
```

| Variable | Description |
|---|---|
| `SENTINEL_TOKEN` | Internal token between MCP and core. Generate with `openssl rand -hex 32` |
| `OIDC_ISSUER` | OIDC issuer URL (e.g. your Keycloak realm) |
| `OIDC_JWKS_URI` | JWKS endpoint for token validation |
| `OIDC_EXPECTED_AUDIENCE` | Expected `aud` claim (leave empty to skip) |
| `RESOURCE_URL` | Public URL of this SentinelX instance |

### 3. Build and run

```bash
docker compose up -d --build
```

### 4. Verify

```bash
# Check containers
docker compose ps

# Test the agent
curl -s -H "Authorization: Bearer $SENTINEL_TOKEN" \
  http://localhost:8091/capabilities | jq .mode
# → "docker-nsenter"
```

## Security model

`privileged: true` gives the container full access to the host. This is intentional — SentinelX is a trusted agent that needs host-level access to manage infrastructure.

The security boundary is the **command allowlist** in `core/agent_docker.py`. Only commands listed in `ALLOWED_COMMANDS` can be executed, and all executions are logged.

The MCP layer adds OAuth/OIDC authentication — only users with valid tokens and correct scopes can reach the agent.

Ports `8091` and `8099` are bound to `127.0.0.1` only. To expose the MCP externally, place a reverse proxy (nginx, Caddy) in front with TLS.

## Customizing the allowlist

Edit `ALLOWED_COMMANDS` in `core/agent_docker.py` and rebuild:

```bash
docker compose build sentinelx-core
docker compose up -d sentinelx-core
```

## Related projects

- [sentinelx-core](https://github.com/pensados/sentinelx-core) — the agent
- [sentinelx-core-mcp](https://github.com/pensados/sentinelx-core-mcp) — the MCP server

## License

MIT
