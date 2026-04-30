#!/usr/bin/env bash
# SentinelX Docker — install.sh
#
# One-liner install:
#   curl -fsSL https://raw.githubusercontent.com/pensados/sentinelx-docker/main/install.sh | bash
#
# Or clone first for a transparent install:
#   git clone --recurse-submodules https://github.com/pensados/sentinelx-docker
#   cd sentinelx-docker && bash install.sh

set -euo pipefail

# ── Colors & helpers ─────────────────────────────────────────────────────────
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
BOLD="\033[1m"
NC="\033[0m"

info()    { echo -e "${GREEN}[sentinelx]${NC} $*"; }
warn()    { echo -e "${YELLOW}[sentinelx]${NC} $*"; }
error()   { echo -e "${RED}[sentinelx] ERROR:${NC} $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}$*${NC}"; }
box()     { echo -e "${CYAN}┌─────────────────────────────────────────────┐${NC}"
            echo -e "${CYAN}│${NC}  $*"
            echo -e "${CYAN}└─────────────────────────────────────────────┘${NC}"; }

# ── stdin fix for curl | bash ────────────────────────────────────────────────
# When run via `curl ... | bash`, stdin is the pipe (script source), not the
# terminal. `read` would get EOF immediately and default to option 1 for every
# question. We reopen stdin from /dev/tty so interactive prompts work correctly.
if [ ! -t 0 ] && [ -e /dev/tty ]; then
    exec < /dev/tty
fi

REPO_URL="https://github.com/pensados/sentinelx-docker"
INSTALL_DIR="${SENTINELX_DIR:-$HOME/sentinelx-docker}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1 && warn "DRY-RUN mode — no containers will be started."

# ── Uninstall mode ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    warn "UNINSTALL mode — this will stop and remove the SentinelX stack."
    echo ""
    echo "  Install directory: ${INSTALL_DIR}"
    echo ""

    if [ ! -d "$INSTALL_DIR" ]; then
        warn "Directory $INSTALL_DIR not found — nothing to uninstall."
        exit 0
    fi

    if [ -z "${SX_YES:-}" ]; then
        read -rp "  Are you sure? This removes all containers, volumes and config [y/N]: " _confirm
        [[ "${_confirm:-n}" =~ ^[Yy]$ ]] || { info "Aborted."; exit 0; }
    fi

    cd "$INSTALL_DIR"

    # Source .env to know which mode was installed
    AUTH_MODE="simple"
    DOMAIN_MODE=""
    AUTH_DOMAIN=""
    MCP_DOMAIN=""
    [ -f .env ] && source .env 2>/dev/null || true
    [ -n "${OIDC_ISSUER:-}" ] && AUTH_MODE="oidc"

    # Stop and remove containers + volumes
    if [ -f docker-compose.oidc.yml ] && [ "$AUTH_MODE" = "oidc" ]; then
        info "Stopping OIDC stack (containers + volumes)..."
        docker compose -f docker-compose.yml -f docker-compose.oidc.yml down -v 2>&1 | grep -E "Removed|Stopped|Network" || true
    elif [ -f docker-compose.yml ]; then
        info "Stopping simple stack (containers + volumes)..."
        docker compose -f docker-compose.yml down -v 2>&1 | grep -E "Removed|Stopped|Network" || true
    fi

    # Remove install directory
    cd "$HOME"
    info "Removing $INSTALL_DIR ..."
    rm -rf "$INSTALL_DIR"

    # Print DNS cleanup instructions
    echo ""
    section "Manual cleanup remaining"
    echo ""
    if [ -n "${MCP_DOMAIN:-}" ]; then
        echo "  DNS records to remove:"
        echo "    ${MCP_DOMAIN}"
        [ -n "${AUTH_DOMAIN:-}" ] && echo "    ${AUTH_DOMAIN}"
        echo ""
    fi
    echo "  nginx/Caddy — remove the server blocks for:"
    [ -n "${MCP_DOMAIN:-}" ]  && echo "    ${MCP_DOMAIN}"
    [ -n "${AUTH_DOMAIN:-}" ] && echo "    ${AUTH_DOMAIN}"
    echo ""
    info "SentinelX uninstalled successfully."
    exit 0
fi


echo -e "${CYAN}"
cat << 'EOF'
  ███████╗███████╗███╗   ██╗████████╗██╗███╗   ██╗███████╗██╗     ██╗  ██╗
  ██╔════╝██╔════╝████╗  ██║╚══██╔══╝██║████╗  ██║██╔════╝██║     ╚██╗██╔╝
  ███████╗█████╗  ██╔██╗ ██║   ██║   ██║██╔██╗ ██║█████╗  ██║      ╚███╔╝
  ╚════██║██╔══╝  ██║╚██╗██║   ██║   ██║██║╚██╗██║██╔══╝  ██║      ██╔██╗
  ███████║███████╗██║ ╚████║   ██║   ██║██║ ╚████║███████╗███████╗██╔╝ ██╗
  ╚══════╝╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝  ╚═╝
EOF
echo -e "${NC}"
echo -e "  ${BOLD}AI Agent Installer${NC} — Connect Claude, ChatGPT and other AI assistants"
echo -e "  to your own server via the Model Context Protocol (MCP)."
echo ""
echo -e "  ${YELLOW}What you'll get:${NC} a running MCP endpoint that lets AI tools"
echo -e "  execute commands, edit files and manage services on your server."
echo ""

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
section "Step 1/6 — Checking prerequisites"

command -v git    >/dev/null 2>&1 || error "git is required.  Install: sudo apt install git"
command -v docker >/dev/null 2>&1 || error "Docker is required.  See: https://docs.docker.com/engine/install"
docker compose version >/dev/null 2>&1 || error "Docker Compose v2 is required.  See: https://docs.docker.com/compose/install"
docker info >/dev/null 2>&1 || error "Docker daemon is not running or you don't have access.\n  Try: sudo usermod -aG docker \$USER && newgrp docker"

DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null)
COMPOSE_VERSION=$(docker compose version --short 2>/dev/null)
info "Docker ${DOCKER_VERSION} / Compose ${COMPOSE_VERSION} — OK"

# ── 2. Clone ──────────────────────────────────────────────────────────────────
section "Step 2/6 — Cloning SentinelX"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Directory $INSTALL_DIR already exists — pulling latest..."
    git -C "$INSTALL_DIR" pull --recurse-submodules
    git -C "$INSTALL_DIR" submodule update --init --recursive
else
    info "Cloning into $INSTALL_DIR ..."
    git clone --recurse-submodules "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ── 3. Install pensa-safe-edit ────────────────────────────────────────────────
SAFE_EDIT_SRC="$INSTALL_DIR/sentinelx-core/bin/sentinelx-safe-edit"
if [ -f "$SAFE_EDIT_SRC" ] && ! command -v pensa-safe-edit >/dev/null 2>&1; then
    if sudo cp "$SAFE_EDIT_SRC" /usr/local/bin/pensa-safe-edit && sudo chmod +x /usr/local/bin/pensa-safe-edit; then
        info "Installed pensa-safe-edit to /usr/local/bin"
    else
        warn "Could not install pensa-safe-edit (sudo failed). Run manually:"
        warn "  sudo cp $SAFE_EDIT_SRC /usr/local/bin/pensa-safe-edit"
    fi
fi

# ── 4. Interactive setup ──────────────────────────────────────────────────────
section "Step 3/6 — Configuration"

# Skip interactive prompts if .env already exists
if [ -f .env ]; then
    warn ".env already exists — skipping interactive setup."
    warn "To reconfigure, delete .env and run install.sh again."
    source .env 2>/dev/null || true
    EXEC_MODE="${SENTINEL_EXEC_MODE:-host}"
    AUTH_MODE="simple"
    [ -n "${OIDC_ISSUER:-}" ] && AUTH_MODE="oidc"
else

    # ── 4a. Execution mode ───────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}Execution mode${NC}"
    echo "  This controls where SentinelX runs commands."
    echo ""
    echo "  1) host       — Commands run on your real server (filesystem, systemd,"
    echo "                  docker, nginx, etc.). Full access. Recommended for"
    echo "                  managing your infrastructure with AI."
    echo ""
    echo "  2) container  — Commands run inside an isolated container."
    echo "                  Safe sandbox. Good for development or untrusted tasks."
    echo ""
    if [ -n "${SX_EXEC_MODE:-}" ]; then
        _exec_choice="$SX_EXEC_MODE"
    else
        read -rp "  Choose [1/2] (default: 1): " _exec_choice
    fi
    case "${_exec_choice:-1}" in
        2|container) EXEC_MODE="container" ;;
        *) EXEC_MODE="host" ;;
    esac
    info "Execution mode: ${EXEC_MODE}"

    # ── 4b. Authentication mode ───────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}Authentication mode${NC}"
    echo "  This controls how Claude or ChatGPT authenticate when connecting"
    echo "  to your MCP endpoint."
    echo ""
    echo "  1) simple  — A single secret token protects the endpoint."
    echo "               Easy setup. Good for personal use."
    echo ""
    echo "  2) oidc    — Full OAuth2/OIDC via Keycloak (included in the stack)."
    echo "               Login with username + password. Supports multiple users,"
    echo "               fine-grained permissions, and audit logs."
    echo "               Recommended for teams or production deployments."
    echo ""
    if [ -n "${SX_AUTH_MODE:-}" ]; then
        _auth_choice="$SX_AUTH_MODE"
    else
        read -rp "  Choose [1/2] (default: 1): " _auth_choice
    fi
    case "${_auth_choice:-1}" in
        2|oidc) AUTH_MODE="oidc" ;;
        *) AUTH_MODE="simple" ;;
    esac
    info "Authentication mode: ${AUTH_MODE}"

    # ── 4c. Domain / endpoint setup ──────────────────────────────────────────
    echo ""
    echo -e "${BOLD}MCP Endpoint domain${NC}"
    echo ""
    echo "  The MCP endpoint is the URL you add to Claude (claude.ai → Settings →"
    echo "  Integrations) or ChatGPT to connect them to your server. It needs to"
    echo "  be publicly accessible over HTTPS."
    echo ""

    # Detect public IP
    PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || \
                curl -s --max-time 5 https://ifconfig.me 2>/dev/null || \
                echo "")

    if [ -n "$PUBLIC_IP" ]; then
        SSLIP_DOMAIN="${PUBLIC_IP}.sslip.io"
        info "Detected public IP: ${PUBLIC_IP}"
    else
        warn "Could not detect public IP automatically."
        SSLIP_DOMAIN=""
    fi

    echo "  1) Automatic  — Use ${SSLIP_DOMAIN:-<ip>.sslip.io} (no DNS setup needed,"
    echo "                  works immediately, ideal for testing)"
    echo ""
    echo "  2) Manual     — Use your own domain (e.g. sentinelx.yourdomain.com)"
    echo "                  We'll tell you exactly which DNS records to create."
    echo ""
    echo "  3) Cloudflare — Auto-create the DNS record using a Cloudflare API token."
    echo ""
    if [ -n "${SX_DOMAIN_MODE:-}" ]; then
        _domain_choice="$SX_DOMAIN_MODE"
    else
        read -rp "  Choose [1/2/3] (default: 1): " _domain_choice
    fi

    case "${_domain_choice:-1}" in
        2|manual)
            DOMAIN_MODE="manual"
            echo ""
            BASE_DOMAIN="${SX_BASE_DOMAIN:-}"
            [ -z "$BASE_DOMAIN" ] && read -rp "  Base domain (e.g. yourdomain.com): " BASE_DOMAIN
            BASE_DOMAIN="${BASE_DOMAIN:-yourdomain.com}"
            MCP_SUBDOMAIN="${SX_MCP_SUBDOMAIN:-sentinelx}"
            MCP_DOMAIN="${MCP_SUBDOMAIN}.${BASE_DOMAIN}"
            if [ "$AUTH_MODE" = "oidc" ]; then
                AUTH_SUBDOMAIN="${SX_AUTH_SUBDOMAIN:-auth-sentinelx}"
                AUTH_DOMAIN="${AUTH_SUBDOMAIN}.${BASE_DOMAIN}"
            fi
            ;;
        3|cloudflare)
            DOMAIN_MODE="cloudflare"
            echo ""
            BASE_DOMAIN="${SX_BASE_DOMAIN:-}"
            [ -z "$BASE_DOMAIN" ] && read -rp "  Base domain (e.g. yourdomain.com): " BASE_DOMAIN
            BASE_DOMAIN="${BASE_DOMAIN:-yourdomain.com}"
            CF_TOKEN="${SX_CF_TOKEN:-}"
            [ -z "$CF_TOKEN" ] && read -rp "  Cloudflare API Token (Zone:Edit permissions): " CF_TOKEN
            MCP_SUBDOMAIN="sentinelx"
            MCP_DOMAIN="${MCP_SUBDOMAIN}.${BASE_DOMAIN}"
            if [ "$AUTH_MODE" = "oidc" ]; then
                AUTH_SUBDOMAIN="auth-sentinelx"
                AUTH_DOMAIN="${AUTH_SUBDOMAIN}.${BASE_DOMAIN}"
            fi
            ;;
        *)
            DOMAIN_MODE="auto"
            if [ -z "$SSLIP_DOMAIN" ]; then
                error "Could not detect public IP. Please choose manual domain setup."
            fi
            MCP_DOMAIN="$SSLIP_DOMAIN"
            if [ "$AUTH_MODE" = "oidc" ]; then
                AUTH_DOMAIN="auth.${SSLIP_DOMAIN}"
            fi
            ;;
    esac

    MCP_URL="https://${MCP_DOMAIN}"
    AUTH_URL="${AUTH_DOMAIN:+https://${AUTH_DOMAIN}}"

    info "MCP endpoint will be: ${MCP_URL}/mcp"

    # ── 4d. DNS setup ────────────────────────────────────────────────────────
    if [ "$DOMAIN_MODE" = "manual" ]; then
        echo ""
        echo -e "${YELLOW}  ── DNS records to create ────────────────────────────────────${NC}"
        echo ""
        printf "  %-8s %-35s %s\n" "Type" "Name" "Value"
        printf "  %-8s %-35s %s\n" "────" "──────────────────────────────────" "──────────────────"
        printf "  %-8s %-35s %s\n" "A" "${MCP_DOMAIN}" "${PUBLIC_IP:-<your-server-ip>}"
        if [ "$AUTH_MODE" = "oidc" ]; then
            printf "  %-8s %-35s %s\n" "A" "${AUTH_DOMAIN}" "${PUBLIC_IP:-<your-server-ip>}"
        fi
        echo ""
        echo "  You can verify propagation with:"
        echo "    dig ${MCP_DOMAIN} +short"
        echo ""
        if [ -z "${SX_SKIP_DNS_WAIT:-}" ]; then
            read -rp "  Press Enter once the DNS records are active (or Ctrl+C to abort)..."
        else
            warn "SX_SKIP_DNS_WAIT set — skipping DNS wait prompt."
        fi

        # Verify DNS propagation (up to 2 min)
        if [ -n "${SX_SKIP_DNS_WAIT:-}" ]; then
            warn "SX_SKIP_DNS_WAIT set — skipping DNS verification."
            DNS_OK=true
        else
        info "Verifying DNS propagation..."
        DNS_OK=false
        for i in $(seq 1 12); do
            resolved=$(dig +short "$MCP_DOMAIN" 2>/dev/null | tail -1 || true)
            if [ -n "$resolved" ]; then
                info "DNS resolved: ${MCP_DOMAIN} → ${resolved}"
                DNS_OK=true
                break
            fi
            warn "Not propagated yet... (${i}/12) waiting 10s"
            sleep 10
        done
        if [ "$DNS_OK" = "false" ]; then
            warn "DNS did not propagate within 2 minutes."
            if [ -n "${SX_SKIP_DNS_WAIT:-}" ] || [ -n "${SX_YES:-}" ]; then
                warn "Continuing anyway (SX_SKIP_DNS_WAIT/SX_YES set)."
            else
                read -rp "  Continue anyway? [y/N]: " _continue
                [[ "${_continue:-n}" =~ ^[Yy]$ ]] || exit 1
            fi
        fi
        fi  # end SX_SKIP_DNS_WAIT check

    elif [ "$DOMAIN_MODE" = "cloudflare" ]; then
        info "Creating DNS record via Cloudflare API..."

        # Get zone ID
        CF_ZONE_ID=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones?name=${BASE_DOMAIN}" \
            -H "Authorization: Bearer ${CF_TOKEN}" \
            -H "Content-Type: application/json" \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result'][0]['id'])" 2>/dev/null) \
            || error "Could not fetch Cloudflare zone for ${BASE_DOMAIN}. Check your API token."

        # Create MCP record
        curl -s -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records" \
            -H "Authorization: Bearer ${CF_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{\"type\":\"A\",\"name\":\"${MCP_SUBDOMAIN}\",\"content\":\"${PUBLIC_IP}\",\"proxied\":true}" \
            >/dev/null
        info "Created DNS record: ${MCP_DOMAIN} → ${PUBLIC_IP}"

        # Create auth record if OIDC
        if [ "$AUTH_MODE" = "oidc" ]; then
            curl -s -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records" \
                -H "Authorization: Bearer ${CF_TOKEN}" \
                -H "Content-Type: application/json" \
                -d "{\"type\":\"A\",\"name\":\"${AUTH_SUBDOMAIN}\",\"content\":\"${PUBLIC_IP}\",\"proxied\":true}" \
                >/dev/null
            info "Created DNS record: ${AUTH_DOMAIN} → ${PUBLIC_IP}"
        fi
    fi
    # (DOMAIN_MODE=auto: sslip.io, no DNS action needed)

    # ── 4e. Generate .env ────────────────────────────────────────────────────
    SENTINEL_TOKEN=$(openssl rand -hex 32)

    if [ "$AUTH_MODE" = "oidc" ]; then
        KC_DB_PASSWORD=$(openssl rand -hex 16)
        KC_ADMIN_PASSWORD=$(openssl rand -hex 12)
        OIDC_ISSUER="https://${AUTH_DOMAIN}/realms/sentinelx"
        OIDC_JWKS_URI="https://${AUTH_DOMAIN}/realms/sentinelx/protocol/openid-connect/certs"
    fi

    cat > .env <<EOF
# SentinelX Docker — generated by install.sh
# KEEP THIS FILE SECRET — do not commit it.

# ── Core ──────────────────────────────────────────────────────────────────────
SENTINEL_TOKEN=${SENTINEL_TOKEN}
SENTINEL_EXEC_MODE=${EXEC_MODE}

# ── Endpoint ──────────────────────────────────────────────────────────────────
MCP_DOMAIN=${MCP_DOMAIN}
RESOURCE_URL=https://${MCP_DOMAIN}

# ── Auth mode: ${AUTH_MODE} ───────────────────────────────────────────────────
AUTH_MODE=${AUTH_MODE}
EOF

    if [ "$AUTH_MODE" = "oidc" ]; then
        cat >> .env <<EOF
OIDC_ISSUER=${OIDC_ISSUER}
OIDC_JWKS_URI=${OIDC_JWKS_URI}
OIDC_EXPECTED_AUDIENCE=
OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
AUTH_DEBUG=false

# ── Keycloak ──────────────────────────────────────────────────────────────────
AUTH_DOMAIN=${AUTH_DOMAIN}
KC_DB_PASSWORD=${KC_DB_PASSWORD}
KC_ADMIN_PASSWORD=${KC_ADMIN_PASSWORD}
RESOURCE_URL=${RESOURCE_URL:-https://${MCP_DOMAIN}}
EOF
    else
        cat >> .env <<EOF
OIDC_ISSUER=
OIDC_JWKS_URI=
OIDC_EXPECTED_AUDIENCE=
AUTH_DEBUG=false
EOF
    fi

    chmod 600 .env
    info "Generated .env"

    # ── Detect available ports ────────────────────────────────────────────────
    # Find free ports for core and mcp in case defaults are taken
    _find_free_port() {
        local start=$1
        local port=$start
        while ss -tuln 2>/dev/null | grep -q ":${port} " ; do
            port=$((port + 1))
        done
        echo $port
    }
    CORE_PORT=$(_find_free_port 8091)
    MCP_PORT=$(_find_free_port 8099)
    if [ "$AUTH_MODE" = "oidc" ]; then
        KC_PORT=$(_find_free_port 8180)
    fi
    # Write port overrides to .env
    echo "CORE_PORT=${CORE_PORT}" >> .env
    echo "MCP_PORT=${MCP_PORT}" >> .env
    [ "$AUTH_MODE" = "oidc" ] && echo "KC_PORT=${KC_PORT}" >> .env
    [ "${CORE_PORT}" != "8091" ] && warn "Port 8091 in use — using ${CORE_PORT} for sentinelx-core"
    [ "${MCP_PORT}" != "8099" ]  && warn "Port 8099 in use — using ${MCP_PORT} for sentinelx-mcp"

fi # end of interactive setup block

# Re-source .env to get all vars
source .env 2>/dev/null || true
EXEC_MODE="${SENTINEL_EXEC_MODE:-host}"
MCP_DOMAIN="${MCP_DOMAIN:-localhost}"
AUTH_MODE="${AUTH_MODE:-simple}"
MCP_URL="https://${MCP_DOMAIN}"

# ── 5. Summary & confirmation ────────────────────────────────────────────────
section "Step 4/6 — Summary"

echo ""
echo -e "  ${BOLD}Configuration summary${NC}"
echo ""
printf "  %-22s %s\n" "Execution mode:"   "${EXEC_MODE}"
printf "  %-22s %s\n" "Authentication:"   "${AUTH_MODE}"
printf "  %-22s %s\n" "MCP endpoint:"     "${MCP_URL}/mcp"
if [ "$AUTH_MODE" = "oidc" ] && [ -n "${AUTH_DOMAIN:-}" ]; then
    printf "  %-22s %s\n" "Auth (Keycloak):"   "https://${AUTH_DOMAIN}"
fi
echo ""
echo -e "  ${YELLOW}Connect Claude at:${NC}  claude.ai → Settings → Integrations"
echo -e "             URL:    ${BOLD}${MCP_URL}/mcp${NC}"
echo ""

if [ -z "${SX_YES:-}" ]; then
    read -rp "  Start SentinelX with this configuration? [Y/n]: " _confirm
    [[ "${_confirm:-y}" =~ ^[Yy]$ ]] || { warn "Aborted."; exit 0; }
else
    info "SX_YES set — skipping confirmation."
fi

# ── 6. Build & start ─────────────────────────────────────────────────────────
section "Step 5/6 — Building and starting SentinelX"

# Pick the right compose file based on auth mode
# Pick the right compose files based on auth mode
COMPOSE_BASE="-f docker-compose.yml"
if [ "$AUTH_MODE" = "oidc" ]; then
    COMPOSE_OVERRIDE="-f docker-compose.oidc.yml"
else
    COMPOSE_OVERRIDE=""
fi
COMPOSE_CMD="docker compose ${COMPOSE_BASE} ${COMPOSE_OVERRIDE}"


if [ "$DRY_RUN" = "1" ]; then
    info "DRY-RUN: would run: ${COMPOSE_CMD} up -d --build"
    info "DRY-RUN: .env generated at $INSTALL_DIR/.env"
    info "DRY-RUN: configuration complete — exiting without starting containers."
    cat "$INSTALL_DIR/.env"
    exit 0
fi

info "Building images..."
cd "$INSTALL_DIR" && env -i HOME="$HOME" PATH="$PATH" \
    bash -c "${COMPOSE_CMD} up -d --build"

info "Waiting for health checks..."
sleep 8

if docker compose ${COMPOSE_BASE} ${COMPOSE_OVERRIDE} ps | grep -q "healthy\|running\|Up"; then
    info "SentinelX is running!"
else
    warn "Containers may still be starting. Check:"
    echo "  cd $INSTALL_DIR && ${COMPOSE_CMD} ps"
    echo "  cd $INSTALL_DIR && ${COMPOSE_CMD} logs"
fi

# ── 7. Reverse proxy config ───────────────────────────────────────────────────
section "Step 6/6 — Reverse proxy configuration"

MCP_PORT="8099"
AUTH_KC_PORT="8080"

echo ""
echo -e "  ${BOLD}SentinelX MCP is running on localhost:${MCP_PORT}${NC}"
echo "  You need to expose it publicly via a reverse proxy with HTTPS."
echo "  Below are ready-to-use config blocks for the most common options."
echo ""

# ── nginx ──
echo -e "  ${BOLD}── nginx ────────────────────────────────────────────────────${NC}"
cat << NGINX_EOF

  Add this server block to your nginx configuration:

  server {
      listen 80;
      server_name ${MCP_DOMAIN};
      return 301 https://\$host\$request_uri;
  }

  server {
      listen 443 ssl http2;
      server_name ${MCP_DOMAIN};

      ssl_certificate     /path/to/fullchain.pem;
      ssl_certificate_key /path/to/privkey.pem;

      # The MCP endpoint — this is the URL you add to Claude or ChatGPT
      location /mcp {
          proxy_pass http://127.0.0.1:${MCP_PORT};
          proxy_http_version 1.1;
          proxy_set_header Host \$host;
          proxy_set_header X-Forwarded-Proto https;
          proxy_set_header Authorization \$http_authorization;
          proxy_buffering off;
          proxy_request_buffering off;
          proxy_read_timeout 3600s;
          proxy_send_timeout 3600s;
          add_header Cache-Control "no-cache";
      }

      # OAuth protected resource discovery (required for Claude/ChatGPT OAuth flow)
      # Only relevant in OIDC mode — in simple mode the token is passed directly
      location /.well-known/oauth-protected-resource {
          default_type application/json;
          return 200 '{"resource":"https://${MCP_DOMAIN}","authorization_servers":["${OIDC_ISSUER:-https://${AUTH_DOMAIN:-${MCP_DOMAIN}}}"],"scopes_supported":["openid","sentinelx:exec","sentinelx:edit","sentinelx:state","sentinelx:service","sentinelx:upload","sentinelx:script","sentinelx:capabilities"]}';
      }
  }

NGINX_EOF

# ── Caddy ──
echo -e "  ${BOLD}── Caddy ────────────────────────────────────────────────────${NC}"
cat << CADDY_EOF

  Add this to your Caddyfile (Caddy handles TLS automatically):

  ${MCP_DOMAIN} {
      handle /mcp {
          reverse_proxy 127.0.0.1:${MCP_PORT}
      }
      handle /.well-known/oauth-protected-resource {
          respond \`{"resource":"https://${MCP_DOMAIN}","authorization_servers":["${OIDC_ISSUER:-https://${MCP_DOMAIN}}"],"scopes_supported":["sentinelx:exec","sentinelx:edit","sentinelx:state","sentinelx:service","sentinelx:upload","sentinelx:script","sentinelx:capabilities"]}\` 200
      }
  }

CADDY_EOF

# Keycloak auth domain block (OIDC mode only)
if [ "$AUTH_MODE" = "oidc" ] && [ -n "${AUTH_DOMAIN:-}" ]; then
    echo -e "  ${BOLD}── nginx (Keycloak auth — ${AUTH_DOMAIN}) ──────────────────────${NC}"
    echo ""
    echo -e "  ${BOLD}⚠️  IMPORTANT — Cloudflare DNS:${NC}"
    echo "  The auth subdomain (${AUTH_DOMAIN}) must be set to"
    echo "  proxied=false (DNS only / grey cloud) in Cloudflare."
    echo "  Cloudflare Bot Fight Mode blocks OAuth/DCR requests without"
    echo "  User-Agent, which breaks the Claude/ChatGPT login flow."
    echo ""
    cat << KC_NGINX_EOF

  server {
      listen 443 ssl http2;
      server_name ${AUTH_DOMAIN};

      ssl_certificate     /path/to/fullchain.pem;
      ssl_certificate_key /path/to/privkey.pem;

      location / {
          proxy_pass http://127.0.0.1:${AUTH_KC_PORT};
          proxy_http_version 1.1;
          proxy_set_header Host \$host;
          proxy_set_header X-Forwarded-Proto https;
          proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
          proxy_read_timeout 3600s;
          proxy_buffering off;
      }
  }

KC_NGINX_EOF
fi

# ── Final instructions ────────────────────────────────────────────────────────
section "You're ready!"

echo ""
box "Your MCP endpoint:  ${MCP_URL}/mcp"
echo ""
echo -e "  ${BOLD}Connect Claude${NC}"
echo "    1. Go to claude.ai → Settings → Integrations"
echo "    2. Click 'Add custom connector'"
echo "    3. Paste this URL: ${MCP_URL}/mcp"
if [ "$AUTH_MODE" = "simple" ]; then
    echo "    4. When prompted for a token, use: ${SENTINEL_TOKEN:-<your token from .env>}"
else
    echo "    4. You'll be redirected to Keycloak to log in."
    echo "       Admin credentials are in .env (KC_ADMIN_PASSWORD)"
    echo ""
    echo "  OAuth credentials for Claude / ChatGPT connector:"
    echo "  (Run after keycloak-setup finishes — check .env or docker logs keycloak-setup)"
    echo "    OAuth Client ID:     sentinelx-mcp"
    echo "    OAuth Client Secret: (see OIDC_CLIENT_SECRET in .env after setup)"
fi
echo ""
echo -e "  ${BOLD}Connect ChatGPT${NC}"
echo "    1. Go to chatgpt.com → Settings → Connected apps → Add"
echo "    2. Paste the same URL: ${MCP_URL}/mcp"
echo ""
echo -e "  ${BOLD}Useful commands${NC}"
echo "    Check status:   cd ${INSTALL_DIR} && ${COMPOSE_CMD} ps"
echo "    View logs:      cd ${INSTALL_DIR} && ${COMPOSE_CMD} logs -f"
echo "    Stop:           cd ${INSTALL_DIR} && ${COMPOSE_CMD} down"
echo "    Restart:        cd ${INSTALL_DIR} && ${COMPOSE_CMD} restart"
echo ""
info "Installation complete."
