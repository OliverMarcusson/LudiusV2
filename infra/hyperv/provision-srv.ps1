# Provisions the range server made by new-srv.ps1 over SSH. Samba AD DC: forest corp.ludius.test (NetBIOS CORP)
# with Samba's internal DNS, bound to the range address only (nothing on the Default Switch NIC), forwarding other
# names to the server's upstream resolver; chrony as the range's authoritative NTP source on its own clock (no
# upstream, MS-SNTP signing for members), so time-jump sets range time here first; one AD user per narrative person.
# Passwords are deliberately simple (owner's request, isolated range): Administrator Admin2026, each person
# <user>2026, with complexity off; the same for the grommunio mailboxes (AAPI admin: admin / Admin2026). grommunio
# section below. Re-runnable: existing domain, database and certificates are kept, users are created or reset.
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_SRV_VM
if ($vm -notmatch '^ludius-[A-Za-z0-9-]+$') { throw "no range server in .env (run new-srv.ps1)" }
$key = Join-Path $PSScriptRoot 'build\srv\id_ed25519'
$known = Join-Path $PSScriptRoot 'build\srv\known_hosts'
$ip = (Get-VMNetworkAdapter -VMName $vm | Where-Object SwitchName -eq 'Default Switch').IPAddresses | Where-Object { $_ -match '^\d+\.' } | Select-Object -First 1
if (-not $ip) { throw "$vm reports no Default Switch address (not running?)" }

$people = @()
$name = $null
foreach ($line in Get-Content (Join-Path $root 'runs\world\narrative.yaml')) {
    if ($line -match '^- name:\s*(.+?)\s*$') { $name = $Matches[1] }
    elseif ($name -and $line -match '^\s+user:\s*([a-z0-9]+)\s*$') { $people += "$($Matches[1])|$name"; $name = $null }
}
if ($people.Count -lt 2) { throw 'no people found in runs\world\narrative.yaml' }
# Everything substituted into the root script is checked here; the bash side also reads it only as heredoc data.
$userRe = '^[a-z][a-z0-9._-]*$'
foreach ($p in $people) {
    $u, $n = $p -split '\|', 2
    if ($u -cnotmatch $userRe -or $n -notmatch "^\p{L}[\p{L}\p{M} .'-]*$") { throw "unsafe narrative person: $p" }
}
$personas = @((Get-ChildItem (Join-Path $root 'controller\data\personas') -Filter *.yaml).BaseName)
foreach ($p in $personas) { if ($p -cnotmatch $userRe) { throw "unsafe persona name: $p" } }
$addr = $cfg.LUDIUS_SRV_ADDRESS
if (-not ($addr -match '^\d{1,3}(\.\d{1,3}){3}$' -and [Net.IPAddress]::TryParse($addr, [ref]$null))) { throw "bad LUDIUS_SRV_ADDRESS: $addr" }

$script = @'
set -euo pipefail
ADDR=__ADDR__
export DEBIAN_FRONTEND=noninteractive
if ! command -v samba-tool >/dev/null; then
  echo 'krb5-config krb5-config/default_realm string CORP.LUDIUS.TEST' | debconf-set-selections
  apt-get -qq update
  apt-get -qq install -y samba-ad-dc samba-ad-provision krb5-user winbind libnss-winbind libpam-winbind chrony dnsutils ldb-tools >/dev/null
fi
if [ ! -f /var/lib/samba/private/sam.ldb ]; then
  UPSTREAM=$(resolvectl dns 2>/dev/null | awk '/eth0|Link 2/ {print $NF; exit}')
  UPSTREAM=${UPSTREAM:-$(awk '/^nameserver/ && $2 !~ /^127\./ {print $2; exit}' /etc/resolv.conf)}
  systemctl disable --now smbd nmbd winbind 2>/dev/null || true
  rm -f /etc/samba/smb.conf
  samba-tool domain provision --realm=CORP.LUDIUS.TEST --domain=CORP --server-role=dc --dns-backend=SAMBA_INTERNAL \
    --use-rfc2307 --adminpass='Admin2026' --host-ip="$ADDR" \
    --option="interfaces=lo $ADDR" --option="bind interfaces only=yes" --option="dns forwarder=$UPSTREAM" \
    --option="ntp signd socket directory=/var/lib/samba/ntp_signd" >/dev/null
  cp /var/lib/samba/private/krb5.conf /etc/krb5.conf
  systemctl unmask samba-ad-dc
  systemctl enable --now samba-ad-dc
fi
# The server's own lookups of the range domain go to Samba (kinit, grommunio), everything else upstream as before.
mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]
DNS=127.0.0.1
Domains=~corp.ludius.test
' >/etc/systemd/resolved.conf.d/samba.conf
systemctl restart systemd-resolved
samba-tool domain passwordsettings set --complexity=off --min-pwd-length=6 --max-pwd-age=0 --history-length=0 >/dev/null

# Range NTP: own clock only (set directly by time-jump), served to the range with Samba's MS-SNTP signing.
install -d -o root -g _chrony -m 0750 /var/lib/samba/ntp_signd
cat >/etc/chrony/chrony.conf <<EOF
driftfile /var/lib/chrony/chrony.drift
local stratum 10
allow 10.77.0.0/24
bindaddress $ADDR
ntpsigndsocket /var/lib/samba/ntp_signd
EOF
systemctl restart chrony

while IFS='|' read -r user full; do
  [ -n "$user" ] || continue
  given=${full%% *}; sur=${full#* }
  if samba-tool user show "$user" >/dev/null 2>&1; then
    samba-tool user setpassword "$user" --newpassword="${user}2026" >/dev/null
  else
    samba-tool user create "$user" "${user}2026" --given-name="$given" --surname="$sur" \
      --mail-address="$user@corp.ludius.test" >/dev/null
  fi
  samba-tool user setexpiry "$user" --noexpiry >/dev/null
done <<'PEOPLE'
__PEOPLE__
PEOPLE

# grommunio core (Exchange protocols: Outlook MAPI/HTTP + Autodiscover over HTTPS via nginx). Mail stays inside the
# range domain: gromox's own delivery agent on IPv6 loopback, no Postfix, no relay. TLS from a small range CA
# (/etc/grommunio-common/ssl/ca.crt, to be trusted by the range clients).
if [ ! -f /etc/apt/sources.list.d/grommunio.sources ]; then
  curl -fsSL https://download.grommunio.com/RPM-GPG-KEY-grommunio | gpg --dearmor --yes -o /usr/share/keyrings/download.grommunio.com.gpg
  printf 'Types: deb\nURIs: https://download.grommunio.com/community/Debian_13\nSuites: Debian_13\nComponents: main\nSigned-By: /usr/share/keyrings/download.grommunio.com.gpg\n' >/etc/apt/sources.list.d/grommunio.sources
  apt-get -qq update
fi
# nginx fails its first start (the VTS module and the certificate are not in place yet): finish afterwards.
apt-get -qq install -y mariadb-server nginx gromox grommunio-common grommunio-admin-api grommunio-admin-web >/dev/null 2>&1 || true
for m in /usr/share/nginx/modules-available/*.conf; do ln -sf "$m" "/etc/nginx/modules-enabled/50-$(basename "$m")"; done
rm -f /etc/nginx/sites-enabled/default
SSL=/etc/grommunio-common/ssl
install -d -m 0755 $SSL
if [ ! -f $SSL/chain.pem ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj '/O=Ludius Range/CN=Ludius Range CA' -keyout $SSL/ca.key -out $SSL/ca.crt 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj '/O=Ludius Range/CN=mail.corp.ludius.test' -keyout $SSL/server.key -out /tmp/server.csr 2>/dev/null
  printf 'subjectAltName=DNS:mail.corp.ludius.test,DNS:autodiscover.corp.ludius.test,DNS:srv01.corp.ludius.test,DNS:corp.ludius.test,IP:%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n' "$ADDR" >/tmp/ext.cnf
  openssl x509 -req -in /tmp/server.csr -CA $SSL/ca.crt -CAkey $SSL/ca.key -CAcreateserial -days 825 -sha256 -extfile /tmp/ext.cnf -out $SSL/server.crt 2>/dev/null
  cat $SSL/server.crt $SSL/ca.crt >$SSL/chain.pem
fi
chmod 0600 $SSL/ca.key; chgrp gromox $SSL/server.key; chmod 0640 $SSL/server.key
printf 'ssl_certificate %s/chain.pem;\nssl_certificate_key %s/server.key;\n' $SSL $SSL >/etc/grommunio-common/nginx/ssl_certificate.conf
ln -sf /etc/grommunio-common/nginx/ssl_certificate.conf /etc/grommunio-admin-common/nginx-ssl.conf
dpkg --configure -a >/dev/null
systemctl enable --now mariadb nginx >/dev/null 2>&1

if ! grep -q '^mysql_password' /etc/gromox/mysql_adaptor.cfg 2>/dev/null; then
  DBPW=$(openssl rand -hex 16)
  mariadb -e "CREATE DATABASE IF NOT EXISTS grommunio; CREATE USER IF NOT EXISTS 'grommunio'@'localhost' IDENTIFIED BY '$DBPW'; ALTER USER 'grommunio'@'localhost' IDENTIFIED BY '$DBPW'; GRANT ALL ON grommunio.* TO 'grommunio'@'localhost';"
  printf 'mysql_username=grommunio\nmysql_password=%s\nmysql_dbname=grommunio\nschema_upgrade=host:srv01\n' "$DBPW" >/etc/gromox/mysql_adaptor.cfg
  printf "DB:\n  host: 'localhost'\n  user: 'grommunio'\n  pass: '%s'\n  database: 'grommunio'\n" "$DBPW" >/etc/grommunio-admin-api/conf.d/database.yaml
  # The config doubles as the done marker: drop it if the schema init fails, so a rerun tries again.
  if ! gromox-dbop -C >/dev/null; then rm -f /etc/gromox/mysql_adaptor.cfg; echo 'gromox-dbop -C failed' >&2; exit 1; fi
fi
printf 'http_listen = [::]:10080\nhttp_listen_tls = [::]:10443\nlda_listen = [::1]:25\noutgoing_smtp_url = smtp://[::1]:25\n' >/etc/gromox/gromox.cfg
printf 'host_id=mail.corp.ludius.test\ndefault_domain=corp.ludius.test\nhttp_support_tls=true\nhttp_certificate_path=%s/chain.pem\nhttp_private_key_path=%s/server.key\n' $SSL $SSL >/etc/gromox/http.cfg
chown -R grommunio:gromoxcf /etc/gromox; chmod 0750 /etc/gromox; chmod 0640 /etc/gromox/*.cfg
chown grommunio:grommunio /etc/grommunio-admin-api/conf.d/database.yaml; chmod 0640 /etc/grommunio-admin-api/conf.d/database.yaml
GX='gromox-event gromox-timer gromox-http gromox-midb gromox-zcore gromox-delivery gromox-delivery-queue php8.4-fpm grommunio-admin-api'
systemctl enable $GX >/dev/null 2>&1; systemctl restart $GX nginx
grommunio-admin passwd --password 'Admin2026' >/dev/null
grommunio-admin domain list 2>/dev/null | grep -q 'corp.ludius.test' || grommunio-admin domain create -u 100 corp.ludius.test >/dev/null
# Community licence: at most 5 normal (log-in) mailboxes. Personas get those; simulated colleagues get shared
# mailboxes (status 4: real mailboxes in the address book that receive mail, but no log-in), which the licence
# does not count.
PERSONAS=' __PERSONAS__ '
while IFS='|' read -r user full; do
  [ -n "$user" ] || continue
  addr="$user@corp.ludius.test"
  case "$PERSONAS" in *" $user "*) status=0 ;; *) status=4 ;; esac
  if grommunio-admin user query username 2>/dev/null | grep -q "^$addr[[:space:]]*$"; then
    grommunio-admin user modify "$addr" --status $status >/dev/null
  else
    grommunio-admin user create "$addr" --domain corp.ludius.test --status $status --property "displayname=$full" >/dev/null
  fi
  [ $status = 0 ] && grommunio-admin passwd "$addr" --password "${user}2026" >/dev/null
done <<'PEOPLE'
__PEOPLE__
PEOPLE
for n in mail autodiscover; do
  samba-tool dns add 127.0.0.1 corp.ludius.test $n A "$ADDR" -U Administrator --password Admin2026 >/dev/null 2>&1 || true
done
samba-tool dns add 127.0.0.1 corp.ludius.test _autodiscover._tcp SRV 'mail.corp.ludius.test 443 0 0' -U Administrator --password Admin2026 >/dev/null 2>&1 || true

echo "samba $(samba -V | cut -d' ' -f2), users $(samba-tool user list | grep -vc -e Administrator -e Guest -e krbtgt)"
host -t SRV _ldap._tcp.corp.ludius.test "$ADDR" | tail -1
host -t A srv01.corp.ludius.test "$ADDR" | tail -1
echo Admin2026 | kinit Administrator@CORP.LUDIUS.TEST >/dev/null && klist | grep -m1 krbtgt
echo "grommunio mailboxes: $(grommunio-admin user query username status 2>/dev/null | awk '/@corp/ {split($2, s, "/"); n[s[1]]++} END {printf "%d log-in, %d shared", n[0], n[4]}')"
curl -s --cacert $SSL/ca.crt -o /dev/null -w 'autodiscover via nginx: %{http_code}\n' --resolve mail.corp.ludius.test:443:$ADDR https://mail.corp.ludius.test/Autodiscover/Autodiscover.xml
PASS=aberg2026 /usr/libexec/gromox/autodiscover -e aberg@corp.ludius.test -H mail.corp.ludius.test 2>&1 | grep -m3 -E 'Server|URL|DisplayName|rror' || true
ss -lntup | awk 'NR>1 && /:(25|53|88|123|389|443|445|8443) / {print $1, $5}' | sort -u | tr '\n' ' '; echo
'@
$script = $script.Replace('__PERSONAS__', ($personas -join ' ')).Replace('__ADDR__', $addr).Replace('__PEOPLE__', ($people -join "`n")).Replace("`r`n", "`n")
$script | ssh -i $key -o UserKnownHostsFile=$known -o StrictHostKeyChecking=accept-new -o BatchMode=yes "ludius@$ip" 'tr -d ''
'' | sudo bash -s'  # pwsh ends piped stdin with CRLF
if ($LASTEXITCODE) { throw "provisioning failed on $vm ($LASTEXITCODE)" }
# The domain Administrator password set above, for host scripts that join members (provision-client.ps1).
if (-not $cfg.LUDIUS_SRV_DC_ADMIN_PASSWORD) {
    $envFile = Join-Path $root '.env'
    $nl = if ((Get-Content -Raw $envFile) -notmatch '
$') { "`r`n" } else { '' }  # else it joins the last key's value
    "${nl}LUDIUS_SRV_DC_ADMIN_PASSWORD=Admin2026" | Add-Content $envFile
}
