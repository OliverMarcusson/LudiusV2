# Provisions the range server made by new-srv.ps1 over SSH. Samba AD DC part: forest corp.ludius.test (NetBIOS CORP)
# with Samba's internal DNS, bound to the range address only (nothing on the Default Switch NIC), forwarding other
# names to the server's upstream resolver; chrony as the range's authoritative NTP source on its own clock (no
# upstream, MS-SNTP signing for members), so time-jump sets range time here first; one AD user per narrative person.
# Passwords are deliberately simple (owner's request, isolated range): Administrator Admin2026, each person
# <user>2026, with complexity off. Re-runnable: an existing domain is kept, users are created or reset.
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
done <<PEOPLE
__PEOPLE__
PEOPLE

echo "samba $(samba -V | cut -d' ' -f2), users $(samba-tool user list | grep -vc -e Administrator -e Guest -e krbtgt)"
host -t SRV _ldap._tcp.corp.ludius.test "$ADDR" | tail -1
host -t A srv01.corp.ludius.test "$ADDR" | tail -1
echo Admin2026 | kinit Administrator@CORP.LUDIUS.TEST >/dev/null && klist | grep -m1 krbtgt
ss -lntup | awk 'NR>1 && /:(53|88|389|445|123) / {print $1, $5}' | sort -u | tr '\n' ' '; echo
'@
$script = $script.Replace('__ADDR__', $cfg.LUDIUS_SRV_ADDRESS).Replace('__PEOPLE__', ($people -join "`n")).Replace("`r`n", "`n")
$script | ssh -i $key -o UserKnownHostsFile=$known -o StrictHostKeyChecking=accept-new -o BatchMode=yes "ludius@$ip" 'tr -d "" | sudo bash -s'  # pwsh ends piped stdin with CRLF
if ($LASTEXITCODE) { throw "provisioning failed on $vm ($LASTEXITCODE)" }
