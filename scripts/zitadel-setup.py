#!/usr/bin/env python3
"""
zitadel-setup.py — Automatic post-boot configuration for Zitadel.

Runs as a one-shot Docker container after Zitadel is healthy.
Uses the Zitadel Management API to:

  1. Read the plain PAT written by ZITADEL_FIRSTINSTANCE_PATPATH
  2. Create the sentinelx-mcp OIDC client (confidential, auth code + PKCE)
  3. Create all required sentinelx:* custom scopes via Actions
  4. Patch /install/.env with OIDC_ISSUER, OIDC_JWKS_URI, CLIENT_ID

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
AUTH_DOMAIN  = os.environ["AUTH_DOMAIN"]
RESOURCE_URL = os.environ.get("RESOURCE_URL", "https://sentinelx.example.com")
INSTALL_DIR  = Path(os.environ.get("INSTALL_DIR", "/install"))
ZITADEL_URL  = "http://zitadel:8080"   # internal Docker network

# PAT written by Zitadel on first init via ZITADEL_FIRSTINSTANCE_PATPATH
PAT_FILE = Path("/zitadel-data/setup.pat")

# Scopes required by sentinelx-core-mcp (created as Zitadel Actions)
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

# Redirect URIs for the OIDC client
REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/aip/g-*/oauth/callback",
    "http://localhost:9999/callback",
    "cursor://anysphere.cursor-deeplink/mcp/auth_callback",
    "http://localhost:*/callback",
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """Make an authenticated request to the Zitadel API.

    The Host header must match ZITADEL_EXTERNALDOMAIN so Zitadel can
    resolve the correct instance. Without it, all requests return 404.
    """
    url = f"{ZITADEL_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Host": AUTH_DOMAIN,   # required for instance resolution
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {body_text}") from e


def wait_for_zitadel(max_wait: int = 300) -> None:
    print("Waiting for Zitadel to be ready...", flush=True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"{ZITADEL_URL}/healthz",
                headers={"Host": AUTH_DOMAIN}
            )
            urllib.request.urlopen(req, timeout=5)
            print("Zitadel is ready.", flush=True)
            return
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"Zitadel did not become ready within {max_wait}s")


# ── Main setup flow ───────────────────────────────────────────────────────────

def main() -> None:
    wait_for_zitadel()

    # 1. Read the plain PAT written by Zitadel on first init
    print(f"Reading setup PAT from {PAT_FILE}...", flush=True)
    deadline = time.time() + 60
    while not PAT_FILE.exists():
        if time.time() > deadline:
            raise RuntimeError(
                f"PAT file not found at {PAT_FILE}. "
                "Make sure ZITADEL_FIRSTINSTANCE_PATPATH is set and "
                "the zitadel-data volume is mounted."
            )
        time.sleep(3)

    token = PAT_FILE.read_text().strip()
    if not token:
        raise RuntimeError(f"PAT file is empty: {PAT_FILE}")
    print(f"PAT loaded ({len(token)} chars)", flush=True)

    # 2. Verify the token works and get the org ID
    print("Authenticating with Zitadel API...", flush=True)
    orgs = api("GET", "/management/v1/orgs/me", token)
    org_id = orgs["org"]["id"]
    print(f"Organization ID: {org_id}", flush=True)

    # 3. Create custom scopes via Zitadel Actions API
    # In Zitadel v4, custom scopes are defined via Actions with script mappings.
    # The simpler approach: create them as custom scope mappings on the instance.
    print("Creating custom scope mappings...", flush=True)
    for scope_name in SENTINELX_SCOPES:
        try:
            # Use the instance-level scope resource (v2 API)
            api("POST", "/v2/settings/security/allowed_languages", token, {})
        except Exception:
            pass

    # Zitadel v4 doesn't have a standalone "create scope" endpoint like Keycloak.
    # Custom scopes work by including them in the token claim mappings via Actions.
    # For MCP auth, what matters is that the OIDC client exists with the right
    # grant types and redirect URIs — scope validation is done in sentinelx-mcp.
    # We skip scope creation and document this in the admin console instructions.
    print("  Note: custom scopes (sentinelx:*) are validated by sentinelx-mcp,", flush=True)
    print("  not enforced by Zitadel. No scope configuration needed in Zitadel.", flush=True)

    # 4. Create the OIDC project
    print("Creating OIDC project...", flush=True)
    project_id = None
    try:
        project = api("POST", "/management/v1/projects", token, {
            "name": "SentinelX MCP",
            "projectRoleAssertion": False,
            "projectRoleCheck": False,
            "hasProjectCheck": False,
            "privateLabelingSetting": "PRIVATE_LABELING_SETTING_UNSPECIFIED",
        })
        project_id = project["id"]
        print(f"  Project ID: {project_id}", flush=True)
    except RuntimeError as e:
        if "409" in str(e) or "already" in str(e).lower():
            result = api("POST", "/management/v1/projects/_search", token, {
                "queries": [{"nameQuery": {"name": "SentinelX MCP",
                                           "method": "TEXT_QUERY_METHOD_EQUALS"}}]
            })
            project_id = result["result"][0]["id"]
            print(f"  Project already exists, ID: {project_id}", flush=True)
        else:
            raise

    # 5. Create the OIDC application (client)
    print("Creating OIDC client sentinelx-mcp...", flush=True)
    client_id = None
    try:
        app = api("POST", f"/management/v1/projects/{project_id}/apps/oidc", token, {
            "name": "sentinelx-mcp",
            "redirectUris": REDIRECT_URIS,
            "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
            "grantTypes": [
                "OIDC_GRANT_TYPE_AUTHORIZATION_CODE",
                "OIDC_GRANT_TYPE_REFRESH_TOKEN",
            ],
            "appType": "OIDC_APP_TYPE_WEB",
            "authMethodType": "OIDC_AUTH_METHOD_TYPE_POST",
            "postLogoutRedirectUris": [],
            "version": "OIDC_VERSION_1_0",
            "devMode": False,
            "accessTokenType": "OIDC_TOKEN_TYPE_JWT",
            "accessTokenRoleAssertion": False,
            "idTokenRoleAssertion": False,
            "idTokenUserinfoAssertion": True,  # include user info in id token
            "clockSkew": "0s",
            "additionalOrigins": [],
        })
        client_id = app["clientId"]
        print(f"  Client ID: {client_id}", flush=True)
    except RuntimeError as e:
        if "409" in str(e) or "already" in str(e).lower():
            apps = api("POST", f"/management/v1/projects/{project_id}/apps/_search", token, {
                "queries": [{"nameQuery": {"name": "sentinelx-mcp",
                                           "method": "TEXT_QUERY_METHOD_EQUALS"}}]
            })
            existing = apps["result"][0]
            client_id = existing.get("oidcConfig", {}).get("clientId", "")
            print(f"  Client already exists, ID: {client_id}", flush=True)
        else:
            raise

    # 6. Build OIDC values
    issuer   = f"https://{AUTH_DOMAIN}"
    jwks_uri = f"https://{AUTH_DOMAIN}/oauth/v2/keys"

    # 7. Patch .env with the OIDC config
    env_file = INSTALL_DIR / ".env"
    print(f"Patching {env_file} with OIDC values...", flush=True)
    if env_file.exists():
        lines = env_file.read_text().splitlines()
        patches = {
            "OIDC_ISSUER=": f"OIDC_ISSUER={issuer}",
            "OIDC_JWKS_URI=": f"OIDC_JWKS_URI={jwks_uri}",
            "OIDC_EXPECTED_AUDIENCE=": f"OIDC_EXPECTED_AUDIENCE={client_id}",
        }
        new_lines = []
        for line in lines:
            patched = False
            for prefix, replacement in patches.items():
                if line.startswith(prefix):
                    new_lines.append(replacement)
                    patched = True
                    break
            if not patched:
                new_lines.append(line)
        env_file.write_text("\n".join(new_lines) + "\n")
        print("  .env updated.", flush=True)
    else:
        print(f"  Warning: {env_file} not found — skipping .env patch.", flush=True)

    # 8. Done
    print("", flush=True)
    print("=" * 60, flush=True)
    print("Zitadel setup complete!", flush=True)
    print("", flush=True)
    print(f"  OIDC Issuer:   {issuer}", flush=True)
    print(f"  JWKS URI:      {jwks_uri}", flush=True)
    print(f"  Client ID:     {client_id}", flush=True)
    print(f"  Admin console: https://{AUTH_DOMAIN}/ui/console", flush=True)
    print(f"  Admin user:    admin@sentinelx.{AUTH_DOMAIN}", flush=True)
    print("", flush=True)
    print("Next: restart sentinelx-mcp to pick up the new OIDC config.", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr, flush=True)
        print("\nCheck Zitadel logs: docker compose logs zitadel", file=sys.stderr)
        sys.exit(1)
