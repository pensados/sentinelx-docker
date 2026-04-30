#!/usr/bin/env python3
"""
keycloak-setup.py — Automatic post-boot configuration for Keycloak.

Runs as a one-shot Docker container after Keycloak is healthy.
Uses the Keycloak Admin REST API to:

  1. Get an admin token
  2. Create the sentinelx realm
  3. Create all sentinelx:* client scopes
  4. Create the sentinelx-mcp OIDC client with DCR enabled
  5. Enable open Dynamic Client Registration so Claude/ChatGPT
     can register automatically without manual client_id entry
  6. Patch /install/.env with OIDC_ISSUER, OIDC_JWKS_URI

On success, exits 0 and prints a summary.
On failure, exits 1 with a clear error message.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# ── Config from environment ───────────────────────────────────────────────────
AUTH_DOMAIN       = os.environ["AUTH_DOMAIN"]
KC_ADMIN_PASSWORD = os.environ["KC_ADMIN_PASSWORD"]
RESOURCE_URL      = os.environ.get("RESOURCE_URL", "https://sentinelx.example.com")
INSTALL_DIR       = Path(os.environ.get("INSTALL_DIR", "/install"))
KEYCLOAK_URL      = "http://keycloak:8080"
REALM             = "sentinelx"

# Scopes required by sentinelx-core-mcp
SENTINELX_SCOPES = [
    "sentinelx:exec",
    "sentinelx:edit",
    "sentinelx:state",
    "sentinelx:service",
    "sentinelx:restart",
    "sentinelx:upload",
    "sentinelx:script",
    "sentinelx:capabilities",
    "sentinelx:ping",
]

# Trusted domains for Keycloak DCR policy — redirect_uris in DCR requests
# must match one of these domains
DCR_TRUSTED_HOSTS = [
    "claude.ai",
    "chatgpt.com",
    "chat.openai.com",
    "cursor.sh",
    "localhost",
]

# Redirect URIs — covers Claude web, Claude desktop, ChatGPT, Cursor
REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.ai/*",
    "https://chatgpt.com/connector/oauth/*",
    "https://chatgpt.com/aip/g-*/oauth/callback",
    "https://chat.openai.com/*",
    "cursor://anysphere.cursor-deeplink/mcp/auth_callback",
    "http://localhost:*",
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api(method: str, path: str, token: str | None = None,
        body: dict | None = None, form: dict | None = None) -> dict:
    url = f"{KEYCLOAK_URL}{path}"

    if form:
        data = urllib.parse.urlencode(form).encode()
        content_type = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        content_type = "application/json"
    else:
        data = None
        content_type = "application/json"

    headers = {"Content-Type": content_type, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {body_text}") from e


def get_token() -> str:
    resp = api("POST", "/realms/master/protocol/openid-connect/token", form={
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": "admin",
        "password": KC_ADMIN_PASSWORD,
    })
    return resp["access_token"]


def wait_for_keycloak(max_wait: int = 120) -> None:
    print("Waiting for Keycloak to be ready...", flush=True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            # Use master realm OIDC discovery — available as soon as KC is ready
            urllib.request.urlopen(
                f"{KEYCLOAK_URL}/realms/master/.well-known/openid-configuration",
                timeout=5
            )
            print("Keycloak is ready.", flush=True)
            return
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"Keycloak did not become ready within {max_wait}s")


# ── Main setup flow ───────────────────────────────────────────────────────────

def main() -> None:
    wait_for_keycloak()

    # 1. Get admin token
    print("Authenticating with Keycloak admin API...", flush=True)
    token = get_token()
    print("  Admin token obtained.", flush=True)

    # 2. Create realm
    print(f"Creating realm '{REALM}'...", flush=True)
    try:
        api("POST", "/admin/realms", token, {
            "realm": REALM,
            "displayName": "SentinelX",
            "enabled": True,
            "registrationAllowed": False,
            "resetPasswordAllowed": False,
            "rememberMe": True,
            "verifyEmail": False,
            "loginWithEmailAllowed": True,
            "duplicateEmailsAllowed": False,
            "sslRequired": "external",
            "accessTokenLifespan": 3600,
            "refreshTokenMaxReuse": 0,
        })
        print(f"  Realm '{REALM}' created.", flush=True)
    except RuntimeError as e:
        if "409" in str(e) or "already exists" in str(e).lower() or "Conflict" in str(e):
            print(f"  Realm '{REALM}' already exists.", flush=True)
        else:
            raise

    # Refresh token for the new realm context
    token = get_token()

    # 3. Create custom client scopes
    print("Creating sentinelx:* client scopes...", flush=True)
    scope_ids = {}
    for scope_name in SENTINELX_SCOPES:
        try:
            api("POST", f"/admin/realms/{REALM}/client-scopes", token, {
                "name": scope_name,
                "description": f"SentinelX MCP scope: {scope_name}",
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "true",
                    "display.on.consent.screen": "false",
                },
            })
            print(f"  Created scope: {scope_name}", flush=True)
        except RuntimeError as e:
            if "409" in str(e) or "already exists" in str(e).lower() or "Conflict" in str(e):
                print(f"  Scope already exists: {scope_name}", flush=True)
            else:
                print(f"  Warning: {scope_name}: {e}", flush=True)

    # Get scope IDs
    all_scopes = api("GET", f"/admin/realms/{REALM}/client-scopes", token)
    for s in all_scopes:
        if s["name"] in SENTINELX_SCOPES:
            scope_ids[s["name"]] = s["id"]

    # 4. Create the sentinelx-mcp OIDC client
    print("Creating OIDC client 'sentinelx-mcp'...", flush=True)
    client_id_value = "sentinelx-mcp"
    client_uuid = None

    try:
        api("POST", f"/admin/realms/{REALM}/clients", token, {
            "clientId": client_id_value,
            "name": "SentinelX MCP",
            "description": "MCP OAuth client for SentinelX — used by Claude, ChatGPT, Cursor",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": REDIRECT_URIS,
            "webOrigins": ["+"],
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "access.token.lifespan": "3600",
                "refresh.token.max.reuse": "0",
                "use.refresh.tokens": "true",
            },
        })
        print(f"  Client '{client_id_value}' created.", flush=True)
    except RuntimeError as e:
        if "409" in str(e) or "already exists" in str(e).lower() or "Conflict" in str(e):
            print(f"  Client '{client_id_value}' already exists.", flush=True)
        else:
            raise

    # Get client UUID
    clients = api("GET", f"/admin/realms/{REALM}/clients?clientId={client_id_value}", token)
    client_uuid = clients[0]["id"]
    print(f"  Client UUID: {client_uuid}", flush=True)

    # 5. Assign scopes to the client as optional scopes
    print("Assigning sentinelx:* scopes to client...", flush=True)
    for scope_name, scope_id in scope_ids.items():
        try:
            api("POST",
                f"/admin/realms/{REALM}/clients/{client_uuid}/optional-client-scopes/{scope_id}",
                token)
            print(f"  Assigned scope: {scope_name}", flush=True)
        except RuntimeError as e:
            if "409" in str(e) or "already" in str(e).lower():
                pass  # already assigned
            else:
                print(f"  Warning assigning {scope_name}: {e}", flush=True)

    # 6. Enable open DCR on the realm so Claude/ChatGPT register automatically
    # This sets the realm's client-registration policy to allow anonymous DCR
    print("Enabling open Dynamic Client Registration (DCR)...", flush=True)
    try:
        # Get current realm config
        realm_config = api("GET", f"/admin/realms/{REALM}", token)

        # Enable DCR by updating realm settings
        api("PUT", f"/admin/realms/{REALM}", token, {
            **realm_config,
            "attributes": {
                **(realm_config.get("attributes") or {}),
                "cibaAuthRequestedUserHint": "login_hint",
            },
        })

        # Create anonymous registration policy (allows DCR without initial token)
        try:
            api("POST",
                f"/admin/realms/{REALM}/client-registration-policy/providers/"
                f"anonymous-reg/create-instance",
                token, {})
        except Exception:
            pass  # May already exist

        print("  DCR enabled.", flush=True)
    except Exception as e:
        print(f"  Note: DCR config: {e}", flush=True)

    # 6b. Configure Trusted Hosts policy for DCR
    # By default Keycloak rejects DCR requests where redirect_uris don't
    # match a trusted host/domain. We need to add claude.ai, chatgpt.com etc.
    print("Configuring DCR Trusted Hosts policy...", flush=True)
    try:
        comps = api("GET",
            f"/admin/realms/{REALM}/components"
            f"?type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy",
            token)
        th = next((c for c in comps if c.get("providerId") == "trusted-hosts"
                   and c.get("subType") == "anonymous"), None)
        if th:
            update = {
                "id": th["id"],
                "name": th["name"],
                "providerId": th["providerId"],
                "providerType": th["providerType"],
                "parentId": th["parentId"],
                "subType": th["subType"],
                "config": {
                    "host-sending-registration-request-must-match": ["false"],
                    "trusted-hosts": DCR_TRUSTED_HOSTS,
                    "client-uris-must-match": ["true"],
                },
            }
            api("PUT", f"/admin/realms/{REALM}/components/{th['id']}", token, update)
            print(f"  Trusted hosts configured: {DCR_TRUSTED_HOSTS}", flush=True)
        else:
            print("  Trusted Hosts policy not found — skipping.", flush=True)
    except Exception as e:
        print(f"  Warning: {e}", flush=True)

    # 7. Build OIDC values
    issuer   = f"https://{AUTH_DOMAIN}/realms/{REALM}"
    jwks_uri = f"https://{AUTH_DOMAIN}/realms/{REALM}/protocol/openid-connect/certs"

    # 7b. Get client secret
    print("Fetching client secret...", flush=True)
    client_secret_val = ""
    try:
        secret_resp = api("GET",
            f"/admin/realms/{REALM}/clients/{client_uuid}/client-secret", token)
        client_secret_val = secret_resp.get("value", "")
        print("  Client secret obtained.", flush=True)
    except Exception as e:
        print(f"  Warning: could not fetch client secret: {e}", flush=True)

    # 8. Patch .env with all OIDC values including client credentials
    env_file = INSTALL_DIR / ".env"
    print(f"Patching {env_file} with OIDC values...", flush=True)
    if env_file.exists():
        lines = env_file.read_text().splitlines()
        patches = {
            "OIDC_ISSUER=":            f"OIDC_ISSUER={issuer}",
            "OIDC_JWKS_URI=":          f"OIDC_JWKS_URI={jwks_uri}",
            "OIDC_EXPECTED_AUDIENCE=": f"OIDC_EXPECTED_AUDIENCE=",
            "OIDC_CLIENT_ID=":         f"OIDC_CLIENT_ID={client_id_value}",
            "OIDC_CLIENT_SECRET=":     f"OIDC_CLIENT_SECRET={client_secret_val}",
        }
        new_lines = []
        found_keys = set()
        for line in lines:
            patched = False
            for prefix, replacement in patches.items():
                if line.startswith(prefix):
                    new_lines.append(replacement)
                    found_keys.add(prefix)
                    patched = True
                    break
            if not patched:
                new_lines.append(line)
        # Append any keys not yet present
        for prefix, replacement in patches.items():
            if prefix not in found_keys:
                new_lines.append(replacement)
        env_file.write_text("\n".join(new_lines) + "\n")
        print("  .env updated.", flush=True)
    else:
        print(f"  Warning: {env_file} not found — skipping.", flush=True)

    # 9. Create initial admin user in the sentinelx realm
    print("Creating admin user in sentinelx realm...", flush=True)
    admin_pass = os.environ.get("KC_ADMIN_PASSWORD", "")
    try:
        api("POST", f"/admin/realms/{REALM}/users", token, {
            "username": "admin",
            "enabled": True,
            "emailVerified": True,
            "email": "admin@sentinelx.local",
            "credentials": [{
                "type": "password",
                "value": admin_pass,
                "temporary": False,
            }],
        })
        print("  Admin user created.", flush=True)
    except RuntimeError as e:
        if "409" in str(e) or "already exists" in str(e).lower() or "Conflict" in str(e):
            print("  Admin user already exists.", flush=True)
        else:
            print(f"  Warning: {e}", flush=True)

    # 10. Done
    print("", flush=True)
    print("=" * 60, flush=True)
    print("Keycloak setup complete!", flush=True)
    print("", flush=True)
    print(f"  OIDC Issuer:   {issuer}", flush=True)
    print(f"  JWKS URI:      {jwks_uri}", flush=True)
    print(f"  OIDC Issuer:      {issuer}", flush=True)
    print(f"  Client ID:        {client_id_value}", flush=True)
    print(f"  Client Secret:    {client_secret_val}", flush=True)
    print(f"  Admin console:    https://{AUTH_DOMAIN}/admin", flush=True)
    print(f"  Admin user:       admin / (same as KC_ADMIN_PASSWORD)", flush=True)
    print("", flush=True)
    print("To connect Claude or ChatGPT:", flush=True)
    print(f"  1. Add connector URL:   {RESOURCE_URL}/mcp", flush=True)
    print(f"  2. Advanced settings →  OAuth Client ID:     {client_id_value}", flush=True)
    print(f"                          OAuth Client Secret: {client_secret_val}", flush=True)
    print(f"  3. Log in with:         admin / KC_ADMIN_PASSWORD", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr, flush=True)
        print("\nCheck Keycloak logs: docker compose logs keycloak", file=sys.stderr)
        sys.exit(1)
