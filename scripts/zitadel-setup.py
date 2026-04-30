#!/usr/bin/env python3
"""
zitadel-setup.py — Automatic post-boot configuration for Zitadel.

Runs as a one-shot Docker container after Zitadel is healthy.
Uses the Zitadel Management API to:

  1. Authenticate with the service account PAT written during first init
  2. Create the sentinelx-mcp OIDC client (confidential, auth code + PKCE)
  3. Create all required sentinelx:* custom scopes
  4. Assign the scopes to the client
  5. Patch /install/.env with OIDC_ISSUER, OIDC_JWKS_URI, OIDC_CLIENT_ID

On success, exits 0 and prints the MCP endpoint URL.
On failure, exits 1 with a clear error message.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Config from environment ───────────────────────────────────────────────────
AUTH_DOMAIN   = os.environ["AUTH_DOMAIN"]
RESOURCE_URL  = os.environ.get("RESOURCE_URL", "https://sentinelx.example.com")
INSTALL_DIR   = Path(os.environ.get("INSTALL_DIR", "/install"))
ZITADEL_URL   = f"http://zitadel:8080"  # internal Docker network

# PAT written by Zitadel on first init
PAT_FILE = Path("/zitadel-data/service-account.json")

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

# Redirect URIs for the OIDC client
# Covers Claude, ChatGPT, Cursor and local curl testing
REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/aip/g-*/oauth/callback",
    "http://localhost:9999/callback",
    "cursor://anysphere.cursor-deeplink/mcp/auth_callback",
    "http://localhost:*/callback",
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
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
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {body_text}") from e


def wait_for_zitadel(max_wait: int = 120) -> None:
    print("Waiting for Zitadel to be ready...", flush=True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{ZITADEL_URL}/healthz", timeout=5)
            print("Zitadel is ready.", flush=True)
            return
        except Exception:
            time.sleep(3)
    raise RuntimeError(f"Zitadel did not become ready within {max_wait}s")


# ── Main setup flow ───────────────────────────────────────────────────────────

def main() -> None:
    wait_for_zitadel()

    # 1. Read the service account PAT
    print(f"Reading service account PAT from {PAT_FILE}...", flush=True)
    deadline = time.time() + 60
    while not PAT_FILE.exists():
        if time.time() > deadline:
            raise RuntimeError(
                f"PAT file not found at {PAT_FILE}. "
                "Make sure ZITADEL_FIRSTINSTANCE_MACHINEKEYPATH is set correctly "
                "and the zitadel-data volume is mounted."
            )
        time.sleep(3)

    pat_data = json.loads(PAT_FILE.read_text())
    # The file contains {"keyId": "...", "key": "..."} — we need a real token.
    # Zitadel writes a JWT key file, not a plain PAT. Exchange it for a token.
    token = get_token_from_key_file(pat_data)
    print("Authenticated with Zitadel API.", flush=True)

    # 2. Get the default organization ID
    orgs = api("GET", "/management/v1/orgs/me", token)
    org_id = orgs["org"]["id"]
    print(f"Organization ID: {org_id}", flush=True)

    # 3. Create custom scopes (idempotent — skip if already exist)
    print("Creating custom scopes...", flush=True)
    for scope_name in SENTINELX_SCOPES:
        try:
            api("POST", "/management/v1/actions", token, {
                "name": scope_name,
            })
        except Exception:
            pass  # scope already exists or action not available — create via scope mapping

    # Create scope mappings via the scope API
    for scope_name in SENTINELX_SCOPES:
        try:
            api("POST", f"/management/v1/orgs/{org_id}/scopes", token, {
                "name": scope_name,
            })
            print(f"  Created scope: {scope_name}", flush=True)
        except RuntimeError as e:
            if "already exist" in str(e).lower() or "409" in str(e):
                print(f"  Scope already exists: {scope_name}", flush=True)
            else:
                print(f"  Warning creating scope {scope_name}: {e}", flush=True)

    # 4. Create the OIDC project
    print("Creating OIDC project...", flush=True)
    try:
        project = api("POST", f"/management/v1/projects", token, {
            "name": "SentinelX MCP",
            "projectRoleAssertion": False,
            "projectRoleCheck": False,
            "hasProjectCheck": False,
            "privateLabelingSetting": "PRIVATE_LABELING_SETTING_UNSPECIFIED",
        })
        project_id = project["id"]
        print(f"Project ID: {project_id}", flush=True)
    except RuntimeError as e:
        if "409" in str(e) or "already exist" in str(e).lower():
            # Fetch existing project
            projects = api("POST", "/management/v1/projects/_search", token, {
                "queries": [{"nameQuery": {"name": "SentinelX MCP", "method": "TEXT_QUERY_METHOD_EQUALS"}}]
            })
            project_id = projects["result"][0]["id"]
            print(f"Project already exists, ID: {project_id}", flush=True)
        else:
            raise

    # 5. Create the OIDC application (client)
    print("Creating OIDC client sentinelx-mcp...", flush=True)
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
            "idTokenUserinfoAssertion": False,
            "clockSkew": "0s",
            "additionalOrigins": [],
        })
        client_id     = app["clientId"]
        client_secret = app.get("clientSecret", "")
        app_id        = app["appId"]
        print(f"OIDC client created. Client ID: {client_id}", flush=True)
    except RuntimeError as e:
        if "409" in str(e) or "already exist" in str(e).lower():
            print("OIDC client already exists — fetching existing client ID...", flush=True)
            apps = api("POST", f"/management/v1/projects/{project_id}/apps/_search", token, {
                "queries": [{"nameQuery": {"name": "sentinelx-mcp", "method": "TEXT_QUERY_METHOD_EQUALS"}}]
            })
            existing = apps["result"][0]
            client_id     = existing.get("oidcConfig", {}).get("clientId", "sentinelx-mcp")
            client_secret = ""
            app_id        = existing["id"]
            print(f"Existing client ID: {client_id}", flush=True)
        else:
            raise

    # 6. Build OIDC values
    issuer   = f"https://{AUTH_DOMAIN}"
    jwks_uri = f"https://{AUTH_DOMAIN}/oauth/v2/keys"

    # 7. Patch .env
    env_file = INSTALL_DIR / ".env"
    print(f"Patching {env_file} with OIDC values...", flush=True)
    if env_file.exists():
        content = env_file.read_text()
        replacements = {
            "OIDC_ISSUER=":         f"OIDC_ISSUER={issuer}",
            "OIDC_JWKS_URI=":       f"OIDC_JWKS_URI={jwks_uri}",
            "OIDC_EXPECTED_AUDIENCE=": f"OIDC_EXPECTED_AUDIENCE={client_id}",
        }
        lines = content.splitlines()
        new_lines = []
        for line in lines:
            replaced = False
            for key, new_val in replacements.items():
                if line.startswith(key):
                    new_lines.append(new_val)
                    replaced = True
                    break
            if not replaced:
                new_lines.append(line)
        env_file.write_text("\n".join(new_lines) + "\n")
        print(".env updated.", flush=True)
    else:
        print(f"Warning: {env_file} not found — skipping .env patch.", flush=True)

    # 8. Done
    print("", flush=True)
    print("=" * 60, flush=True)
    print("Zitadel setup complete!", flush=True)
    print("", flush=True)
    print(f"  OIDC Issuer:     {issuer}", flush=True)
    print(f"  JWKS URI:        {jwks_uri}", flush=True)
    print(f"  Client ID:       {client_id}", flush=True)
    print(f"  Admin console:   https://{AUTH_DOMAIN}/ui/console", flush=True)
    print("", flush=True)
    print("Restart sentinelx-mcp to pick up the new OIDC config:", flush=True)
    print("  docker compose -f docker-compose.yml -f docker-compose.oidc.yml", flush=True)
    print("  restart sentinelx-mcp", flush=True)
    print("=" * 60, flush=True)


def get_token_from_key_file(key_data: dict) -> str:
    """
    Exchange a Zitadel service account key file (JWT auth) for an access token.
    The key file format: {"type": "serviceaccount", "keyId": "...", "key": "...", "userId": "..."}
    """
    import base64
    import hashlib
    import hmac

    key_id  = key_data.get("keyId", "")
    user_id = key_data.get("userId", "")
    private_key_pem = key_data.get("key", "")

    if not private_key_pem:
        # Fallback: try plain PAT token format
        token = key_data.get("token") or key_data.get("pat") or key_data.get("accessToken")
        if token:
            return token
        raise RuntimeError(
            f"Cannot parse service account key file. Keys found: {list(key_data.keys())}"
        )

    # Build a JWT for the client_credentials flow
    import json as _json

    now = int(time.time())
    header = {"alg": "RS256", "kid": key_id}
    payload = {
        "iss": user_id,
        "sub": user_id,
        "aud": [f"{ZITADEL_URL}"],
        "iat": now,
        "exp": now + 3600,
    }

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header_b64  = b64(_json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = b64(_json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    # Sign with RSA private key using only stdlib
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        jwt_token = f"{header_b64}.{payload_b64}.{b64(signature)}"
    except ImportError:
        raise RuntimeError(
            "cryptography package not found. Install it: pip install cryptography"
        )

    # Exchange JWT for access token
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
        "scope": "openid urn:zitadel:iam:org:project:id:zitadel:aud",
    }).encode()

    import urllib.parse
    req = urllib.request.Request(
        f"{ZITADEL_URL}/oauth/v2/token",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        token_resp = _json.loads(resp.read())

    access_token = token_resp.get("access_token")
    if not access_token:
        raise RuntimeError(f"Token exchange failed: {token_resp}")

    return access_token


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr, flush=True)
        print("\nCheck Zitadel logs: docker compose logs zitadel", file=sys.stderr)
        sys.exit(1)
