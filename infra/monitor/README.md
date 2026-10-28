# Monitor and Proxmox UI on the rented box: `https://monitor.ludius.se`, `https://pve.ludius.se`

The monitor shows live desktops of the persona VMs and the Proxmox UI controls the box, so neither is reachable without login. Both reuse the homelab's edge (Caddy + Claustra SSO) and a tailnet; no Cloudflare Tunnel or Access. The Proxmox UI has the same path as the monitor, without the Host-rewrite hop, and keeps its own login on top of Claustra.

```
browser --https--> Cloudflare DNS -> homelab Caddy  monitor.ludius.se
                                       import claustra_forward   (Claustra login, forward_auth)
                                       -> 127.0.0.1:18452  (loopback hop, rewrites Host to 127.0.0.1:7452)
                                       -> <box tailnet ip>:8452   (tailnet)
                    box: tailscale serve --http=8452  ->  127.0.0.1:7452  (host agent, monitor endpoint)

browser --https--> Cloudflare DNS -> homelab Caddy  pve.ludius.se
                                       import claustra_forward http://<box tailnet ip>:8453   (Claustra login, then tailnet)
                    box: tailscale serve --http=8453  ->  https+insecure://127.0.0.1:8006  (pveproxy, self-signed TLS)
```

- The agent stays on `127.0.0.1:7452`. `tailscale serve` listens on tailnet addresses only. The box opens no inbound port for the monitor.
- **pveproxy is moved off the public address.** Today it listens on `0.0.0.0:8006`. The `proxmox_host` role (`tasks/pve-exposure.yml`, included before and after `proxmox-ve` is installed, so its first start is already on loopback) writes `LISTEN_IP="127.0.0.1"` to `/etc/default/pveproxy`. `bring-up.sh` does the same on a box that was set up another way (restart of pveproxy, which ends open web sessions). The Packer build and the host agent already use `127.0.0.1:8006`.
- **Enterprise apt sources are switched off.** Installing `proxmox-ve` adds `pve-enterprise.sources` (and a Ceph enterprise source if Ceph is installed) pointing at `enterprise.proxmox.com`, which answers 401 without a subscription and breaks every later `apt` run. `tasks/pve-repos.yml` renames any apt source mentioning that host to `*.disabled` right after the install (kept for a later subscription; the no-subscription repository stays).
- **What else listens after a PVE install, and what was chosen.** pveproxy 8006 and pvedaemon 85 (loopback by default) are covered above; postfix 25 is already "Local only" in the role; sshd 22 stays; chrony and dnsmasq (`port=0`) have no TCP listener. spiceproxy 3128 is **masked** (SPICE is not used; noVNC and xterm.js go through pveproxy; masking does not depend on spiceproxy honouring `LISTEN_IP`, which is unconfirmed). rpcbind 111 (service and socket; with it rpc.statd) is **masked**: it comes with nfs-common and no NFS storage is used. **No host firewall was added:** the box has no console, a firewall mistake locks the operator out, and it would sit beside the range bridges' iptables FORWARD rules. Instead `check.sh` fails on any TCP listener on a public address other than 22 (`PUBLIC_TCP_ALLOW`), so a listener this list missed shows up on box day, and each public address is probed on the Proxmox ports too.
- Persona VMs stay on `vmbr1` (no uplink, no forwarding). Tailscale runs on the host only and advertises no routes or exit node.
- Why the extra Caddy hop: the agent answers only `Host: 127.0.0.1:7452` (`RequireHost("*:7452")` and `AllowedHosts`), and `claustra_forward` takes only an upstream argument, so `header_up` cannot go inside it. The first block keeps the real `Host` for Claustra; the second rewrites it for the agent. `Origin` is untouched, so the agent's WebSocket origin check needs `Monitor__AllowedOrigins=https://monitor.ludius.se` (`d56d297`; exact origins, `;`-separated, set outside `appsettings.json`; `host/agent` is not edited here).

## Inputs (box day)

| Input | Where |
| --- | --- |
| Tailscale auth key: tagged `tag:ludius-box`, pre-authorised, ephemeral, short expiry | `TS_AUTHKEY` env on the box, never committed |
| Tailnet policy (applied, see below) | Tailscale admin console |
| Proxmox admin account in the PVE realm and its password, **typed by the owner on the box, never stored in the repo or passed on a command line** (below) | `pveum` on the box |
| The box's tailnet IP after bring-up (`tailscale ip -4` on the box) | `BOX_IP` for the homelab steps |
| Agent environment: `Monitor__Root=<checkout>/web/monitor/dist` and `Monitor__AllowedOrigins=https://monitor.ludius.se` | the agent's service unit (printed by `bring-up.sh`) |
| Homelab: Caddy container name, the Cloudflare token that covers `ludius.se` | owner / orchestrator |

Optional overrides: `TS_HOSTNAME` (default `ludius-box`), `TS_PORT` (`8452`), `PVE_TS_PORT` (`8453`), `PVE_TARGET` (`https+insecure://127.0.0.1:8006`), `BUN_VERSION` (`1.3.14`).

### Tailnet policy (applied via the API; this section follows it)

Tailscale policy is deny-by-default and has no deny rule: access exists only if some rule grants it, in whichever direction. A rented box must not reach the owner's devices, and the owner's other devices must not reach the box's ports directly (that would bypass Claustra). So the **only** rule touching `tag:ludius-box` is the last grant below. Syntax: [grants](https://tailscale.com/kb/1324/grants), [policy syntax](https://tailscale.com/kb/1337/policy-syntax) for `hosts`, `autogroup:member` and `tests`.

As applied on 2026-10-28 (the live policy file is authoritative; the owner's `ssh` check rule and member `funnel` nodeAttr are kept and not shown):

```json
"tagOwners": { "tag:artifex-worker": ["<owner login email>"], "tag:ludius-box": ["<owner login email>"] },
"hosts":     { "m0-homelab-cl": "100.101.88.26" },
"grants": [
  { "src": ["autogroup:member"],   "dst": ["autogroup:member", "tag:artifex-worker"], "ip": ["*"] },
  { "src": ["tag:artifex-worker"], "dst": ["autogroup:member", "tag:artifex-worker"], "ip": ["*"] },
  { "src": ["tag:artifex-worker"], "dst": ["100.101.88.26"], "ip": ["tcp:8443"] },
  { "src": ["m0-homelab-cl"],      "dst": ["tag:ludius-box"], "ip": ["tcp:8452", "tcp:8453"] }
],
"tests": [
  { "src": "m0-homelab-cl",       "accept": ["tag:ludius-box:8452", "tag:ludius-box:8453"], "deny": ["tag:ludius-box:22", "tag:ludius-box:8006"] },
  { "src": "<owner login email>", "deny":   ["tag:ludius-box:8452", "tag:ludius-box:8453", "tag:ludius-box:22"] },
  { "src": "tag:ludius-box",      "deny":   ["m0-homelab-cl:443", "m0-homelab-cl:22"] }
]
```

Why this holds: `autogroup:member` is the user-owned devices and, unlike `*`, not tagged ones, so the two broad grants reach members and `tag:artifex-worker` but never `tag:ludius-box`, in either direction; `tag:ludius-box` is a source nowhere, so the box can open no connection into the tailnet (its replies to the homelab are not new connections). `tag:artifex-worker` is another project's tag and is untouched here. If the policy is ever edited again: no rule may have `*`, `autogroup:tagged`, `autogroup:danger-all` or `tag:ludius-box` in `src` or `dst` apart from the last grant, and there must be no `autoApprovers` entry, `ssh` rule or `nodeAttrs` funnel for the tag.

The `tests` block is the proof at save time: Tailscale rejects a policy file where any assertion fails. The box has joined (hostname `ludius-box`, `100.119.100.45`, tagged), so the `tag:` tests are live.

**Prove it on real traffic** (BOX_IP is `100.119.100.45` for this rental; `tailscale ip -4` on the box otherwise):

- From any device that is not the homelab (the owner's laptop or phone): `curl -m 5 -sS http://BOX_IP:8452/api/vms` must **time out** (curl exit 28), not answer and not refuse. Repeat for ports 8453, 7452, 8006 and 22.
- From the homelab: the same `curl` answers (the Verify step below).
- From the box: `timeout 5 bash -c 'exec 3<>/dev/tcp/100.101.88.26/22'` and the same against each of the owner's other tailnet IPs must fail. (Only meaningful if the target listens on that port; the `tests` block is the authority.)
- Do not use `tailscale ping` as the proof: a ping can answer while the policy denies TCP (unconfirmed, so not relied on).

## Order of commands

1. **Desktop, before box day:** run the Claustra precheck (below). The tailnet policy is applied (above); create the tagged auth key if the box is not enrolled yet.
2. **Box** (root, in the repo checkout):
   ```sh
   TS_AUTHKEY=tskey-auth-... infra/monitor/bring-up.sh
   ```
   Installs Tailscale from Tailscale's apt repo (and `jq`) unless it is already there (this box has 1.104.4 from pkgs.tailscale.com), joins the tailnet unless it is already enrolled (then `TS_AUTHKEY` is not needed), then **verifies the box's identity: tagged `tag:ludius-box` and nothing else, no user owner, no advertised routes or exit node, no exit node in use.** If the box is already enrolled with anything else it aborts before `serve` and tells you to `tailscale logout` and start over. Only then it runs `tailscale serve --bg --http=8452 http://127.0.0.1:7452`, installs Bun (pinned, sha256-checked against the release's `SHASUMS256.txt`), builds `web/monitor`, and runs `check.sh`. Before `serve` it also binds pveproxy to loopback and masks spiceproxy and rpcbind (see above), and it publishes a second mapping, `tailscale serve --bg --http=8453 https+insecure://127.0.0.1:8006`. `DRY_RUN=1` prints the mutating commands only (the identity check is read-only and still runs on an enrolled machine).
3. **Box:** install and start the agent and the Host-fixing Caddy: `infra/box/install.sh` (systemd unit `ludius-agent`, `/opt/ludius-agent`, `Monitor__Root` and `Monitor__AllowedOrigins` set in the unit). Then point serve at the Caddy: `AGENT_MONITOR=http://127.0.0.1:7453 infra/monitor/bring-up.sh` (see "Fallback if `tailscale serve` rewrites Host"; this is the box's real path).
4. **Homelab** (the orchestrator applies after the owner's OK; nothing here is applied by the worker):
   ```sh
   scp infra/monitor/homelab/monitor.caddy root@192.168.0.67:/tmp/monitor.caddy
   ssh root@192.168.0.67
   cd /srv/compose/edge
   BOX_IP=<tailnet ip>; BOX_DNS=<box MagicDNS name, e.g. ludius-box.tailec5e26.ts.net>; TS_PORT=8452
   grep -q 'BEGIN ludius-monitor' Caddyfile && echo "already applied" && exit
   [ -e Caddyfile.bak-before-ludius-monitor ] || cp -a Caddyfile Caddyfile.bak-before-ludius-monitor
   sed -e "s/__BOX_TAILNET_IP__/$BOX_IP/" -e "s/__BOX_DNS_NAME__/$BOX_DNS/" -e "s/__TS_PORT__/$TS_PORT/g" /tmp/monitor.caddy >> Caddyfile
   ss -ltn | grep -w 18452 && echo "port 18452 is taken, pick another in the snippet"
   docker compose exec <caddy-service> caddy validate --config /etc/caddy/Caddyfile   # service and path unconfirmed
   ```
   If validate fails: `cp -a Caddyfile.bak-before-ludius-monitor Caddyfile`. Otherwise reload:
   ```sh
   docker compose exec <caddy-service> caddy reload --config /etc/caddy/Caddyfile
   ```
   The Proxmox site is its own marked block, applied the same way (own backup name, own marker pair):
   ```sh
   scp infra/monitor/homelab/pve.caddy root@192.168.0.67:/tmp/pve.caddy     # from the desktop, before ssh
   cd /srv/compose/edge
   BOX_IP=<tailnet ip>; BOX_DNS=<box MagicDNS name>; PVE_TS_PORT=8453
   grep -q 'BEGIN ludius-pve' Caddyfile && echo "already applied" && exit
   [ -e Caddyfile.bak-before-ludius-pve ] || cp -a Caddyfile Caddyfile.bak-before-ludius-pve
   sed -e "s/__BOX_TAILNET_IP__/$BOX_IP/" -e "s/__BOX_DNS_NAME__/$BOX_DNS/" -e "s/__PVE_TS_PORT__/$PVE_TS_PORT/g" /tmp/pve.caddy >> Caddyfile
   docker compose exec <caddy-service> caddy validate --config /etc/caddy/Caddyfile
   ```
   On a validate failure `cp -a Caddyfile.bak-before-ludius-pve Caddyfile`, otherwise reload as above. WebSockets (noVNC console, xterm.js shell) need no extra Caddy config: `reverse_proxy` upgrades them, and `tailscale serve` proxies upgrades as well (unconfirmed for the installed version; the browser console test in "Verify" settles it).
   **Host header (seen on the box, Tailscale 1.104.4):** `tailscale serve` answers only the box's MagicDNS name as Host (`<ip>:8453` gets a 404) and passes that Host to the backend unchanged. Hence the loopback hops set `Host <BOX_DNS>:<port>`; pveproxy ignores Host, but the agent answers only `127.0.0.1:7452`, so the monitor needs the box-side Caddy fallback below (`AGENT_MONITOR=http://127.0.0.1:7453`).

   **Claustra:** each host must be registered as a forward-auth relying party, or Claustra answers `403 unknown relying party`. Same pattern as `ark.marcusson.dev` in `nixos-infra/modules/claustra.nix` (copy agh's owner allowlist), run on the homelab:
   ```sh
   sudo -u claustra psql --set=ON_ERROR_STOP=1 --dbname=claustra <<'SQL'
   INSERT INTO forward_hosts (host, name, enabled, access_policy, created_by)
   SELECT 'pve.ludius.se', 'Ludius Proxmox (rented box)', true, 'allowlist', created_by FROM forward_hosts WHERE host = 'agh.marcusson.dev'
   ON CONFLICT (host) DO UPDATE SET name = EXCLUDED.name, enabled = true, access_policy = 'allowlist';
   INSERT INTO forward_allowed_emails (rp_host, normalized_email, added_by)
   SELECT 'pve.ludius.se', normalized_email, added_by FROM forward_allowed_emails WHERE rp_host = 'agh.marcusson.dev'
   ON CONFLICT DO NOTHING;
   SQL
   ```
   (`monitor.ludius.se` the same way.) Teardown: `DELETE FROM forward_allowed_emails WHERE rp_host = '<host>'; DELETE FROM forward_hosts WHERE host = '<host>';`
5. **Homelab DNS:** `grep -rn DOMAINS /srv/compose/edge` finds the `favonia/cloudflare-ddns` containers. Back up the file first (`cp -a compose.yaml compose.yaml.bak-before-ludius-monitor`, matching the Caddyfile pattern). Add `monitor.ludius.se` and `pve.ludius.se` to the `DOMAINS` of a container whose sops-sealed token covers the `ludius.se` zone (the list is comma-separated: `DOMAINS=monitor.ludius.se,pve.ludius.se`), then `docker compose up -d <that-ddns-service>`. If no such container exists (none for `ludius.se` yet), copy an existing ddns service, give it its own sealed token and `DOMAINS=monitor.ludius.se,pve.ludius.se`. Follow the existing containers' `PROXIED` setting.

### Proxmox admin account (box, once, before the UI is used)

`root` has no password (installimage disabled it), and root@pam is not how the UI is used here. Create a PVE-realm administrator on the box over SSH, **typing the password at the prompt** so it is never in a file, the shell history or the repo:

```sh
pveum user add <name>@pve
pveum passwd <name>@pve                                   # prompts twice; type it, do not pass it as an argument
pveum acl modify / --users <name>@pve --roles Administrator
```

Log in at `https://pve.ludius.se` with realm "Proxmox VE authentication server" after the Claustra login. The PVE login stays on as a second layer: Claustra proves who reached the page, PVE proves who may operate the box. TOTP is optional and recommended for a login that reaches a hypervisor: Datacenter > Permissions > Two Factor > Add > TOTP, as `<name>@pve`. Remove the account (`pveum user delete <name>@pve`) at teardown if the box is kept.

### Claustra precheck (before box day, needs no box)

Claustra is only known here through the pattern `import claustra_forward <upstream>`. Check cross-domain login before relying on it. Add a throwaway block, validate, reload, and open it in a browser:

```
monitor.ludius.se {
	import claustra_forward http://127.0.0.1:9   # nothing listens; a 502 after login is fine
}
```

Logged out you must land on the Claustra login; after login you must get a 502 from the dead upstream, not the login again (a loop means the SSO cookie does not carry to `ludius.se`). Back the Caddyfile up before adding the block (`cp -a Caddyfile Caddyfile.bak-before-ludius-monitor-precheck`) and put it back afterwards (`cp -a Caddyfile.bak-before-ludius-monitor-precheck Caddyfile`, then validate and reload). The throwaway block carries no markers, so `remove-monitor.sh` does not remove it.

## Verify

On the box: `infra/monitor/check.sh` must exit 0. It fails closed: if `ss`, `ip`, `tailscale` or `jq` cannot answer it exits 2 (abort), never "nothing found, all good". It checks (1) from `ss -ltn`, IPv4 and IPv6, that ports 7452, 7453, 8452, 8453 and the Proxmox ports 8006 (pveproxy), 3128 (spiceproxy) and 85 (pvedaemon) listen on loopback only (a wildcard or global-address listener on any of them fails), and that no other TCP listener sits on a public address except `PUBLIC_TCP_ALLOW` (default `22`; rpcbind 111 or postfix 25 would fail here); (2) that a TCP connect to each of those ports on every global address of the box, IPv6 included, is refused or times out (`tailscale0` excluded); (3) from `tailscale serve status --json` that serve holds exactly two mappings, `:8452 -> http://127.0.0.1:7452` and `:8453 -> https+insecure://127.0.0.1:8006`, each at `/` only, with no other port, no Services, no Foreground session and no funnel. (`ss` never lists the tailnet port because `tailscale serve` listens inside tailscaled; the connect probes cover it.)

`infra/monitor/test.sh` runs the same script, the bring-up identity gate and pveproxy binding, and `teardown.sh` against stubbed `ss`, `ip`, `tailscale` and `systemctl`, and the homelab removal script against sample Caddyfiles (needs `jq` and `python3`; run it before box day).

From the homelab, over the tailnet, with the agent running:

```sh
curl -fsS -H 'Host: 127.0.0.1:7452' http://<BOX_IP>:8452/api/vms
```

From anywhere:

- Logged out, `curl -sI https://monitor.ludius.se/` and `curl -sI https://monitor.ludius.se/api/vms` give a redirect to Claustra (or 401), never the app or JSON. A WebSocket upgrade to `/api/vms/<name>/stream` without the session cookie is refused too.
- In a browser: Claustra login page when logged out; after login the monitor loads and the VM frames stream.
- Optional negative checks: `http://<public ip>:8452`, `:8453`, `:7452` and `https://<public ip>:8006` refuse (also covered by `check.sh`).

Proxmox UI:

- Logged out, `curl -sI https://pve.ludius.se/` gives a redirect to Claustra (or 401), never the PVE login page; `curl -sI https://pve.ludius.se/api2/json/version` the same.
- From the homelab over the tailnet: `curl -s -o /dev/null -w '%{http_code}' http://<BOX_IP>:8453/` answers 200 (the PVE login page), while `https://<public ip>:8006` from anywhere is refused.
- In a browser after the Claustra login: the PVE login page, then a login with the account above. Open a VM console (noVNC) and Node > Shell (xterm.js): both must stay connected; a console that opens and drops at once means the WebSocket upgrade is not passing one of the hops.

## Teardown (end of rental)

Box:

```sh
infra/monitor/teardown.sh     # tailscale serve --http=8452 off; --http=8453 off; tailscale logout
```

Delete the machine in the Tailscale admin console if listed, and revoke the auth key.

Homelab:

```sh
scp infra/monitor/homelab/remove-monitor.sh root@192.168.0.67:/tmp/
ssh root@192.168.0.67
export CADDY='docker compose -f /srv/compose/edge/compose.yaml exec -T <caddy-service> caddy'   # service name unconfirmed
/tmp/remove-monitor.sh                      # the monitor block
BLOCK=ludius-pve /tmp/remove-monitor.sh     # the Proxmox block
```

The script refuses unless the Caddyfile has exactly one `# BEGIN ludius-monitor` and one `# END ludius-monitor` line, BEGIN first (otherwise it says so and leaves the file alone; `DRY_RUN=1` shows the lines it would remove). `BLOCK` names the marker pair (`ludius-monitor` by default). It writes `Caddyfile.bak-before-<block>-teardown`, removes the block, runs `caddy validate` and, if that fails, restores the backup and stops; only a validated file is reloaded.

Remove `monitor.ludius.se` and `pve.ludius.se` from `DOMAINS` and `docker compose up -d <ddns-service>`. Removing a domain from `DOMAINS` does not delete the Cloudflare record (to my recollection of cloudflare-ddns; unconfirmed), so delete the `monitor` and `pve` records in the `ludius.se` zone in the dashboard. Remove the ddns container too if it was added only for this.

### Fallback if `tailscale serve` rewrites Host

Put a tiny Caddy on the box that sets Host, and point serve at it. Binds loopback only, so the exposure story is unchanged:

```
# infra/box/Caddyfile, installed by infra/box/install.sh (apt install caddy; systemctl disable --now the default site if it listens on :80)
http://:7453 {   # port only: a site named 127.0.0.1:7453 answers an empty 200 to the forwarded MagicDNS Host
	bind 127.0.0.1
	reverse_proxy 127.0.0.1:7452 {
		header_up Host 127.0.0.1:7452
	}
}
```

Then run `AGENT_MONITOR=http://127.0.0.1:7453 infra/monitor/bring-up.sh` (serve maps `:8452` to `:7453`; it passes the same value to `check.sh`, so run a later check as `AGENT_MONITOR=http://127.0.0.1:7453 infra/monitor/check.sh`). The check always covers 7452, 7453 and 8452, so the agent's own `127.0.0.1:7452` is verified loopback-only as well. Origin is untouched either way.

## Box-day checklist (beyond the commands above)

- [x] Tailnet policy applied with the `tests` block; box enrolled as `ludius-box` (`100.119.100.45`, tagged). [ ] The curl proofs under "Tailnet policy" pass.
- [ ] **Claustra cross-domain login:** the precheck passes on `monitor.ludius.se` (login page, then the dead-upstream 502, no login loop).
- [ ] **Claustra authorizes the owner only:** `claustra_forward` proves "logged in to Claustra", not "is the owner". Confirm in Claustra which accounts can log in and which hosts they may reach (a per-host allowlist, if it has one; not visible from the Caddyfile pattern). If any other account exists, log in as it and confirm `monitor.ludius.se` is refused before frames are ever served.
- [ ] `infra/monitor/check.sh` exits 0 on the box with the agent running and pveproxy restarted on loopback (`ss -ltn` shows `127.0.0.1:8006`, nothing on 111 or 3128).
- [ ] A PVE-realm admin exists (password typed by the owner), TOTP decided; the console and shell tests under "Verify" pass through `pve.ludius.se`.
- [ ] Cloudflare token covers `ludius.se`; TLS for `monitor.ludius.se` and `pve.ludius.se` issued; the Verify steps below pass.

## Unconfirmed for box day

- **Cloudflare token vs `ludius.se`:** whether the existing sealed token covers the `ludius.se` zone. No `ludius.se` domain is in the edge yet. If not, a new token (Zone › DNS edit, `ludius.se` only) and a new ddns container are needed.
- **TLS issuance for `ludius.se`:** if the edge's Caddy gets certificates by DNS-01 with a marcusson.dev-scoped token, the new host needs the same for `ludius.se` (or HTTP-01 reachable on 80/443).
- **Claustra on a second registrable domain:** an SSO cookie scoped to `marcusson.dev` is not sent to `monitor.ludius.se`. Whether Claustra supports a second cookie domain, and whether it needs a client or allowlist entry per host (`agh.marcusson.dev` only imports the snippet, which suggests none), is not visible from the Caddyfile pattern. The precheck above answers it. Fallback: a `*.marcusson.dev` name.
- **Caddy runs on the host network:** the snippet reaches Claustra at `127.0.0.1:13002`, which implies it, so the loopback hop at `127.0.0.1:18452` and the dial to the box's `100.x` address should work. A containerised Caddy without host networking would not reach the tailnet; `docker inspect` the edge container.
- **`tailscale serve` and `Host`:** serve's reverse proxy sets `Out.Host = In.Host` for TCP upstreams (`ipn/ipnlocal/serve.go` on tailscale's `main`; read, not run), so the `Host: 127.0.0.1:7452` Caddy sends arrives at the agent unchanged. The installed version is not confirmed: the homelab `curl` in "Verify" decides, and a 400 or 404 there means Host was rewritten. Fallback below. Serve may also print a link to enable the feature for the tailnet once.
- **`claustra_forward` and `header_up`:** the snippet is defined only on the homelab, so whether it can pass extra proxy options is unconfirmed (`grep -n -B2 -A12 claustra_forward /srv/compose/edge/Caddyfile` shows it). The design does not depend on it: the Host rewrite sits in the second block, after Claustra has seen the real Host.
- **Proxmox specifics:** that `tailscale serve` accepts `https+insecure://127.0.0.1:8006` with `--http=8453` and prints it in `serve status --json` as that exact `Proxy` string (check.sh requires it; if the string differs the check fails closed and shows the JSON). That pveproxy, which ignores `Host`, works through two proxies, and that noVNC and xterm.js upgrades pass Claustra's `forward_auth` (the same subrequest pattern as the monitor's stream). That `LISTEN_IP` in `/etc/default/pveproxy` is honoured by the installed pveproxy (documented in pveproxy(8)) and what else a stock install listens on beyond the list above; the check's public-listener rule is the net for the latter.
- **Already-enrolled box:** `bring-up.sh` takes the "already up" path on this box and must pass the identity gate (`tailscale status --json` and `tailscale debug prefs` field shapes of 1.104.4, see the JSON shapes bullet); the gate aborts rather than passes if a field differs. Not yet run on the box.
- **`tailscale up` flags and key format:** written for a current Tailscale from the stable Debian trixie repo; the key goes on the command line.
- **JSON shapes read by the scripts:** `tailscale serve status --json` (`TCP`, `Web`, `Handlers`, `Proxy`, `AllowFunnel`, `Services`), `tailscale status --json` (`Self.Tags`, `Self.UserID`, `User[<id>].LoginName` of `tagged-devices` for tagged nodes) and `tailscale debug prefs` (`AdvertiseRoutes`, `ExitNodeID`, `ExitNodeIP`) follow Tailscale's source as I remember it and were exercised only against stubs of that shape. If a real one differs the scripts fail closed (check.sh prints the JSON it saw, bring-up aborts), they do not pass; adjust the `jq` filter then.
- **Policy features:** that `autogroup:member` excludes tagged devices and that `tests` accept `tag:ludius-box` before any device holds it are read from the docs, not tried; the saved-policy tests are what settles them.

## Validation done (static)

`shellcheck` on all scripts, `infra/monitor/test.sh` (stubbed `ss`/`ip`/`tailscale`/`systemctl`, a throwaway listener on 127.0.0.1 and ::1, sample Caddyfiles), `DRY_RUN=1` of `teardown.sh`, and for the Ansible role `yamllint` and `ansible-playbook --syntax-check proxmox-host.yml` in a `python:3.13` container (ansible-core does not run on a Windows control node); `pve-repos.yml` was also run in that container against sample apt source files (enterprise `.sources` and `.list` renamed, the no-subscription file untouched, second run changed nothing). The rest of the role was not executed. The Caddy snippet was not run through `caddy validate` (no Caddy here, and the `claustra_forward` snippet is defined only on the homelab).
