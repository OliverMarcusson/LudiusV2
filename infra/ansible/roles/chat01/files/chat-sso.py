#!/usr/bin/python3
"""Kerberos sign-in for Mattermost on chat01, posing as GitLab's OAuth (Team Edition's only SSO). nginx checks the
browser's Kerberos ticket (SPNEGO) on /oauth/authorize and passes the account on in X-Remote-User; this answers with
a one-time code. Mattermost trades it for a token (/oauth/token) and reads the user (/api/v4/user), both over
loopback. Only the narrative's people (USERS) sign in; codes and tokens are single use and expire after TTL seconds
of monotonic time (the range clock jumps)."""

import base64
import json
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REALM = os.environ["REALM"]
CLIENT_ID = os.environ["CLIENT_ID"]
REDIRECT = os.environ["REDIRECT"]  # Mattermost's <site>/signup/gitlab/complete
SECRET = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "secret").read_text().strip()
USERS: dict[str, dict] = json.loads(Path(os.environ["USERS"]).read_text())
TTL = 120
codes: dict[str, tuple[str, float]] = {}
tokens: dict[str, tuple[str, float]] = {}


def take(store: dict[str, tuple[str, float]], key: str) -> str:
    user, at = store.pop(key, ("", 0.0))
    return user if time.monotonic() - at < TTL else ""


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: dict | None = None, location: str = "") -> None:
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/oauth/authorize":
            q = dict(urllib.parse.parse_qsl(url.query))
            name, _, realm = self.headers.get("X-Remote-User", "").partition("@")
            user = name.lower()
            if q.get("client_id") != CLIENT_ID or (realm and realm.upper() != REALM) or user not in USERS:
                return self.send(403, {"error": "access_denied"})
            code = secrets.token_urlsafe(32)
            codes[code] = (user, time.monotonic())
            query = urllib.parse.urlencode({"code": code, "state": q.get("state", "")})
            return self.send(302, location=f"{REDIRECT}?{query}")
        if url.path == "/api/v4/user":
            auth = self.headers.get("Authorization", "")
            user = take(tokens, auth[7:]) if auth[:7].lower() == "bearer " else ""
            if not user:
                return self.send(401, {"message": "401 Unauthorized"})
            u = USERS[user]
            return self.send(200, {"id": u["id"], "username": user, "login": user, "email": u["email"], "name": u["name"]})
        self.send(404)

    def do_POST(self) -> None:
        if urllib.parse.urlsplit(self.path).path != "/oauth/token":
            return self.send(404)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()))
        cid, secret = form.get("client_id", ""), form.get("client_secret", "")
        if (auth := self.headers.get("Authorization", "")).lower().startswith("basic "):
            cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            cid, secret = urllib.parse.unquote_plus(cid), urllib.parse.unquote_plus(secret)
        if cid != CLIENT_ID or not secrets.compare_digest(secret, SECRET):
            return self.send(401, {"error": "invalid_client"})
        user = take(codes, form.get("code", ""))
        if not user:
            return self.send(400, {"error": "invalid_grant"})
        token = secrets.token_urlsafe(32)
        tokens[token] = (user, time.monotonic())
        self.send(200, {"access_token": token, "token_type": "bearer"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8070), Handler).serve_forever()
