//! `ludius setup discover`: read-only probes of a range Ludius did not build, inside the declared private subnet
//! and domain only, plus the boundary gate (docs/range-setup-design.html, Discovery). Nothing here changes the range:
//! scans, DNS and LDAP reads, anonymous share lists, one SMTP RCPT that is never followed by DATA, and the node's own
//! bridge, firewall and VM config. Run on the Proxmox node as root.

use std::io::{BufRead, BufReader, Write};
use std::net::{Ipv4Addr, TcpStream};
use std::process::Command;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};

const PORTS: &str = "25,53,80,88,389,443,445,587,993,3389,5985";

/// A private IPv4 subnet, /16 to /30.
#[derive(Debug, Clone, Copy)]
pub struct Subnet {
    net: Ipv4Addr,
    prefix: u8,
}

impl Subnet {
    pub fn parse(s: &str) -> Result<Self> {
        let (a, p) = s.split_once('/').context("subnet: a.b.c.d/prefix")?;
        let ip: Ipv4Addr = a.parse().context("subnet address")?;
        let prefix: u8 = p.parse().context("subnet prefix")?;
        ensure!((16..=30).contains(&prefix), "subnet prefix /{prefix}: /16 to /30");
        let mask = u32::MAX << (32 - prefix);
        let net = Ipv4Addr::from(u32::from(ip) & mask);
        ensure!(net == ip, "{s} is not a network address ({net}/{prefix})");
        let o = net.octets();
        let private = o[0] == 10 || (o[0] == 172 && (16..32).contains(&o[1])) || (o[0] == 192 && o[1] == 168);
        ensure!(private, "{s} is not a private range: discovery never probes public addresses");
        Ok(Self { net, prefix })
    }

    pub fn contains(&self, ip: Ipv4Addr) -> bool {
        let mask = u32::MAX << (32 - self.prefix);
        u32::from(ip) & mask == u32::from(self.net)
    }
}

impl std::fmt::Display for Subnet {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}/{}", self.net, self.prefix)
    }
}

fn domain_ok(d: &str) -> bool {
    let label = |l: &str| !l.is_empty() && l.len() <= 63 && l.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') && !l.starts_with('-');
    d.len() <= 253 && d.contains('.') && d.split('.').all(label)
}

/// stdout of a command that exited 0 (each is bounded by `timeout`); None when it failed or is missing.
fn run(argv: &[&str]) -> Option<String> {
    let out = Command::new("timeout").args(["-k", "5", "60"]).args(argv).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

#[derive(Default)]
struct Report {
    facts: Vec<Value>,
    gate: Vec<Value>,
}

impl Report {
    fn fact(&mut self, what: &str, value: impl Into<String>, evidence: impl Into<String>) {
        let (value, evidence) = (value.into(), evidence.into());
        println!("  ✔ {what}: {value}\n      {evidence}");
        self.facts.push(json!({"what": what, "value": value, "evidence": evidence}));
    }
    fn none(&mut self, what: &str, evidence: impl Into<String>) {
        let evidence = evidence.into();
        println!("  ✘ {what}: nothing found\n      {evidence}");
        self.facts.push(json!({"what": what, "value": null, "evidence": evidence}));
    }
    fn check(&mut self, what: &str, pass: bool, evidence: impl Into<String>) {
        let evidence = evidence.into();
        println!("  {} {what}\n      {evidence}", if pass { "✔" } else { "✘" });
        self.gate.push(json!({"check": what, "pass": pass, "evidence": evidence}));
    }
}

/// Live hosts and their open ports, from nmap inside the subnet only.
fn scan(subnet: Subnet) -> Result<Vec<(Ipv4Addr, Vec<u16>)>> {
    let net = subnet.to_string();
    let ping = run(&["nmap", "-n", "-sn", "-oG", "-", &net]).context("nmap ping scan failed (is nmap installed?)")?;
    let up: Vec<Ipv4Addr> = ping
        .lines()
        .filter(|l| l.contains("Status: Up"))
        .filter_map(|l| l.split_whitespace().nth(1)?.parse().ok())
        .filter(|ip| subnet.contains(*ip))
        .collect();
    if up.is_empty() {
        return Ok(vec![]);
    }
    let mut argv = vec!["nmap", "-n", "-Pn", "--open", "-p", PORTS, "-oG", "-"];
    let ips: Vec<String> = up.iter().map(Ipv4Addr::to_string).collect();
    argv.extend(ips.iter().map(String::as_str));
    let ports = run(&argv).context("nmap port scan failed")?;
    Ok(up
        .into_iter()
        .map(|ip| {
            let open = ports
                .lines()
                .find(|l| l.contains("Ports:") && l.split_whitespace().nth(1) == Some(&ip.to_string()))
                .map(|l| l.split("Ports:").nth(1).unwrap_or("").split(',').filter_map(|p| p.trim().split('/').next()?.parse().ok()).collect())
                .unwrap_or_default();
            (ip, open)
        })
        .collect())
}

fn dig(server: Ipv4Addr, kind: &str, name: &str) -> Vec<String> {
    run(&["dig", "+short", "+time=3", "+tries=1", &format!("@{server}"), kind, name])
        .map(|o| o.lines().filter(|l| !l.is_empty() && !l.starts_with(';')).map(String::from).collect())
        .unwrap_or_default()
}

/// A records of `name`, or why they can't be known. NOERROR and NXDOMAIN are answers; a timeout, SERVFAIL or
/// REFUSED is not (unreadable stays unknown).
fn resolve_a(server: Ipv4Addr, name: &str) -> std::result::Result<Vec<Ipv4Addr>, String> {
    let out = run(&["dig", "+time=3", "+tries=1", "+noall", "+comments", "+answer", &format!("@{server}"), "A", name]).ok_or_else(|| format!("no DNS answer from {server}"))?;
    let status = out.lines().find_map(|l| l.split("status: ").nth(1)).and_then(|s| s.split(',').next()).unwrap_or("?").to_string();
    match status.as_str() {
        "NXDOMAIN" => Ok(vec![]),
        "NOERROR" => Ok(out.lines().filter(|l| !l.starts_with(';')).filter_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            (f.get(3) == Some(&"A")).then(|| f.get(4)?.parse().ok()).flatten()
        }).collect()),
        s => Err(format!("DNS status {s} from {server}")),
    }
}

/// One SMTP RCPT to an address outside the range domain: the gate passes only on a 5xx refusal. Never sends DATA.
fn smtp_refuses_outside(ip: Ipv4Addr, port: u16, domain: &str) -> (bool, String) {
    let attempt = || -> Result<(bool, String)> {
        let s = TcpStream::connect_timeout(&(ip, port).into(), Duration::from_secs(5))?;
        s.set_read_timeout(Some(Duration::from_secs(10)))?;
        let mut r = BufReader::new(s.try_clone()?);
        let mut w = s;
        let mut reply = || -> Result<String> {
            loop {
                let mut line = String::new();
                ensure!(r.read_line(&mut line)? > 0, "connection closed");
                if line.as_bytes().get(3) != Some(&b'-') {
                    return Ok(line.trim_end().to_string());
                }
            }
        };
        let banner = reply()?;
        ensure!(banner.starts_with('2'), "banner {banner}");
        let mut say = |w: &mut TcpStream, cmd: &str| -> Result<String> {
            w.write_all(format!("{cmd}\r\n").as_bytes())?;
            reply()
        };
        say(&mut w, "EHLO ludius-discover")?;
        let from = say(&mut w, &format!("MAIL FROM:<ludius-probe@{domain}>"))?;
        if !from.starts_with('2') {
            let _ = say(&mut w, "QUIT");
            // the server refuses even the sender (e.g. submission without auth): nothing can be relayed out
            return Ok((from.starts_with('5'), format!("MAIL FROM refused: {from}")));
        }
        let rcpt = say(&mut w, "RCPT TO:<ludius-probe@example.com>")?;
        let _ = say(&mut w, "RSET");
        let _ = say(&mut w, "QUIT");
        Ok((rcpt.starts_with('5'), format!("RCPT TO:<ludius-probe@example.com> answered: {rcpt}")))
    };
    attempt().unwrap_or_else(|e| (false, format!("SMTP dialogue failed ({e}): can't show that outside delivery is refused")))
}

/// The node's interface with an address in the subnet: the range bridge.
fn range_bridge(subnet: Subnet) -> Option<String> {
    let out = run(&["ip", "-4", "-o", "addr", "show"])?;
    out.lines().find_map(|l| {
        let f: Vec<&str> = l.split_whitespace().collect();
        let ip: Ipv4Addr = f.get(3)?.split('/').next()?.parse().ok()?;
        subnet.contains(ip).then(|| f[1].to_string())
    })
}

/// Proxmox VMs on this node with a NIC on `bridge`: (vmid, name, status, every bridge it is on).
fn bridge_vms(bridge: &str) -> Result<Vec<(u64, String, String, Vec<String>)>> {
    let node = run(&["hostname"]).context("hostname")?.trim().to_string();
    let list: Value = serde_json::from_str(&run(&["pvesh", "get", &format!("/nodes/{node}/qemu"), "--output-format", "json"]).context("pvesh qemu list")?)?;
    let mut vms = vec![];
    for v in list.as_array().into_iter().flatten() {
        let Some(id) = v["vmid"].as_u64() else { continue };
        if v["template"].as_u64() == Some(1) {
            continue;
        }
        // a VM whose config can't be read could be multi-homed: the whole inventory is unreadable then
        let cfg: Value = serde_json::from_str(&run(&["pvesh", "get", &format!("/nodes/{node}/qemu/{id}/config"), "--output-format", "json"]).with_context(|| format!("config of VM {id} unreadable"))?)?;
        let bridges: Vec<String> = cfg
            .as_object()
            .into_iter()
            .flatten()
            .filter(|(k, _)| k.starts_with("net"))
            .filter_map(|(_, n)| n.as_str()?.split(',').find_map(|p| p.strip_prefix("bridge=")).map(String::from))
            .collect();
        if bridges.iter().any(|b| b == bridge) {
            vms.push((id, v["name"].as_str().unwrap_or("").to_string(), v["status"].as_str().unwrap_or("").to_string(), bridges));
        }
    }
    vms.sort();
    Ok(vms)
}

/// The guest's OS from the QEMU guest agent (read-only); None without an agent.
fn guest_os(id: u64) -> Option<String> {
    let v: Value = serde_json::from_str(&run(&["qm", "guest", "cmd", &id.to_string(), "get-osinfo"])?).ok()?;
    v["pretty-name"].as_str().or(v["name"].as_str()).map(String::from)
}

/// A Windows client Ludius can drive (10 or 11, not Server).
pub fn is_desktop(os: &str) -> bool {
    let o = os.to_lowercase();
    o.contains("windows") && (o.contains(" 10") || o.contains(" 11")) && !o.contains("server")
}

/// No route out of the range bridge: no physical port in it, the node doesn't route it (forwarding off, or our
/// mangle drops for both directions), and no VM on it has a NIC on another bridge.
/// The bridge's ports, or why they can't be read (not a bridge, an unreadable entry).
fn bridge_ports(bridge: &str) -> Result<Vec<String>> {
    ensure!(std::fs::metadata(format!("/sys/class/net/{bridge}/bridge"))?.is_dir(), "{bridge} is not a bridge");
    std::fs::read_dir(format!("/sys/class/net/{bridge}/brif"))?.map(|e| Ok(e?.file_name().to_string_lossy().into_owned())).collect()
}

/// A bridge port Ludius can account for: a VM NIC (tapNNNiK) or the PVE firewall's veth (fwprNNNpK) of a VM in
/// `ids`. Anything else (a physical NIC, bond, VLAN, other veth) could carry traffic out and fails the gate.
pub fn vm_port(port: &str, ids: &[u64]) -> bool {
    let id = |rest: &str, sep: char| {
        let (n, k) = rest.split_once(sep)?;
        (!k.is_empty() && k.chars().all(|c| c.is_ascii_digit())).then_some(())?;
        n.parse::<u64>().ok()
    };
    port.strip_prefix("tap").and_then(|r| id(r, 'i')).or_else(|| port.strip_prefix("fwpr").and_then(|r| id(r, 'p'))).is_some_and(|n| ids.contains(&n))
}

fn no_route_out(r: &mut Report, bridge: &str, vms: Option<&[(u64, String, String, Vec<String>)]>) {
    let Some(vms) = vms else {
        r.check("the range's VM inventory was read", false, "Proxmox VM list or a VM config unreadable: can't account for the bridge's ports or multi-homed VMs");
        return;
    };
    let ids: Vec<u64> = vms.iter().map(|v| v.0).collect();
    match bridge_ports(bridge) {
        Ok(ports) => {
            let other: Vec<&String> = ports.iter().filter(|p| !vm_port(p, &ids)).collect();
            r.check(
                &format!("{bridge} has only the NICs of its VMs"),
                other.is_empty(),
                if other.is_empty() {
                    format!("ports: {}", if ports.is_empty() { "none".into() } else { ports.join(", ") })
                } else {
                    format!("other ports (physical, bond, VLAN or veth?): {}", other.iter().map(|p| p.as_str()).collect::<Vec<_>>().join(", "))
                },
            );
        }
        Err(e) => r.check(&format!("{bridge} ports read"), false, format!("{e:#}")),
    }
    let forwarding = |f: &str| std::fs::read_to_string(f).map(|s| s.trim() == "1").unwrap_or(true);
    let v4 = forwarding("/proc/sys/net/ipv4/ip_forward");
    let v6 = forwarding("/proc/sys/net/ipv6/conf/all/forwarding");
    let dropped = |t: &str| {
        run(&[t, "-t", "mangle", "-S", "FORWARD"]).is_some_and(|s| {
            let lead: Vec<&str> = s.lines().skip(1).take_while(|l| l.ends_with(" -j DROP")).collect();
            ["-i", "-o"].iter().all(|d| lead.contains(&format!("-A FORWARD {d} {bridge} -j DROP").as_str()))
        })
    };
    let (d4, d6) = (dropped("iptables"), dropped("ip6tables"));
    let pass = (!v4 || d4) && (!v6 || d6);
    r.check(
        &format!("the node does not route {bridge}"),
        pass,
        format!("IPv4 forwarding {}, IPv6 forwarding {}, leading mangle FORWARD drops in and out: IPv4 {d4}, IPv6 {d6}", on(v4), on(v6)),
    );
    let multi: Vec<String> = vms.iter().filter(|v| v.3.iter().any(|b| b != bridge)).map(|v| format!("{} {} ({})", v.0, v.1, v.3.join(", "))).collect();
    r.check(
        &format!("no VM on {bridge} also sits on another bridge"),
        multi.is_empty(),
        if multi.is_empty() { format!("{} VMs, each only on {bridge}", vms.len()) } else { format!("multi-homed: {}", multi.join("; ")) },
    );
}

fn on(b: bool) -> &'static str {
    if b { "on" } else { "off" }
}

pub fn main(args: &[String]) -> Result<()> {
    const USAGE: &str = "usage: ludius setup discover --subnet A.B.C.D/P --domain DOMAIN [--out DIR]";
    let get = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let (Some(subnet), Some(domain)) = (get("--subnet"), get("--domain")) else { bail!(USAGE) };
    let subnet = Subnet::parse(&subnet)?;
    let domain = domain.to_lowercase();
    ensure!(domain_ok(&domain), "--domain {domain}: a DNS name like corp.example.test");
    let out = std::path::PathBuf::from(get("--out").unwrap_or_else(|| ".".into()));
    let mut r = Report::default();

    println!("discovering inside {subnet} only (domain {domain}); read-only");
    let hosts = scan(subnet)?;
    let has = |port: u16| hosts.iter().filter(move |h| h.1.contains(&port)).map(|h| h.0);
    println!("\nhosts: {}", hosts.iter().map(|(ip, p)| format!("{ip} [{}]", p.iter().map(u16::to_string).collect::<Vec<_>>().join(","))).collect::<Vec<_>>().join(", "));

    println!("\nservices:");
    let dns: Vec<Ipv4Addr> = has(53).collect();
    let (mut dcs, mut mx, mut auto_targets) = (vec![], vec![], vec![]);
    for s in &dns {
        dcs = dig(*s, "SRV", &format!("_ldap._tcp.dc._msdcs.{domain}"));
        mx = dig(*s, "MX", &domain);
        if !dcs.is_empty() || !mx.is_empty() {
            let auto = dig(*s, "SRV", &format!("_autodiscover._tcp.{domain}"));
            auto_targets = auto.iter().filter_map(|a| a.split_whitespace().nth(3)).map(String::from).collect();
            if !auto.is_empty() {
                r.fact("Autodiscover", auto.join(", "), format!("SRV _autodiscover._tcp.{domain} @{s}"));
            }
            break;
        }
    }
    if dcs.is_empty() {
        r.none("AD", format!("no SRV _ldap._tcp.dc._msdcs.{domain} from the DNS servers {dns:?}"));
    } else {
        r.fact("AD domain controllers", dcs.join(", "), format!("SRV _ldap._tcp.dc._msdcs.{domain}"));
    }
    for ip in has(389) {
        if let Some(dse) = run(&["ldapsearch", "-LLL", "-x", "-o", "nettimeout=5", "-H", &format!("ldap://{ip}"), "-s", "base", "-b", "", "defaultNamingContext", "domainFunctionality", "dnsHostName"]) {
            let attr = |k: &str| dse.lines().find_map(|l| l.strip_prefix(&format!("{k}: "))).unwrap_or("?").to_string();
            r.fact("LDAP", format!("{} ({}), domain functional level {}", attr("dnsHostName"), attr("defaultNamingContext"), attr("domainFunctionality")), format!("anonymous rootDSE on ldap://{ip}"));
        }
    }
    if mx.is_empty() {
        r.none("mail exchanger", format!("no MX for {domain}"));
    } else {
        r.fact("mail exchanger", mx.join(", "), format!("MX {domain}"));
    }
    for ip in has(443) {
        if let Some(code) = run(&["curl", "-sk", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", &format!("https://{ip}/EWS/Exchange.asmx")]).filter(|c| c == "401") {
            r.fact("EWS", format!("https://{ip}/EWS/Exchange.asmx"), format!("HTTP {code} (asks for credentials)"));
        }
    }
    for ip in has(993) {
        r.fact("IMAP", format!("{ip}:993"), "TLS port open");
    }
    for ip in has(445) {
        match run(&["smbclient", "-N", "-g", "-L", &format!("//{ip}")]) {
            Some(l) => {
                let disks: Vec<&str> = l.lines().filter_map(|x| x.strip_prefix("Disk|")?.split('|').next()).filter(|n| !n.ends_with('$')).collect();
                if disks.is_empty() {
                    r.none(&format!("shares on {ip}"), "anonymous list returned no disk shares");
                } else {
                    r.fact(&format!("shares on {ip}"), disks.join(", "), "anonymous smbclient -L");
                }
            }
            None => r.none(&format!("shares on {ip}"), "anonymous share list refused (credentials needed to see them)"),
        }
    }
    for (ip, ports) in &hosts {
        for (port, scheme) in [(80u16, "http"), (443, "https")] {
            if !ports.contains(&port) {
                continue;
            }
            // no -L: a redirect could point outside the subnet
            if let Some(page) = run(&["curl", "-sk", "--max-redirs", "0", "-m", "10", &format!("{scheme}://{ip}/")]) {
                let lower = page.to_lowercase();
                if let Some(t) = lower.find("<title>").and_then(|i| lower[i + 7..].find("</title>").map(|j| page[i + 7..i + 7 + j].trim().to_string())) {
                    r.fact(&format!("web on {ip}"), t, format!("{scheme}://{ip}/ title"));
                }
            }
        }
    }

    println!("\nhypervisor:");
    let bridge = range_bridge(subnet);
    // None: the VM inventory could not be read, which fails the gate below
    let vms: Option<Vec<_>> = match &bridge {
        Some(b) => bridge_vms(b).map_err(|e| println!("  ✘ Proxmox VMs unreadable: {e:#}")).ok(),
        None => None,
    };
    let mut desktops = vec![];
    for (id, name, status, _) in vms.iter().flatten() {
        let os = if status == "running" { guest_os(*id) } else { None };
        let kind = match &os {
            Some(o) if is_desktop(o) => {
                desktops.push(json!({"vmid": id, "name": name, "os": o}));
                "desktop"
            }
            Some(_) => "skipped (not a Windows 10/11 client)",
            None => "unknown OS (no guest agent answer, or not running)",
        };
        r.fact(&format!("VM {id} {name}"), format!("{status}, {}: {kind}", os.as_deref().unwrap_or("?")), "Proxmox config and QEMU guest agent");
    }

    println!("\nboundary gate (hard stop):");
    let smtp: Vec<(Ipv4Addr, u16)> = hosts.iter().flat_map(|(ip, p)| [25u16, 587].into_iter().filter(|x| p.contains(x)).map(move |x| (*ip, x))).collect();
    // Every host DNS names for mail must be one of them: a named mail host that is filtered or closed to the node,
    // or outside the subnet, is unknown, and unknown fails. (Desktops' default firewalls filter 25, so a filtered
    // port alone is no mail server.)
    // Without a DNS server the node can reach, the mail hosts the range's machines use are unknown.
    // ponytail: a workgroup range with no DNS in the subnet fails here too; give such ranges a declared mail host if one shows up.
    if dns.is_empty() {
        r.check(&format!("a DNS server in {subnet} answers (to learn the mail hosts of {domain})"), false, "no host with port 53 open to the node");
    }
    // MX and autodiscover targets must resolve; mail.DOMAIN is only a guess, so NXDOMAIN is fine for it.
    let mut named: Vec<(String, bool)> = mx.iter().filter_map(|m| m.split_whitespace().nth(1)).map(|n| (n.to_string(), true)).collect();
    named.extend(auto_targets.iter().map(|n| (n.clone(), true)));
    named.push((format!("mail.{domain}."), false));
    named.sort_by(|x, y| x.0.cmp(&y.0).then(y.1.cmp(&x.1))); // per name, the required entry first
    named.dedup_by(|b, a| a.0 == b.0); // keeps the first: required wins over the guess
    for (name, required) in &named {
        let addrs = match dns.first() {
            Some(s) => resolve_a(*s, name),
            None if *required => Err(format!("no DNS server in {subnet} to resolve it")),
            None => Ok(vec![]),
        };
        let addrs = match addrs.and_then(|a| if a.is_empty() && *required { Err("named for mail but has no address".to_string()) } else { Ok(a) }) {
            Ok(a) => a,
            Err(e) => {
                r.check(&format!("mail host {name} resolved"), false, format!("{e}: can't show where its mail goes"));
                continue;
            }
        };
        for a in addrs {
            if !(subnet.contains(a) && smtp.iter().any(|s| s.0 == a && s.1 == 25)) {
                let why = if subnet.contains(a) { "port 25 not open to the node: can't show it refuses outside delivery".to_string() } else { format!("{a} is outside {subnet}") };
                r.check(&format!("mail host {name} ({a}) answers SMTP inside the range"), false, why);
            }
        }
    }
    if smtp.is_empty() && r.gate.is_empty() {
        r.check("mail refuses delivery outside the range", true, format!("no SMTP server in the subnet and no mail host named for {domain}"));
    }
    for (ip, port) in smtp {
        let (pass, why) = smtp_refuses_outside(ip, port, &domain);
        r.check(&format!("{ip}:{port} refuses delivery outside {domain}"), pass, why);
    }
    match &bridge {
        Some(b) => no_route_out(&mut r, b, vms.as_deref()),
        None => r.check("no route out", false, format!("this node has no interface in {subnet}: can't inspect the range bridge (run discover on the range's Proxmox node)")),
    }

    let gate_pass = r.gate.iter().all(|c| c["pass"] == true);
    let doc = json!({
        "subnet": subnet.to_string(), "domain": domain, "at": chrono::Local::now().to_rfc3339(),
        "hosts": hosts.iter().map(|(ip, p)| json!({"ip": ip.to_string(), "open": p})).collect::<Vec<_>>(),
        "bridge": bridge, "desktops": desktops, "facts": r.facts, "gate": {"pass": gate_pass, "checks": r.gate},
    });
    std::fs::create_dir_all(&out)?;
    let file = out.join("discovery.json");
    std::fs::write(&file, serde_json::to_string_pretty(&doc)?)?;
    println!("\nwrote {}", file.display());
    ensure!(gate_pass, "boundary gate failed: Ludius does not deploy into this range until every check passes");
    println!("boundary gate passed");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subnets_and_desktops() {
        assert!(Subnet::parse("10.20.0.0/24").is_ok());
        assert!(Subnet::parse("192.168.5.0/24").is_ok());
        assert!(Subnet::parse("172.31.0.0/16").is_ok());
        assert!(Subnet::parse("8.8.8.0/24").is_err(), "public");
        assert!(Subnet::parse("172.32.0.0/24").is_err(), "public");
        assert!(Subnet::parse("10.20.0.5/24").is_err(), "host bits");
        assert!(Subnet::parse("10.0.0.0/8").is_err(), "too wide");
        let s = Subnet::parse("10.20.0.0/24").unwrap();
        assert!(s.contains("10.20.0.9".parse().unwrap()) && !s.contains("10.20.1.9".parse().unwrap()));
        assert!(domain_ok("corp.example.test") && !domain_ok("corp") && !domain_ok("a;b.test"));
        assert!(is_desktop("Microsoft Windows 10 Pro") && is_desktop("Microsoft Windows 11 Enterprise"));
        assert!(!is_desktop("Microsoft Windows Server 2022 Standard") && !is_desktop("Debian GNU/Linux 13"));
        assert!(vm_port("tap121i0", &[121]) && vm_port("fwpr121p0", &[121]));
        for p in ["tap122i0", "enp1s0", "bond0", "vmbr0.20", "veth100i0", "tap121i", "tap121ix"] {
            assert!(!vm_port(p, &[121]), "{p}");
        }
    }
}
