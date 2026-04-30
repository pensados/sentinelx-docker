# sentinelx-docker

Docker deployment stack for [SentinelX Core](https://github.com/pensados/sentinelx-core) + [SentinelX Core MCP](https://github.com/pensados/sentinelx-core-mcp).

Lets Claude, ChatGPT and other AI clients manage your server via the **Model Context Protocol (MCP)** — execute commands, edit files, restart services, monitor state — all from the AI chat interface.

---

## Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/pensados/sentinelx-docker/main/install.sh | bash
```

The interactive installer asks 3 questions (exec mode, auth mode, domain) and handles everything else: clones the repo, generates secrets, writes `.env`, and starts the stack.

### Headless / CI install

```bash
SX_YES=1 \
SX_EXEC_MODE=host \
SX_AUTH_MODE=oidc \
SX_DOMAIN_MODE=manual \
SX_BASE_DOMAIN=yourdomain.com \
  bash install.sh
```

| Variable | Values | Description |
|---|---|---|
| `SX_EXEC_MODE` | `host` / `container` | How commands run (see below) |
| `SX_AUTH_MODE` | `simple` / `oidc` | Authentication mode |
| `SX_DOMAIN_MODE` | `sslip` / `manual` / `cloudflare` | Domain setup |
| `SX_BASE_DOMAIN` | `yourdomain.com` | Required for `manual` and `cloudflare` |
| `SX_YES` | `1` | Skip all confirmations |
| `SX_SKIP_DNS_WAIT` | `1` | Skip DNS propagation wait |

Add `--dry-run` to generate `.env` without starting containers.

---

## Authentication modes

### Simple auth (`SX_AUTH_MODE=simple`)

A shared secret token is set in the connector config. Fast to set up, good for personal use.

**Stack:** 2 containers — `sentinelx-core` + `sentinelx-mcp`

### OIDC auth (`SX_AUTH_MODE=oidc`)

Full OAuth2/OIDC via **Keycloak** (bundled in the stack). Users log in with username + password. Supports multiple users, scopes, and audit logs. Required for Claude web / ChatGPT automatic OAuth flow.

**Stack:** 4 containers — `sentinelx-core` + `sentinelx-mcp` + `keycloak` + `keycloak-db`

After the stack starts, `keycloak-setup` runs automatically and prints:

```
============================================================
Keycloak setup complete!

  OIDC Issuer:      https://auth.yourdomain.com/realms/sentinelx
  Client ID:        sentinelx-mcp
  Client Secret:    <generated>
  Admin console:    https://auth.yourdomain.com/admin
  Admin user:       admin / KC_ADMIN_PASSWORD from .env

To connect Claude or ChatGPT:
  1. Add connector URL:    https://sentinelx.yourdomain.com/mcp
  2. Advanced OAuth settings:
       OAuth Client ID:     sentinelx-mcp
       OAuth Client Secret: <generated>
  3. Log in with:          admin / KC_ADMIN_PASSWORD
============================================================
```

The `client_id` and `client_secret` are also written to `.env` as `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` for reference.

> **Cloudflare users:** the auth subdomain (`auth.yourdomain.com`) must be set to **proxied=false** (grey cloud / DNS only) in Cloudflare. Cloudflare's Bot Fight Mode blocks OAuth requests without a browser User-Agent, which breaks the Claude/ChatGPT login flow.

---

## Execution modes

### Host mode (`SX_EXEC_MODE=host`) — recommended

`sentinelx-core` runs with `pid: host` and `privileged: true`. Every command is executed via `nsenter` entering PID 1's namespaces — same filesystem, same network, same services as the host. The agent behaves identically to running natively.

### Container mode (`SX_EXEC_MODE=container`)

Commands run inside an isolated container. No host access. Good for development or sandboxed environments. The allowlist is not enforced in this mode — Docker is the security boundary.

---

## Architecture

```
Claude / ChatGPT (MCP client)
  │
  │  HTTPS  /mcp
  ▼
[nginx / Caddy — TLS termination]
  │
  │  HTTP  :8099
  ▼
sentinelx-mcp  🐳  (OIDC validation, tool proxy)
  │
  │  HTTP  :8091 (internal)
  ▼
sentinelx-core  🐳  (host mode: pid=host, privileged=true)
  │
  │  nsenter --target 1 --mount --uts --ipc --net --pid
  ▼
Host system  (filesystem, systemd, docker, network)

─── OIDC mode only ──────────────────────────────────────
  │
  ▼
keycloak  🐳  (:8080 internal)  ←  keycloak-db  🐳
  │
  │  HTTPS  auth.yourdomain.com
  ▼
[nginx / Caddy — TLS termination, proxied=false in Cloudflare]
```

---

## Requirements

- Docker Engine 20.10+
- Docker Compose v2
- Linux host (host mode requires Linux PID namespaces)
- A domain with DNS pointing to your server (for OIDC mode)
- nginx or Caddy for TLS termination (install.sh prints the config)

---

## Manual setup

```bash
git clone --recurse-submodules https://github.com/pensados/sentinelx-docker
cd sentinelx-docker
cp .env.example .env
# Edit .env — set SENTINEL_TOKEN and auth variables
docker compose up -d --build                                     # simple auth
docker compose -f docker-compose.yml -f docker-compose.oidc.yml up -d --build  # OIDC
```

---

## Configuration reference

| Variable | Description |
|---|---|
| `SENTINEL_TOKEN` | Internal token between MCP and core |
| `SENTINEL_EXEC_MODE` | `host` or `container` |
| `OIDC_ISSUER` | OIDC issuer URL (set by keycloak-setup) |
| `OIDC_JWKS_URI` | JWKS endpoint for token validation (set by keycloak-setup) |
| `OIDC_CLIENT_ID` | OAuth client ID (set by keycloak-setup) |
| `OIDC_CLIENT_SECRET` | OAuth client secret (set by keycloak-setup) |
| `RESOURCE_URL` | Public HTTPS URL of this MCP endpoint |
| `AUTH_DOMAIN` | Subdomain for Keycloak (OIDC mode only) |
| `KC_ADMIN_PASSWORD` | Keycloak admin password (OIDC mode only) |
| `KC_DB_PASSWORD` | Keycloak DB password (OIDC mode only) |

---

## Security model

`privileged: true` gives the container full access to the host. This is intentional — SentinelX is a trusted agent for infrastructure management.

Security layers:

1. **Command allowlist** — `core/agent_docker.py` defines which commands the agent can run
2. **OIDC/OAuth** — only authenticated users with valid tokens and correct scopes reach the agent
3. **Network** — ports `8091` and `8099` are bound to `127.0.0.1` only; external access only via TLS reverse proxy

---

## Operations

```bash
# Logs
docker compose logs -f

# Restart
docker compose restart sentinelx-mcp

# Update submodules
git submodule update --remote --merge
git add sentinelx-core sentinelx-core-mcp
git commit -m "chore: update submodules"
docker compose up -d --build
```

---

## Related projects

- [sentinelx-core](https://github.com/pensados/sentinelx-core) — the agent
- [sentinelx-core-mcp](https://github.com/pensados/sentinelx-core-mcp) — the MCP server

## License

MIT
