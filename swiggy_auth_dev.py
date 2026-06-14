"""Dev-only script: open the Swiggy OAuth login page.

Run this, a browser tab opens, you log in, Swiggy redirects to
http://localhost:8080/callback?code=...  This script catches that redirect,
exchanges the code for tokens, and prints the access + refresh tokens.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

CLIENT_ID = "cart-optimizer-dev"   # arbitrary — registered below if needed
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = "mcp:tools mcp:resources mcp:prompts"
AUTH_ENDPOINT = "https://mcp.swiggy.com/auth/authorize"
TOKEN_ENDPOINT = "https://mcp.swiggy.com/auth/token"
REGISTER_ENDPOINT = "https://mcp.swiggy.com/auth/register"


def pkce_pair():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def register_client():
    """Dynamic client registration (RFC 7591). Returns client_id."""
    payload = json.dumps({
        "client_name": "cart-optimizer-dev",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }).encode()
    req = urllib.request.Request(
        REGISTER_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["client_id"]


# ── tiny one-shot callback server ─────────────────────────────────────────────

_code_received: dict = {}
_server_ready = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _code_received["code"] = (params.get("code") or [None])[0]
        _code_received["error"] = (params.get("error") or [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h1>Got it! You can close this tab.</h1>")

    def log_message(self, *_):
        pass


def _start_callback_server():
    server = http.server.HTTPServer(("localhost", 8080), _CallbackHandler)
    _server_ready.set()
    server.handle_request()   # one request only


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)

    # Try dynamic registration first; fall back to the hardcoded id.
    try:
        client_id = register_client()
        print(f"[auth] registered client_id: {client_id}")
    except Exception as e:
        client_id = CLIENT_ID
        print(f"[auth] registration failed ({e}), using default client_id: {client_id}")

    # Start callback listener before opening the browser.
    t = threading.Thread(target=_start_callback_server, daemon=True)
    t.start()
    _server_ready.wait()

    auth_url = (
        AUTH_ENDPOINT
        + "?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
    )
    print(f"\n[auth] opening: {auth_url}\n")
    webbrowser.open(auth_url)

    t.join(timeout=120)
    code = _code_received.get("code")
    if not code:
        print("[auth] ERROR:", _code_received.get("error", "no code received"))
        return

    # Exchange code for tokens.
    token_data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())

    token_file = Path.home() / ".cart-optimizer" / "token.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({**tokens, "client_id": client_id}, indent=2))

    print("\n[auth] SUCCESS")
    print(f"  access_token:  {tokens.get('access_token', '')[:40]}...")
    print(f"  refresh_token: {tokens.get('refresh_token', '')[:40]}...")
    print(f"  expires_in:    {tokens.get('expires_in')}s")
    print(f"\nToken saved to {token_file}")
    print("Run the optimizer:  python3 -m cart_optimizer.run --budget 300 --restaurant 668678")


if __name__ == "__main__":
    main()
