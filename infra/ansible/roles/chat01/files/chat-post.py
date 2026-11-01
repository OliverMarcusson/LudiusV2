#!/usr/bin/python3
"""chat-post USER OTHER < message: post as USER (a narrative user without a VM, e.g. a manager) in the direct chat with
OTHER through the Mattermost API on loopback. USER's access token is made once (mmctl --local) and kept root-only here.
Prints the post id; exits non-zero on any failure."""

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

user, other = sys.argv[1:3]
if not all(re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", u) for u in (user, other)) or user == other:
    sys.exit("bad user")
message = sys.stdin.read().strip()
if not message:
    sys.exit("empty message")
store = Path("/etc/ludius-chat/tokens")
store.mkdir(mode=0o700, exist_ok=True)
f = store / user
if not f.exists():
    out = subprocess.run(["runuser", "-u", "mattermost", "--", "/opt/mattermost/bin/mmctl", "--local", "--json", "token", "generate", user, "ludius chat-post"],
                         check=True, capture_output=True, text=True, timeout=30).stdout
    token = json.loads(out)
    token = token[0] if isinstance(token, list) else token
    f.write_text(token["token"])
    f.chmod(0o600)


def api(path: str, body: object) -> dict | list:
    req = urllib.request.Request(f"http://127.0.0.1:8065/api/v4{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {f.read_text().strip()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


ids = {u["username"]: u["id"] for u in api("/users/usernames", [user, other])}
channel = api("/channels/direct", [ids[user], ids[other]])
print(api("/posts", {"channel_id": channel["id"], "message": message})["id"])
