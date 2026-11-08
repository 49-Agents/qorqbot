"""One-time local Google Calendar connection setup: python -m qorqut_google_calendar.connect --help.

The installed-application OAuth flow with PKCE and a loopback callback; it requests calendar event editing and calendar metadata
reading, not Gmail access. The connection is saved in the private calendar configuration by the host's provider-neutral
``qorqut.calendar.connect.save_connection``. Nothing is read or contacted when this module is imported."""

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import httpx

from qorqut.calendar.connect import save_connection

SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.calendars.readonly"


def google(client_path, port):
    client = json.loads(Path(client_path).read_text())["installed"]
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            valid = urlsplit(self.path).path == "/" and secrets.compare_digest(query.get("state", [""])[0], state)
            if valid:
                result.update(query)
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Return to the terminal to finish connecting." if valid else b"Invalid authorization response.")

        def log_message(self, *args):
            pass  # Authorization codes must never enter access logs.

    with HTTPServer(("127.0.0.1", port), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        print("Open this URL in a browser on this machine (or forward the callback port over SSH):\n" + url)
        webbrowser.open(url)
        server.timeout = 1
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in result:
        raise ValueError("Authorization was cancelled or timed out")
    with httpx.Client(timeout=30) as http:
        response = http.post("https://oauth2.googleapis.com/token", data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "code": result["code"][0], "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"})
        if response.status_code != 200:
            raise ValueError("Google token exchange failed")
        token = response.json()
        calendar = http.get("https://www.googleapis.com/calendar/v3/calendars/primary",
                            headers={"Authorization": "Bearer " + token["access_token"]})
        if calendar.status_code != 200 or "refresh_token" not in token:
            raise ValueError("Google calendar access or offline authorization is missing")
    return {"provider": "google", "email": calendar.json()["id"], "client_id": client["client_id"],
            "client_secret": client["client_secret"], "refresh_token": token["refresh_token"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private output calendar.json, outside Git")
    parser.add_argument("--name", required=True, help="Connection name, e.g. main or work")
    parser.add_argument("--google-client", type=Path, help="Downloaded Desktop OAuth client JSON")
    parser.add_argument("--port", type=int, default=8765, help="Google loopback callback port")
    args = parser.parse_args(argv)
    try:
        if not args.google_client:
            parser.error("Google requires --google-client")
        connection = google(args.google_client, args.port)
        save_connection(args.config, args.name, connection)
        print(f"Saved {args.name} in {args.config}. No events were created.")
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        parser.exit(1, "Connection setup failed. Check the client file, account access and network; no credentials were printed.\n")


if __name__ == "__main__":
    main()
