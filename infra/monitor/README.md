# Monitor on the rented box: `https://monitor.ludius.se`

The monitor shows live desktops of the persona VMs, so it is never reachable without login. It reuses the homelab's edge (Caddy + Claustra SSO) and a tailnet; no Cloudflare Tunnel or Access.

```
browser --https--> Cloudflare DNS -> homelab Caddy  monitor.ludius.se
                                       import claustra_forward   (Claustra login, forward_auth)
                                       -> 127.0.0.1:18452  (loopback hop, rewrites Host to 127.0.0.1:7452)
                                       -> <box tailnet ip>:8452   (tailnet)
                    box: tailscale serve --http=8452  ->  127.0.0.1:7452  (host agent, monitor endpoint)
```

- The agent stays on `127.0.0.1:7452`. `tailscale serve` listens on tailnet addresses only. The box opens no inbound port for the monitor.
- Persona VMs stay on `vmbr1` (no uplink, no forwarding). Tailscale runs on the host only and advertises no routes or exit node.
- Why the extra Caddy hop: the agent answers only `Host: 127.0.0.1:7452` (`RequireHost("*:7452")` and `AllowedHosts`), and `claustra_forward` takes only an upstream argument, so `header_up` cannot go inside it. The first block keeps the real `Host` for Claustra; the second rewrites it for the agent. `Origin` is untouched, so the agent's WebSocket origin check needs `Monitor__AllowedOrigins=https://monitor.ludius.se` (`d56d297`; exact origins, `;`-separated, set outside `appsettings.json`; `host/agent` is not edited here).

## Inputs (box day)

| Input | Where |
| --- | --- |
| Tailscale auth key: tagged `tag:ludius-box`, pre-authorised, ephemeral, short expiry | `TS_AUTHKEY` env on the box, never committed |
| Tailnet ACL entry for `tag:ludius-box` (below) | Tailscale admin console |
| The box's tailnet IP after bring-up (`tailscale ip -4` on the box) | `BOX_IP` for the homelab steps |
| Agent environment: `Monitor__Root=<checkout>/web/monitor/dist` and `Monitor__AllowedOrigins=https://monitor.ludius.se` | the agent's service unit (printed by `bring-up.sh`) |
| Homelab: Caddy container name, the Cloudflare token that covers `ludius.se` | owner / orchestrator |

Optional overrides: `TS_HOSTNAME` (default `ludius-box`), `TS_PORT` (`8452`), `BUN_VERSION` (`1.3.14`).

### Tailnet ACL (do this before the key is used)

A rented box must not reach the owner's other devices. Only the homelab may reach the box, and only on the monitor port. Adapt to the existing policy; the point is `tag:ludius-box` as a destination for the homelab and never a source:

```json
"tagOwners": { "tag:ludius-box": ["autogroup:admin"] },
"acls": [ { "action": "accept", "src": ["<homelab tag or user>"], "dst": ["tag:ludius-box:8452"] } ]
```

If the policy is the default allow-all, add a rule that denies `tag:ludius-box` as a source, or the box can reach everything on the tailnet.

## Order of commands

1. **Desktop, before box day:** run the Claustra precheck (below). Create the tagged auth key and the ACL entry.
2. **Box** (root, in the repo checkout):
   ```sh
   TS_AUTHKEY=tskey-auth-... infra/monitor/bring-up.sh
   ```
   Installs Tailscale from Tailscale's apt repo, joins the tailnet, runs `tailscale serve --bg --http=8452 http://127.0.0.1:7452`, installs Bun (pinned, sha256-checked against the release's `SHASUMS256.txt`), builds `web/monitor`, then runs `check.sh`. `DRY_RUN=1` prints the mutating commands only.
3. **Box:** start the agent with `Monitor__Root` and `Monitor__AllowedOrigins` (values printed by step 2).
4. **Homelab** (the orchestrator applies after the owner's OK; nothing here is applied by the worker):
   ```sh
   scp infra/monitor/homelab/monitor.caddy root@192.168.0.67:/tmp/monitor.caddy
   ssh root@192.168.0.67
   cd /srv/compose/edge
   BOX_IP=<tailnet ip>; TS_PORT=8452
   grep -q 'BEGIN ludius-monitor' Caddyfile && echo "already applied" && exit
   [ -e Caddyfile.bak-before-ludius-monitor ] || cp -a Caddyfile Caddyfile.bak-before-ludius-monitor
   sed -e "s/__BOX_TAILNET_IP__/$BOX_IP/" -e "s/__TS_PORT__/$TS_PORT/" /tmp/monitor.caddy >> Caddyfile
   ss -ltn | grep -w 18452 && echo "port 18452 is taken, pick another in the snippet"
   docker compose exec <caddy-service> caddy validate --config /etc/caddy/Caddyfile   # service and path unconfirmed
   ```
   If validate fails: `cp -a Caddyfile.bak-before-ludius-monitor Caddyfile`. Otherwise reload:
   ```sh
   docker compose exec <caddy-service> caddy reload --config /etc/caddy/Caddyfile
   ```
5. **Homelab DNS:** `grep -rn DOMAINS /srv/compose/edge` finds the `favonia/cloudflare-ddns` containers. Back up the file first (`cp -a compose.yaml compose.yaml.bak-before-ludius-monitor`, matching the Caddyfile pattern). Add `monitor.ludius.se` to the `DOMAINS` of a container whose sops-sealed token covers the `ludius.se` zone, then `docker compose up -d <that-ddns-service>`. If no such container exists (none for `ludius.se` yet), copy an existing ddns service, give it its own sealed token and `DOMAINS=monitor.ludius.se`. Follow the existing containers' `PROXIED` setting.

### Claustra precheck (before box day, needs no box)

Claustra is only known here through the pattern `import claustra_forward <upstream>`. Check cross-domain login before relying on it. Add a throwaway block, validate, reload, and open it in a browser:

```
monitor.ludius.se {
	import claustra_forward http://127.0.0.1:9   # nothing listens; a 502 after login is fine
}
```

Logged out you must land on the Claustra login; after login you must get a 502 from the dead upstream, not the login again (a loop means the SSO cookie does not carry to `ludius.se`). Remove the block again (same sed range as teardown).

## Verify

On the box: `infra/monitor/check.sh` must pass. It checks the monitor ports listen on loopback only, nothing answers on the public IP for 7452 or 8452, and `tailscale serve status` shows `:8452 -> 127.0.0.1:7452`. (`ss` never lists the tailnet port because `tailscale serve` listens inside tailscaled; the public-IP probes cover it.)

From the homelab, over the tailnet, with the agent running:

```sh
curl -fsS -H 'Host: 127.0.0.1:7452' http://<BOX_IP>:8452/api/vms
```

From anywhere:

- Logged out, `curl -sI https://monitor.ludius.se/` and `curl -sI https://monitor.ludius.se/api/vms` give a redirect to Claustra (or 401), never the app or JSON. A WebSocket upgrade to `/api/vms/<name>/stream` without the session cookie is refused too.
- In a browser: Claustra login page when logged out; after login the monitor loads and the VM frames stream.
- Optional negative checks: `http://<public ip>:8452` and `:7452` refuse (also covered by `check.sh`).

## Teardown (end of rental)

Box:

```sh
infra/monitor/teardown.sh     # tailscale serve --http=8452 off; tailscale logout
```

Delete the machine in the Tailscale admin console if listed, and revoke the auth key.

Homelab (back up first, same naming):

```sh
cd /srv/compose/edge
cp -a Caddyfile Caddyfile.bak-before-ludius-monitor-teardown
sed -i '/^# BEGIN ludius-monitor/,/^# END ludius-monitor/d' Caddyfile
docker compose exec <caddy-service> caddy validate --config /etc/caddy/Caddyfile
docker compose exec <caddy-service> caddy reload --config /etc/caddy/Caddyfile
```

Remove `monitor.ludius.se` from `DOMAINS` and `docker compose up -d <ddns-service>`. Removing a domain from `DOMAINS` does not delete the Cloudflare record (to my recollection of cloudflare-ddns; unconfirmed), so delete the `monitor` record in the `ludius.se` zone in the dashboard. Remove the ddns container too if it was added only for this.

### Fallback if `tailscale serve` rewrites Host

Put a tiny Caddy on the box that sets Host, and point serve at it. Binds loopback only, so the exposure story is unchanged:

```
# /etc/caddy/Caddyfile on the box (apt install caddy; systemctl disable --now the default site if it listens on :80)
http://127.0.0.1:7453 {
	bind 127.0.0.1
	reverse_proxy 127.0.0.1:7452 {
		header_up Host 127.0.0.1:7452
	}
}
```

Then run `AGENT_MONITOR=http://127.0.0.1:7453 infra/monitor/bring-up.sh` (serve maps `:8452` to `:7453`) and check with `AGENT_PORT=7453 infra/monitor/check.sh`. The agent's own `127.0.0.1:7452` stays loopback-only; `ss -ltn` confirms it. Origin is untouched either way.

## Unconfirmed for box day

- **Cloudflare token vs `ludius.se`:** whether the existing sealed token covers the `ludius.se` zone. No `ludius.se` domain is in the edge yet. If not, a new token (Zone › DNS edit, `ludius.se` only) and a new ddns container are needed.
- **TLS issuance for `ludius.se`:** if the edge's Caddy gets certificates by DNS-01 with a marcusson.dev-scoped token, the new host needs the same for `ludius.se` (or HTTP-01 reachable on 80/443).
- **Claustra on a second registrable domain:** an SSO cookie scoped to `marcusson.dev` is not sent to `monitor.ludius.se`. Whether Claustra supports a second cookie domain, and whether it needs a client or allowlist entry per host (`agh.marcusson.dev` only imports the snippet, which suggests none), is not visible from the Caddyfile pattern. The precheck above answers it. Fallback: a `*.marcusson.dev` name.
- **Caddy runs on the host network:** the snippet reaches Claustra at `127.0.0.1:13002`, which implies it, so the loopback hop at `127.0.0.1:18452` and the dial to the box's `100.x` address should work. A containerised Caddy without host networking would not reach the tailnet; `docker inspect` the edge container.
- **`tailscale serve` and `Host`:** serve's reverse proxy sets `Out.Host = In.Host` for TCP upstreams (`ipn/ipnlocal/serve.go` on tailscale's `main`; read, not run), so the `Host: 127.0.0.1:7452` Caddy sends arrives at the agent unchanged. The installed version is not confirmed: the homelab `curl` in "Verify" decides, and a 400 or 404 there means Host was rewritten. Fallback below. Serve may also print a link to enable the feature for the tailnet once.
- **`claustra_forward` and `header_up`:** the snippet is defined only on the homelab, so whether it can pass extra proxy options is unconfirmed (`grep -n -B2 -A12 claustra_forward /srv/compose/edge/Caddyfile` shows it). The design does not depend on it: the Host rewrite sits in the second block, after Claustra has seen the real Host.
- **`tailscale up` flags and key format:** written for a current Tailscale from the stable Debian trixie repo; the key goes on the command line.

## Validation done (static)

`shellcheck` on the three scripts and `DRY_RUN=1` of `bring-up.sh` and `teardown.sh`. The Caddy snippet was not run through `caddy validate` (no Caddy here, and the `claustra_forward` snippet is defined only on the homelab).
