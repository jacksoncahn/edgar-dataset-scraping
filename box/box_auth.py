"""
Box OAuth helper.

Use ensure_box_auth() from other scripts, or run this file directly:
  python box/box_auth.py
"""

from __future__ import annotations

import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from boxsdk import OAuth2
from dotenv import load_dotenv

# Load the project-root .env (this file lives in box/).
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

try:
    from box.box_upload import update_env_vars
except ImportError:  # running as `python box/box_auth.py`
    from box_upload import update_env_vars


def _token_is_currently_valid() -> bool:
    """
    Lightweight check using only BOX_INITIAL_ACCESS_TOKEN.

    Uses a raw HTTP call (not the Box SDK client) so a 401 cannot trigger an
    automatic refresh — that would fail/crash when no refresh token is passed,
    and we also don't want to burn a single-use refresh token during this check.
    """
    access_token = os.getenv("BOX_INITIAL_ACCESS_TOKEN", "").strip()
    if not access_token:
        return False

    try:
        response = requests.get(
            "https://api.box.com/2.0/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except requests.RequestException as error:
        print(f"Auth check network error ({type(error).__name__}); proceeding to login.")
        return False

    if response.status_code == 200:
        return True

    print(
        f"Stored access token is not valid "
        f"(HTTP {response.status_code}); starting login flow."
    )
    return False


def _run_oauth_login() -> None:
    """Open browser OAuth, capture redirect, write tokens into .env."""
    client_id = os.getenv("BOX_CLIENT_ID", "").strip()
    client_secret = os.getenv("BOX_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("BOX_REDIRECT_URI", "http://localhost:3000").strip()

    if not client_id or not client_secret:
        raise SystemExit("Missing BOX_CLIENT_ID / BOX_CLIENT_SECRET in the project .env.")

    oauth = OAuth2(client_id=client_id, client_secret=client_secret)
    auth_url, _csrf_token = oauth.get_authorization_url(redirect_uri)

    auth_code = None

    class TokenReceiver(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                auth_code = query["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"Success! You can close this window and check your terminal."
                )
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, format, *args):  # noqa: A003
            return

    print("Opening browser to log into Box...")
    print(f"Auth URL: {auth_url}")
    webbrowser.open(auth_url)

    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    server = HTTPServer((host, port), TokenReceiver)
    server.handle_request()

    if not auth_code:
        print("Authentication failed (no auth code received).")
        raise SystemExit(1)

    access_token, refresh_token = oauth.authenticate(auth_code)
    print("\n--- Tokens saved to .env ---")
    print(f"BOX_INITIAL_ACCESS_TOKEN={access_token}")
    print(f"BOX_REFRESH_TOKEN={refresh_token}")

    update_env_vars(
        {
            "BOX_INITIAL_ACCESS_TOKEN": access_token,
            "BOX_REFRESH_TOKEN": refresh_token,
        }
    )
    os.environ["BOX_INITIAL_ACCESS_TOKEN"] = access_token
    os.environ["BOX_REFRESH_TOKEN"] = refresh_token


def ensure_box_auth() -> bool:
    """
    Ensure Box tokens are usable before scrapers/uploads run.

    Returns True if existing tokens looked valid; False if a fresh OAuth login
    was performed (tokens written to .env).
    """
    load_dotenv(ROOT_DIR / ".env", override=True)
    if _token_is_currently_valid():
        print("Box credentials already appear valid; skipping login.")
        return True
    _run_oauth_login()
    load_dotenv(ROOT_DIR / ".env", override=True)
    return False


if __name__ == "__main__":
    ensure_box_auth()
