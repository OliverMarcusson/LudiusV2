//! `ludius setup plan|apply RANGE.toml`: build a range from a spec (docs/range-setup-design.html, phase 1).
//!
//! Phase 1 builds a new range on a Proxmox node, run on the node itself as root from a checkout: it orchestrates
//! the existing infra/ (Ansible, Packer), it does not reimplement it. Everything range-specific lands next to the
//! spec: `state.json` (finished steps, so an interrupted apply resumes), `manifest.jsonl` (what each step created,
//! irreversible effects flagged), `ansible-vars.yml`, the generated `personas/` and narrative (`runs/`), `logs/`
//! and `controller.env`.
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result, bail, ensure};
use rand::{RngExt as _, SeedableRng};
use regex::Regex;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::llm::{Check, LlmClient};
use crate::tasks::Persona;

// --- spec -------------------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Spec {
    pub range: Range,
    pub org: Org,
    pub personas: Personas,
    #[serde(default)]
    pub addons: Addons,
    pub ai: Ai,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Range {
    pub mode: String,
    pub subnet: String,
    pub domain: String,
    pub hypervisor: Hypervisor,
    /// sha256 of /var/lib/vz/template/iso/win10.iso (Packer never builds an unchecked ISO). Build ranges only.
    #[serde(default)]
    pub windows_iso_sha256: String,
    /// The scenario file whose reserved resources the personas keep off, or "none".
    #[serde(default = "d_none")]
    pub scenario: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Hypervisor {
    pub kind: String,
    #[serde(default = "d_storage")]
    pub storage: String,
    #[serde(default = "d_bridge")]
    pub bridge: String,
    #[serde(default = "d_build_bridge")]
    pub build_bridge: String,
    #[serde(default = "d_pool")]
    pub pool: String,
    #[serde(default = "d_prefix")]
    pub vm_prefix: String,
    /// Servers get base+10.., personas base+21..
    #[serde(default = "d_vmid_base")]
    pub vmid_base: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Org {
    pub name: String,
    pub industry: String,
    pub departments: Vec<String>,
    #[serde(default)]
    pub seed: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Personas {
    /// Build ranges: one new VM each. Existing ranges: the number of `desktops`.
    #[serde(default)]
    pub count: u32,
    /// Existing ranges: the Proxmox names of the desktops that get a persona, one each, in persona order.
    #[serde(default)]
    pub desktops: Vec<String>,
    /// Optional role mix hints for the generator ("2 in finance, 1 in sales").
    #[serde(default)]
    pub roles: Vec<String>,
    #[serde(default = "d_tz")]
    pub tz: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Addons {
    pub ad: Option<Addon>,
    pub mail: Option<Addon>,
    pub chat: Option<Addon>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Addon {
    #[serde(default)]
    pub backend: String,
    /// AD backend "adopt": "secret:<name>", the file holding the domain Administrator's password.
    #[serde(default)]
    pub admin: String,
    /// Mail backend "adopt" (an existing Exchange): its EWS URL, a name in the server's certificate.
    #[serde(default)]
    pub ews: String,
    /// Mail backend "adopt" (an existing IMAP server): its name, in the server's certificate; IMAPS on 993.
    #[serde(default)]
    pub imap: String,
    /// Mail backend "adopt", IMAP: the submission server, "NAME[:465|587]" (default: the imap name, 587 STARTTLS).
    #[serde(default)]
    pub smtp: String,
    /// Chat backend "adopt" (an existing Mattermost): its https URL, a name in the server's certificate.
    #[serde(default)]
    pub url: String,
    /// Chat backend "adopt": the Mattermost team the personas join.
    #[serde(default)]
    pub team: String,
    /// Chat backend "adopt": the system admin's username (its password in `admin`).
    #[serde(default)]
    pub user: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ai {
    pub planner: Model,
    pub decider: Model,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Model {
    pub provider: String,
    pub model: String,
    #[serde(default)]
    pub url: String,
    /// "secret:<name>": the file <secrets>/<name>. Never the key itself.
    #[serde(default)]
    pub key: String,
}

fn d_none() -> String {
    "none".into()
}
fn d_storage() -> String {
    "local-lvm".into()
}
fn d_bridge() -> String {
    "vmbr1".into()
}
fn d_build_bridge() -> String {
    "vmbr2".into()
}
fn d_pool() -> String {
    "ludius".into()
}
fn d_prefix() -> String {
    "ludius-".into()
}
fn d_vmid_base() -> u32 {
    100
}
fn d_tz() -> String {
    "Europe/Stockholm".into()
}

pub const BASE_VMID: u32 = 8999;
pub const TEMPLATE_VMID: u32 = 9000;
const FAILED_BUILD: &str = "deletes the VM a failed earlier build of this image left (this node, our pool, the build's name, not a template)";
const AGENT_UNIT: &str = "ludius-host-agent.service";
const PERSONA_MB: u32 = 6144;

fn re(p: &str) -> Regex {
    Regex::new(p).expect("static regex")
}

/// Where setup keeps secrets on the node: LUDIUS_SECRETS, else /etc/ludius/secrets.
pub fn secrets_dir() -> PathBuf {
    std::env::var_os("LUDIUS_SECRETS").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/etc/ludius/secrets"))
}

impl Spec {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path).with_context(|| path.display().to_string())?;
        let mut spec: Spec = toml::from_str(&text).with_context(|| path.display().to_string())?;
        if spec.existing() && spec.personas.count == 0 {
            spec.personas.count = spec.personas.desktops.len() as u32;
        }
        spec.validate()?;
        Ok(spec)
    }

    /// The first three octets of the /24 range subnet (Ansible's range_net).
    pub fn range_net(&self) -> &str {
        self.range.subnet.strip_suffix(".0/24").unwrap_or("")
    }

    /// Deploy into a range Ludius didn't build: no VMs created, the chosen desktops and the AD are adopted.
    pub fn existing(&self) -> bool {
        self.range.mode == "existing"
    }

    pub fn ad(&self) -> &str {
        self.addons.ad.as_ref().map_or("", |a| a.backend.as_str())
    }

    pub fn mail(&self) -> &str {
        self.addons.mail.as_ref().map_or("", |a| a.backend.as_str())
    }

    /// An existing range's Exchange: mailboxes for the personas, the scope and the EWS account; the server is the operator's.
    pub fn mail_adopt(&self) -> bool {
        self.existing() && self.mail() == "adopt" && !self.imap_adopt()
    }

    /// An existing range's IMAP server: nothing is made on it, every account signs in as itself (the AD password).
    pub fn imap_adopt(&self) -> bool {
        self.existing() && self.mail() == "adopt" && self.addons.mail.as_ref().is_some_and(|m| !m.imap.is_empty())
    }

    /// IMAP adopt: the IMAP name and the submission server as NAME:PORT.
    fn imap_hosts(&self) -> (String, String) {
        let m = self.addons.mail.clone().unwrap_or_default();
        let smtp = if m.smtp.is_empty() { m.imap.clone() } else { m.smtp };
        let smtp = if smtp.contains(':') { smtp } else { format!("{smtp}:587") };
        (m.imap, smtp)
    }

    pub fn chat(&self) -> bool {
        self.addons.chat.is_some()
    }

    /// An existing range's Mattermost: accounts, memberships and the narrative's team channels over its API.
    pub fn chat_adopt(&self) -> bool {
        self.existing() && self.addons.chat.is_some()
    }

    fn chat_addon(&self) -> Addon {
        self.addons.chat.clone().unwrap_or_default()
    }

    /// (user index -> vmid, ip)
    pub fn persona_slot(&self, i: u32) -> (u32, String) {
        (self.range.hypervisor.vmid_base + 21 + i, format!("{}.{}", self.range_net(), 21 + i))
    }

    /// Range servers as (vmid, name, MB).
    pub fn servers(&self) -> Vec<(u32, String, u32)> {
        if self.existing() {
            return vec![];
        }
        let (b, p) = (self.range.hypervisor.vmid_base, &self.range.hypervisor.vm_prefix);
        let mut s = if self.mail() == "exchange" {
            vec![(b + 10, "ludius-dc01".to_string(), 4096), (b + 11, "ludius-ex01".to_string(), 16384)]
        } else {
            vec![(b + 10, format!("{p}srv01"), 3072)]
        };
        if self.chat() {
            s.push((b + 12, "ludius-chat01".into(), 4096));
        }
        s
    }

    pub fn validate(&self) -> Result<()> {
        let r = &self.range;
        let h = &r.hypervisor;
        ensure!(r.mode == "build" || r.mode == "existing", "range.mode {:?}: \"build\" (a new range) or \"existing\" (deploy into a range Ludius didn't build)", r.mode);
        ensure!(h.kind == "proxmox", "range.hypervisor.kind {:?}: phase 1 drives Proxmox only", h.kind);
        let caps = re(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.0/24$").captures(&r.subnet).with_context(|| format!("range.subnet {:?}: a /24 such as 10.77.0.0/24", r.subnet))?;
        let o: Vec<u32> = (1..=3).map(|i| caps[i].parse().unwrap_or(999)).collect();
        ensure!(o.iter().all(|x| *x <= 255), "range.subnet {:?}: not an IPv4 network", r.subnet);
        let private = o[0] == 10 || (o[0] == 172 && (16..32).contains(&o[1])) || (o[0] == 192 && o[1] == 168);
        ensure!(private, "range.subnet {:?}: must be private (10/8, 172.16/12, 192.168/16): the range never routes out", r.subnet);
        ensure!(o[..2] != [10, 78], "range.subnet {:?}: 10.78.0.0/24 is the NAT build bridge", r.subnet);
        ensure!(re(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$").is_match(&r.domain), "range.domain {:?}: lower-case DNS name with a dot", r.domain);
        ensure!(self.existing() || re(r"^[0-9a-f]{64}$").is_match(&r.windows_iso_sha256), "range.windows_iso_sha256: 64 lower-case hex characters");
        ensure!(r.scenario == "none" || Path::new(&r.scenario).is_absolute(), "range.scenario: \"none\" or an absolute path to the scenario file");
        ensure!(re(r"^vmbr\d+$").is_match(&h.bridge) && re(r"^vmbr\d+$").is_match(&h.build_bridge) && h.bridge != h.build_bridge, "range.hypervisor bridge/build_bridge: two different vmbrN names");
        ensure!(re(r"^[A-Za-z0-9_-]+$").is_match(&h.storage) && re(r"^[A-Za-z0-9_-]+$").is_match(&h.pool), "range.hypervisor storage/pool: plain Proxmox ids");
        ensure!(re(r"^[a-z0-9-]+$").is_match(&h.vm_prefix), "range.hypervisor.vm_prefix: lower-case letters, digits and dashes");
        ensure!((100..=900).contains(&h.vmid_base), "range.hypervisor.vmid_base: 100-900");

        let n = self.personas.count;
        ensure!(n >= 1, "personas.count: at least one");
        ensure!(self.personas.tz.parse::<chrono_tz::Tz>().is_ok(), "personas.tz {:?}: an IANA time zone", self.personas.tz);
        ensure!(!self.org.name.trim().is_empty() && !self.org.industry.trim().is_empty(), "org.name and org.industry are required");
        ensure!(!self.org.departments.is_empty(), "org.departments: at least one");
        if self.existing() {
            let d = &self.personas.desktops;
            ensure!(n as usize == d.len(), "personas.count {n}: an existing range has one persona per entry in personas.desktops ({}); leave count out", d.len());
            ensure!(d.iter().all(|v| re(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$").is_match(v)), "personas.desktops: Proxmox VM names");
            ensure!(d.iter().collect::<std::collections::BTreeSet<_>>().len() == d.len(), "personas.desktops: a desktop twice");
            let ad = self.addons.ad.as_ref().context("addons.ad: an existing range needs backend = \"adopt\" (its domain gets the persona accounts)")?;
            ensure!(ad.backend == "adopt", "addons.ad.backend {:?}: an existing range adopts its AD (\"adopt\")", ad.backend);
            ensure!(re(r"^secret:[A-Za-z0-9_.-]+$").is_match(&ad.admin), "addons.ad.admin: \"secret:<name>\", the domain Administrator's password in {}/<name>", secrets_dir().display());
            if let Some(m) = &self.addons.mail {
                ensure!(m.backend == "adopt", "addons.mail.backend {:?}: an existing range adopts its mail server (\"adopt\")", m.backend);
                if m.imap.is_empty() {
                    ensure!(m.smtp.is_empty(), "addons.mail.smtp: only with imap (an Exchange takes mail over EWS)");
                    ensure!(re(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?/").is_match(&m.ews), "addons.mail.ews {:?}: the EWS URL, https://NAME/EWS/Exchange.asmx with a name in the server's certificate (or imap = NAME for an IMAP server)", m.ews);
                } else {
                    ensure!(m.ews.is_empty(), "addons.mail: ews (an Exchange) or imap (an IMAP server), not both");
                    ensure!(re(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$").is_match(&m.imap), "addons.mail.imap {:?}: the IMAP server's name, in its certificate (IMAPS on port 993)", m.imap);
                    ensure!(
                        m.smtp.is_empty() || re(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(:(465|587))?$").is_match(&m.smtp),
                        "addons.mail.smtp {:?}: NAME[:465|587], the submission server (default: the imap name, 587)",
                        m.smtp
                    );
                }
            }
            if let Some(c) = &self.addons.chat {
                ensure!(c.backend == "adopt", "addons.chat.backend {:?}: an existing range adopts its Mattermost (\"adopt\")", c.backend);
                ensure!(re(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$").is_match(&c.url), "addons.chat.url {:?}: https://NAME, a name in the chat server's certificate", c.url);
                ensure!(re(r"^[a-z0-9][a-z0-9-]{1,63}$").is_match(&c.team), "addons.chat.team {:?}: the Mattermost team's name (its URL handle)", c.team);
                ensure!(re(r"^[a-z0-9._-]{1,64}$").is_match(&c.user), "addons.chat.user {:?}: the system admin's username", c.user);
                ensure!(re(r"^secret:[A-Za-z0-9_.-]+$").is_match(&c.admin), "addons.chat.admin: \"secret:<name>\", the system admin's password in {}/<name>", secrets_dir().display());
            }
        } else {
            ensure!(self.personas.desktops.is_empty(), "personas.desktops: only for range.mode \"existing\"");
            ensure!(n <= 78, "personas.count: at most 78 (addresses .21-.99)");
            ensure!(h.vmid_base + 21 + n <= 999, "personas.count {n}: VM ids past 999 with vmid_base {}", h.vmid_base);
            match (self.ad(), self.mail()) {
                ("samba", "imap" | "grommunio") | ("windows-dc", "exchange") => {}
                (_, "exchange") => bail!("addons.mail exchange needs addons.ad backend \"windows-dc\" (Exchange does not run against Samba AD)"),
                ("", _) | (_, "") => bail!(
                    "phase 1 needs addons.ad and addons.mail: a workgroup range or AD without mail is not scripted yet (infra/ansible builds AD and mail on the same server)"
                ),
                (ad, mail) => bail!("addons.ad {ad:?} with addons.mail {mail:?}: supported are samba + imap|grommunio and windows-dc + exchange"),
            }
            ensure!(self.mail() != "grommunio" || n <= 5, "personas.count {n}: grommunio's community licence has 5 log-in mailboxes");
            if let Some(c) = &self.addons.chat {
                ensure!(c.backend.is_empty() || c.backend == "mattermost", "addons.chat.backend {:?}: mattermost", c.backend);
            }
        }

        for (what, m) in [("planner", &self.ai.planner), ("decider", &self.ai.decider)] {
            ensure!(!m.model.trim().is_empty(), "ai.{what}.model is required");
            match m.provider.as_str() {
                "openrouter" => ensure!(m.url.is_empty(), "ai.{what}: url is only for provider \"custom\""),
                "custom" => ensure!(m.url.starts_with("https://") || m.url.starts_with("http://"), "ai.{what}.url: the endpoint URL"),
                p => bail!("ai.{what}.provider {p:?}: openrouter or custom"),
            }
            ensure!(re(r"^secret:[A-Za-z0-9_.-]+$").is_match(&m.key), "ai.{what}.key: \"secret:<name>\" (the key lives in {}/<name>)", secrets_dir().display());
        }
        // ponytail: the controller reads one key (OPENROUTER_API_KEY) for both; separate keys need a second variable there
        ensure!(self.ai.planner.key == self.ai.decider.key, "ai.planner.key and ai.decider.key must be the same secret for now (the controller reads one key)");
        Ok(())
    }

    fn people_hash(&self) -> String {
        hash(&serde_json::to_vec(&(&self.org, &self.personas)).unwrap_or_default())
    }
}

fn hash(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}

// --- plan -------------------------------------------------------------------------------------

#[derive(Clone)]
pub struct Step {
    pub id: &'static str,
    pub what: String,
    /// Shell run from the checkout (bash -o pipefail); None for the steps setup does itself.
    pub sh: Option<String>,
    /// When this succeeds the step's result already exists (e.g. a template built earlier): not re-run.
    pub exists: Option<String>,
    pub creates: Vec<String>,
    pub irreversible: Vec<String>,
    /// Re-run when the spec changed since it finished (idempotent playbooks and files from the spec).
    pub rerun: bool,
}

pub struct Layout {
    pub dir: PathBuf,
}

impl Layout {
    pub fn new(spec_path: &Path) -> Self {
        Self { dir: std::path::absolute(spec_path).ok().and_then(|p| p.parent().map(Path::to_path_buf)).unwrap_or_else(|| PathBuf::from(".")) }
    }
    pub fn state(&self) -> PathBuf {
        self.dir.join("state.json")
    }
    pub fn manifest(&self) -> PathBuf {
        self.dir.join("manifest.jsonl")
    }
    pub fn vars(&self) -> PathBuf {
        self.dir.join("ansible-vars.yml")
    }
    pub fn personas(&self) -> PathBuf {
        self.dir.join("personas")
    }
    pub fn runs(&self) -> PathBuf {
        self.dir.join("runs")
    }
    pub fn narrative(&self) -> PathBuf {
        self.runs().join("world").join("narrative.yaml")
    }
    pub fn logs(&self) -> PathBuf {
        self.dir.join("logs")
    }
    pub fn controller_env(&self) -> PathBuf {
        self.dir.join("controller.env")
    }
}

const ANSIBLE: &str = "cd infra/ansible && ansible-playbook";

pub fn plan(spec: &Spec, l: &Layout) -> Vec<Step> {
    let build = plan_build(spec, l);
    if spec.existing() { plan_existing(spec, l, &build) } else { build }
}

fn agent_sh(spec: &Spec, exempt: &str, max: u32) -> String {
    format!(
        "set -e; test -x target/release/ludius-host-agent || {{ echo 'build it first: cargo build --release -p ludius-host-agent'; exit 1; }}; \
         install -d -m 700 {sec} /var/lib/ludius/agent-scratch; umask 077; \
         printf 'root@pam!packer=%s' \"$(cat /root/.ludius-packer-token)\" > {sec}/pve-token; \
         printf 'PVE_NODE=%s\\nPVE_POOL={pool}\\nPVE_CAP_EXEMPT={exempt}\\nAGENT_MAX_RUNNING={max}\\nAGENT_LISTEN=127.0.0.1:7451\\nPVE_TOKEN_FILE={sec}/pve-token\\nAGENT_SCRATCH_DIR=/var/lib/ludius/agent-scratch\\n' \"$(hostname)\" > /etc/ludius/host-agent.env; \
         printf '[Unit]\\nDescription=Ludius host agent (ludius setup)\\nAfter=pve-cluster.service\\n[Service]\\nEnvironmentFile=/etc/ludius/host-agent.env\\nExecStart=%s/target/release/ludius-host-agent\\nRestart=on-failure\\n[Install]\\nWantedBy=multi-user.target\\n' \"$PWD\" > /etc/systemd/system/{AGENT_UNIT}; \
         systemctl daemon-reload; systemctl enable {AGENT_UNIT}; systemctl restart {AGENT_UNIT}; sleep 3; systemctl is-active {AGENT_UNIT}",
        sec = secrets_dir().display(),
        pool = spec.range.hypervisor.pool,
    )
}

fn plan_build(spec: &Spec, l: &Layout) -> Vec<Step> {
    let h = &spec.range.hypervisor;
    let vars = l.vars().display().to_string();
    let n = spec.personas.count;
    let persona_ids: Vec<String> = (0..n).map(|i| spec.persona_slot(i).0.to_string()).collect();
    // A failed earlier build leaves its VM (-on-error=abort); remove it only when it is ours: this node, our pool,
    // the build's name, not a template. Anything else under that id stops the step.
    let clean = |id: u32, name: &str| {
        format!(
            "python3 -c 'import json,socket,subprocess,sys; r=[v for v in json.loads(subprocess.check_output([\"pvesh\",\"get\",\"/cluster/resources\",\"--type\",\"vm\",\"--output-format\",\"json\"])) if v[\"vmid\"]=={id}]; \
             sys.exit(0 if not r else 3 if (r[0].get(\"pool\")==\"{pool}\" and r[0].get(\"name\")==\"{name}\" and r[0].get(\"node\")==socket.gethostname() and not r[0].get(\"template\")) else 4)'; \
             case $? in 0) ;; 3) echo 'removing VM {id} ({name}) left by a failed earlier build'; qm stop {id} --skiplock 1 >/dev/null 2>&1 || true; qm destroy {id} --purge 1 ;; \
             *) echo 'VM {id} exists and is not a failed {name} build in pool {pool} on this node; refusing'; exit 1 ;; esac; ",
            pool = h.pool
        )
    };
    let packer = |only: &str| {
        let (id, name) = if only.starts_with("base") { (BASE_VMID, "ludius-win10-base") } else { (TEMPLATE_VMID, "ludius-win10-golden") };
        format!(
            "{}packer init infra/packer && packer build -only={only} -on-error=abort -var proxmox_node=$(hostname) -var proxmox_token=$(cat /root/.ludius-packer-token) \
             -var iso_checksum={} -var storage={} -var range_pool={} -var build_bridge={} infra/packer",
            clean(id, name),
            spec.range.windows_iso_sha256,
            h.storage,
            h.pool,
            h.build_bridge
        )
    };
    let template = |id: u32| format!("qm config {id} 2>/dev/null | grep -q '^template: 1'");
    let mut servers: Vec<String> = spec.servers().iter().map(|(id, name, _)| format!("VM {id} {name}")).collect();
    servers.extend((0..n).map(|i| format!("VM {} (persona clone)", spec.persona_slot(i).0)));
    vec![
        Step {
            id: "preflight",
            what: "Proxmox node, root, Windows ISO, signed observer, AI key, ludius-rs and the host agent built in the checkout".into(),
            sh: Some(format!(
                "set -e; pveversion; test \"$(id -u)\" = 0 || {{ echo 'run setup as root on the node'; exit 1; }}; \
                 test -f /var/lib/vz/template/iso/win10.iso || {{ echo 'copy the Windows 10 ISO to /var/lib/vz/template/iso/win10.iso'; exit 1; }}; \
                 test -f out/observer/uiprobe.exe -a -f secrets/range-root-ca.cer || {{ echo 'out/observer/uiprobe.exe and secrets/range-root-ca.cer missing: pwsh infra/packer/observer-artifacts.ps1 on the desktop, then copy them'; exit 1; }}; \
                 test -x ./ludius-rs || {{ echo './ludius-rs missing in the checkout (range-up checks each observer with it)'; exit 1; }}; \
                 test -x target/release/ludius-host-agent || {{ echo 'host agent not built: cargo build --release -p ludius-host-agent'; exit 1; }}; \
                 test -s {} || {{ echo 'AI key file missing'; exit 1; }}; {}\
                 command -v packer || {{ echo 'install packer: https://developer.hashicorp.com/packer/install'; exit 1; }}",
                key_path(&spec.ai.planner.key).display(),
                key_checks(spec)
            )),
            exists: None,
            creates: vec![],
            irreversible: vec![],
            rerun: true,
        },
        Step {
            id: "tools",
            what: "Ansible in ~/ansible (venv) and the collections in infra/ansible/collections".into(),
            sh: Some(
                "set -e; test -x ~/ansible/bin/ansible-playbook || { dpkg -s python3-venv >/dev/null 2>&1 || apt-get install -y python3-venv; \
                 python3 -m venv ~/ansible && ~/ansible/bin/pip install ansible-core pywinrm; }; \
                 cd infra/ansible && ansible-galaxy collection install -r requirements.yml -p collections"
                    .into(),
            ),
            exists: None,
            creates: vec!["dir ~/ansible (venv)".into(), "dir infra/ansible/collections".into()],
            irreversible: vec![],
            rerun: false,
        },
        Step {
            id: "vault",
            what: format!("Ansible vault with generated range passwords, its password in {}", secrets_dir().join("ansible-vault").display()),
            sh: None,
            exists: None,
            creates: vec!["file infra/ansible/group_vars/all/vault.yml".into(), format!("file {}", secrets_dir().join("ansible-vault").display())],
            irreversible: vec!["the generated passwords exist only in the vault: losing the vault password file loses them".into()],
            rerun: false,
        },
        Step {
            id: "personas",
            what: format!("{n} personas and the company narrative from the planner ({}), kept for every later run", spec.ai.planner.model),
            sh: None,
            exists: None,
            creates: vec![format!("dir {}", l.personas().display()), format!("file {}", l.narrative().display())],
            irreversible: vec![],
            rerun: false,
        },
        Step {
            id: "vars",
            what: format!("Ansible variables from the spec -> {vars}"),
            sh: None,
            exists: None,
            creates: vec![format!("file {vars}")],
            irreversible: vec![],
            rerun: true,
        },
        Step {
            id: "host",
            what: format!("Host: bridges {} (isolated) and {} (NAT build), DHCP on the build bridge, vhost_vsock, ISOs, pool {}, Packer token", h.bridge, h.build_bridge, h.pool),
            sh: Some(format!("{ANSIBLE} -i localhost, -c local proxmox-host.yml --tags range-host -e @{vars}")),
            exists: None,
            creates: vec![
                format!("bridge {}", h.bridge),
                format!("bridge {}", h.build_bridge),
                "file /etc/network/interfaces.d/ludius".into(),
                "file /etc/dnsmasq.d/ludius-build.conf".into(),
                format!("pool {}", h.pool),
                "token root@pam!packer (/root/.ludius-packer-token)".into(),
            ],
            irreversible: vec![],
            rerun: true,
        },
        Step {
            id: "image-base",
            what: format!("Packer stage 1: Windows 10 install and updates -> template {BASE_VMID} (~40 min)"),
            sh: Some(packer("base.proxmox-iso.base")),
            exists: Some(template(BASE_VMID)),
            creates: vec![format!("VM {BASE_VMID} ludius-win10-base (template)")],
            irreversible: vec![FAILED_BUILD.into()],
            rerun: false,
        },
        Step {
            id: "image-golden",
            what: format!("Packer stage 2: Office, observer, settings, sysprep -> template {TEMPLATE_VMID}"),
            sh: Some(packer("golden.proxmox-clone.golden")),
            exists: Some(template(TEMPLATE_VMID)),
            creates: vec![format!("VM {TEMPLATE_VMID} ludius-win10-golden (template)")],
            irreversible: vec![FAILED_BUILD.into()],
            rerun: false,
        },
        Step {
            id: "agent",
            what: format!("Host agent as systemd unit {AGENT_UNIT} (pool {}, range servers exempt from the running cap)", h.pool),
            sh: Some(agent_sh(spec, &spec.servers().iter().map(|(id, name, _)| format!("{id}:{name}")).collect::<Vec<_>>().join(","), n + spec.servers().len() as u32)),
            exists: None,
            creates: vec![
                format!("systemd unit /etc/systemd/system/{AGENT_UNIT}"),
                "file /etc/ludius/host-agent.env".into(),
                format!("file {}/pve-token (the Packer API token, root@pam!packer)", secrets_dir().display()),
            ],
            irreversible: vec![],
            rerun: true,
        },
        Step {
            id: "range",
            what: format!("range-up.yml: {} (AD {}, mail {}){}, persona clones {}", spec.servers().iter().map(|s| s.1.as_str()).collect::<Vec<_>>().join(", "), spec.ad(), spec.mail(), if spec.chat() { ", chat" } else { "" }, persona_ids.join(" ")),
            sh: Some(format!("{ANSIBLE} range-up.yml -e @{vars}")),
            exists: None,
            creates: servers,
            irreversible: if spec.mail() == "exchange" { vec!["Windows Server evaluation activation (online, once per server)".into()] } else { vec![] },
            rerun: true,
        },
        Step {
            id: "controller",
            what: format!("Controller settings -> {}", l.controller_env().display()),
            sh: None,
            exists: None,
            creates: vec![format!("file {}", l.controller_env().display()), format!("file {} (persona password suffix, 0600)", secrets_dir().join(PERSONA_SUFFIX).display())],
            irreversible: vec![],
            rerun: true,
        },
        Step {
            id: "verify",
            what: format!(
                "Ready check: {}one drift canary per persona ({}; {VERIFY_DECISIONS} decisions at most)",
                match [(!spec.mail().is_empty()).then_some("mail"), spec.chat().then_some("chat")].into_iter().flatten().collect::<Vec<_>>().join(" and ") {
                    reads if reads.is_empty() => String::new(),
                    reads => format!("{reads} read for the first persona, then "),
                },
                verify_apps(spec).join(", ")
            ),
            sh: Some(verify_sh(spec, l)),
            exists: None,
            creates: vec![format!("dir {}/canary (canary logs)", l.runs().display()), format!("file {}/health.json", l.runs().display())],
            irreversible: vec!["each canary does real work in the persona's noise folders and mailbox (a note, a document, a mail)".into()],
            rerun: true,
        },
    ]
}

const SNAPSHOT: &str = "ludius-before-setup";

/// Deploy into a range Ludius didn't build: the boundary gate first, a snapshot of every range VM before the first
/// change, then personas on the chosen desktops and accounts in the adopted AD. Creates no VMs.
fn plan_existing(spec: &Spec, l: &Layout, build: &[Step]) -> Vec<Step> {
    let h = &spec.range.hypervisor;
    let vars = l.vars().display().to_string();
    let dir = l.dir.display().to_string();
    let discovery = format!("{dir}/discovery.json");
    let desktops = spec.personas.desktops.join(" ");
    let admin = key_path(&spec.addons.ad.as_ref().map(|a| a.admin.clone()).unwrap_or_default());
    let shared = |id: &str| build.iter().find(|s| s.id == id).cloned().expect("build plan step");
    // the desktops' VM ids on this node, by name (qm list is node-local); a missing or ambiguous name stops the step
    let ids = format!(
        "ids=; for n in {desktops}; do i=$(qm list | awk -v n=\"$n\" 'NR>1 && $2==n {{print $1}}'); \
         [ \"$(echo \"$i\" | grep -c .)\" = 1 ] || {{ echo \"desktop $n: not exactly one VM of that name on this node\"; exit 1; }}; ids=\"$ids${{ids:+,}}$i\"; done; "
    );
    vec![
        Step {
            id: "preflight",
            what: "Proxmox node, root, signed observer, AI key, domain admin secret, ludius-rs and the host agent built in the checkout".into(),
            sh: Some(format!(
                "set -e; pveversion; test \"$(id -u)\" = 0 || {{ echo 'run setup as root on the node'; exit 1; }}; \
                 test -f out/observer/uiprobe.exe -a -f secrets/range-root-ca.cer || {{ echo 'out/observer/uiprobe.exe and secrets/range-root-ca.cer missing: pwsh infra/packer/observer-artifacts.ps1 on the desktop, then copy them'; exit 1; }}; \
                 test -x ./ludius-rs || {{ echo './ludius-rs missing in the checkout'; exit 1; }}; \
                 test -x target/release/ludius-host-agent || {{ echo 'host agent not built: cargo build --release -p ludius-host-agent'; exit 1; }}; \
                 test -s {key} || {{ echo 'AI key file missing'; exit 1; }}; {checks}\
                 test -s {admin} || {{ echo 'the domain Administrator password file {admin} is missing (addons.ad.admin)'; exit 1; }}{chat}",
                key = key_path(&spec.ai.planner.key).display(),
                checks = key_checks(spec),
                admin = admin.display(),
                chat = if spec.chat_adopt() {
                    let f = key_path(&spec.chat_addon().admin).display().to_string();
                    format!("; test -s {f} || {{ echo 'the chat admin password file {f} is missing (addons.chat.admin)'; exit 1; }}")
                } else {
                    String::new()
                },
            )),
            exists: None,
            creates: vec![],
            irreversible: vec![],
            rerun: true,
        },
        shared("tools"),
        shared("vault"),
        Step {
            id: "gate",
            what: format!("Boundary gate (hard stop): discover {} read-only, and {desktops} are Windows desktops on its bridge", spec.range.subnet),
            sh: Some(format!(
                "set -e; command -v nmap ldapsearch smbclient dig >/dev/null || apt-get install -y nmap ldap-utils smbclient dnsutils; \
                 ./ludius-rs setup discover --subnet {} --domain {}{ews}{imap}{chat} --out {dir}; \
                 python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); have={{v[\"name\"] for v in d[\"desktops\"]}}; miss=[n for n in sys.argv[2:] if n not in have]; \
                 miss and sys.exit(\"not Windows 10/11 desktops on the range bridge: \" + \" \".join(miss))' {discovery} {desktops}",
                spec.range.subnet, spec.range.domain,
                ews = if spec.mail_adopt() { format!(" --ews {}", shell_quote(&ews_url(spec))) } else { String::new() },
                imap = if spec.imap_adopt() {
                    let (imap, smtp) = spec.imap_hosts();
                    format!(" --imap {} --smtp {}", shell_quote(&imap), shell_quote(&smtp))
                } else {
                    String::new()
                },
                chat = if spec.chat_adopt() {
                    let c = spec.chat_addon();
                    format!(
                        " --chat {} --chat-team {} --chat-user {} --chat-secret {}",
                        shell_quote(&c.url),
                        shell_quote(&c.team),
                        shell_quote(&c.user),
                        shell_quote(&key_path(&c.admin).display().to_string())
                    )
                } else {
                    String::new()
                },
            )),
            exists: None,
            creates: vec![
                format!("file {discovery}"),
                format!("file {dir}/ews-ca.pem (mail adopt: the EWS certificate, pinned)"),
                format!("file {dir}/imap-ca.pem (IMAP adopt: the mail server's certificate, pinned)"),
                format!("files {dir}/chat-ca.pem and {dir}/chat.json (chat adopt: the chat certificate, pinned; the team's public channels)"),
                "node packages nmap, ldap-utils, smbclient, dnsutils when missing (read-only probes; on the node, not the range)".into(),
            ],
            irreversible: vec![],
            rerun: true,
        },
        Step {
            id: "snapshot",
            what: format!("Snapshot {SNAPSHOT} of every VM on the range bridge, before the first change"),
            sh: Some(format!(
                "set -e; br=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[\"bridge\"])' {discovery}); \
                 for id in $(qm list | awk 'NR>1 {{print $1}}'); do qm config $id | grep -Eq \"^net[0-9]+:.*bridge=$br(,|$)\" || continue; \
                 if qm listsnapshot $id | grep -q ' {SNAPSHOT} '; then echo \"VM $id: {SNAPSHOT} exists, kept\"; \
                 else qm snapshot $id {SNAPSHOT} --description 'ludius setup: the range before Ludius was deployed' || exit 1; echo \"VM $id: {SNAPSHOT}\"; fi; done"
            )),
            exists: None,
            creates: vec![format!("snapshot {SNAPSHOT} on every VM on the range bridge (roll back: qm rollback <id> {SNAPSHOT})")],
            irreversible: vec![],
            rerun: false,
        },
        Step {
            id: "host",
            what: format!("Host: vhost_vsock, virtio-win ISOs, pool {}, API token (after the gate and the snapshot; no bridges: the range's are the operator's)", h.pool),
            sh: Some(format!("{ANSIBLE} -i localhost, -c local proxmox-host.yml --tags adopt-host -e range_pool={}", h.pool)),
            exists: None,
            creates: vec![format!("pool {}", h.pool), "token root@pam!packer (/root/.ludius-packer-token)".into(), "virtio-win ISOs in /var/lib/vz/template/iso".into()],
            irreversible: vec![],
            rerun: true,
        },
        shared("personas"),
        shared("vars"),
        Step {
            id: "agent",
            what: format!("Desktops {desktops} into pool {}, host agent as systemd unit {AGENT_UNIT}", h.pool),
            // a rerun (or an operator who pooled them already) finds members: add only the rest; another pool still fails
            sh: Some(format!(
                "set -e; {ids}have=$(pvesh get /pools/{p} --output-format json | python3 -c 'import json,sys; print(\" \".join(str(m[\"vmid\"]) for m in json.load(sys.stdin).get(\"members\",[]) if \"vmid\" in m))'); \
                 add=; for i in ${{ids//,/ }}; do case \" $have \" in *\" $i \"*) ;; *) add=\"$add${{add:+,}}$i\";; esac; done; \
                 [ -z \"$add\" ] || pvesh set /pools/{p} --vms \"$add\"; echo \"pool {p}: $ids (added: ${{add:-none}})\"; {}",
                agent_sh(spec, "", spec.personas.count),
                p = h.pool
            )),
            exists: None,
            creates: vec![
                format!("pool {} membership of {desktops} (the host agent acts only on its pool)", h.pool),
                format!("systemd unit /etc/systemd/system/{AGENT_UNIT}"),
                "file /etc/ludius/host-agent.env".into(),
                format!("file {}/pve-token", secrets_dir().display()),
            ],
            irreversible: vec![],
            rerun: true,
        },
    ]
    .into_iter()
    .chain(spec.chat_adopt().then(|| {
        let c = spec.chat_addon();
        Step {
            id: "chat",
            what: format!("Mattermost {} (API, as {}): an account per narrative person, team {} and its public channels, the narrative's team channels", c.url, c.user, c.team),
            sh: None,
            exists: None,
            creates: vec![
                format!("Mattermost accounts for the narrative people (marked props.ludius = {}), password user + the persona suffix", chat::USER_MARK),
                format!("their membership of team {} and every public channel in it", c.team),
                format!("private channels for the narrative's team channels, purpose '{}'", chat::CHANNEL_MARK),
            ],
            irreversible: vec!["an account already marked as Ludius's gets its password reset to the persona password".into()],
            rerun: true,
        }
    }))
    .chain([
        Step {
            id: "adopt",
            what: format!(
                "range-adopt.yml: persona accounts in OU Ludius of {}{}, then on {desktops}: observer, settings, autologon",
                spec.range.domain,
                if spec.mail_adopt() {
                    ", their Exchange mailboxes and the EWS account"
                } else if spec.imap_adopt() {
                    ", Outlook IMAP profiles for their mail server"
                } else {
                    ""
                }
            ),
            sh: Some(format!("{ANSIBLE} range-adopt.yml -e @{vars}")),
            exists: None,
            creates: vec![
                format!("AD OU Ludius in {} with one account per narrative person", spec.range.domain),
                "on the desktops: event log sizes, Netlogon DisablePasswordChange (machine password kept through clock warps), the base role's power, lock, first-run and Edge settings".into(),
                format!("on {desktops}: observer in C:\\Program Files\\RangeAssist, its logon task, the range code-signing root, viosock driver, Office first-run settings, autologon as the persona"),
            ]
            .into_iter()
            .chain(
                spec.mail_adopt()
                    .then(|| {
                        [
                            format!("Exchange: a mailbox per persona account (OU Ludius), marked CustomAttribute1 {EWS_MARKER}"),
                            format!("Exchange: management scope '{EWS_SCOPE}' (OU Ludius, that marker) and role assignment '{EWS_ASSIGNMENT}' (ApplicationImpersonation in it) for {EWS_USER}"),
                            format!("AD account {EWS_USER} in OU Ludius, its password in {}", ews_secret(spec).display()),
                            format!("file {dir}/exchange.json (the mail domain the mailboxes got)"),
                        ]
                    })
                    .into_iter()
                    .flatten(),
            )
            .chain(spec.imap_adopt().then(|| {
                format!(
                    "on {desktops}: Outlook profile Ludius with the persona's IMAP account at {} (submission {}), its password stored by DPAPI; nothing on the mail server (each account signs in with its AD password)",
                    spec.imap_hosts().0,
                    spec.imap_hosts().1
                )
            }))
            .chain(spec.chat_adopt().then(|| format!("on {desktops}: a Mattermost session in the persona's Edge profile (signed in once, headless, in the persona's session; Edge policy UserDataDir %LOCALAPPDATA%\\Ludius\\Edge in the persona's hive)")))
            .collect(),
            irreversible: vec![
                "Windows Update, Defender and automatic maintenance off on the desktops (the image settings Ludius runs with)".into(),
                "WinRM is stopped and disabled on the desktops afterwards, also where it ran before".into(),
                "an account already in OU Ludius (from an earlier run) gets its password reset to the persona password".into(),
                "each desktop gets a vsock device and a fixed 1920x1080 standard display (QEMU args, vga std), applied with one power cycle".into(),
            ]
            .into_iter()
            .chain(spec.mail_adopt().then(|| "Exchange mailboxes, once created, hold mail; removing them (Disable-Mailbox) deletes it".to_string()))
            .collect(),
            rerun: true,
        },
        shared("controller"),
        shared("verify"),
    ])
    .collect()
}

const VERIFY_DECISIONS: u32 = 1000;
const VERIFY_APPS: [&str; 3] = ["outlook", "word", "excel"];

/// The range is ready only when every active add-on's outcome reads work and every persona finishes its canaries
/// (done contract: evidence outside the UI). `canary` exits 0 on unknown too, so only "ok" on every app counts.
/// Outlook only with a mail add-on: without a mail server it opens its account wizard, which is no drift.
fn verify_apps(spec: &Spec) -> Vec<&'static str> {
    VERIFY_APPS.into_iter().filter(|a| *a != "outlook" || !spec.mail().is_empty()).collect()
}

fn verify_sh(spec: &Spec, l: &Layout) -> String {
    let apps = verify_apps(spec).join(" ");
    let chat = if spec.chat() { "./ludius-rs chat check \"$first\" || fail=1; " } else { "" };
    let mail = if spec.mail().is_empty() { "" } else { "./ludius-rs mail check \"$first\" || fail=1; " };
    format!(
        r#"set -a; . {env}; set +a; export LUDIUS_CAP_DECISION={VERIFY_DECISIONS} LUDIUS_CAP_LLM=100; fail=0
users=$(ls {personas} | sed -n 's/\.yaml$//p'); first=$(echo "$users" | head -n1)
{mail}{chat}
for u in $users; do
  v=LUDIUS_VM_${{u^^}}; vm=${{!v:-{prefix}$u}}
  r=$(./ludius-rs --vm "$vm" canary --persona $u {apps} 2>&1); rc=$?; echo "$r"
  ok=$(grep -c '^  [a-z]*: ok (' <<< "$r")
  if [ $rc = 0 ] && [ "$ok" = {n} ]; then echo "canary $u: {n} apps ok"; else echo "canary $u: $ok of {n} apps ok"; fail=1; fi
done
exit $fail"#,
        env = l.controller_env().display(),
        personas = l.personas().display(),
        prefix = spec.range.hypervisor.vm_prefix,
        n = verify_apps(spec).len(),
    )
}

fn key_path(r: &str) -> PathBuf {
    secrets_dir().join(r.strip_prefix("secret:").unwrap_or(r))
}

// --- state and manifest -----------------------------------------------------------------------

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct State {
    pub spec_sha256: String,
    pub people_sha256: String,
    pub done: Vec<String>,
}

impl State {
    pub fn load(l: &Layout) -> Result<Self> {
        match std::fs::read_to_string(l.state()) {
            Ok(t) => serde_json::from_str(&t).with_context(|| l.state().display().to_string()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }

    fn save(&self, l: &Layout) -> Result<()> {
        let tmp = l.state().with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        std::fs::rename(&tmp, l.state())?;
        Ok(())
    }

    /// Reconcile with the spec: a changed spec re-runs the `rerun` steps; changed people refuse (personas exist).
    pub fn reconcile(&mut self, spec: &Spec, spec_text: &[u8], steps: &[Step]) -> Result<Vec<String>> {
        let (sh, ph) = (hash(spec_text), spec.people_hash());
        if !self.people_sha256.is_empty() && self.people_sha256 != ph && self.done.iter().any(|d| d == "personas") {
            bail!("[org] or [personas] changed after the personas were generated; start a new range directory instead (the generated people stay with this one)");
        }
        let mut reset = vec![];
        if !self.spec_sha256.is_empty() && self.spec_sha256 != sh {
            for s in steps.iter().filter(|s| s.rerun) {
                if let Some(i) = self.done.iter().position(|d| d == s.id) {
                    reset.push(self.done.remove(i));
                }
            }
        }
        self.spec_sha256 = sh;
        self.people_sha256 = ph;
        Ok(reset)
    }
}

/// One manifest line per step event: "started" (with what the step may create; the playbooks are idempotent, so
/// some of it may already have existed), then "done" or "failed"; or "adopted" (the result existed, nothing ran).
/// Each OpenRouter key answers before the slow steps (a bad key otherwise shows only at verify). The key goes
/// to curl as a header file, never on its command line.
fn key_checks(spec: &Spec) -> String {
    let mut keys: Vec<String> = [&spec.ai.planner, &spec.ai.decider]
        .iter()
        .filter(|m| m.provider == "openrouter")
        .map(|m| key_path(&m.key).display().to_string())
        .collect();
    keys.dedup();
    keys.iter()
        .map(|k| {
            let k = shell_quote(k);
            format!(
                "curl -fsS -m 20 -o /dev/null -H @<(printf 'Authorization: Bearer %s' \"$(cat {k})\") https://openrouter.ai/api/v1/key || {{ echo 'OpenRouter refused the key in '{k}; exit 1; }}; "
            )
        })
        .collect()
}

fn mmss(d: std::time::Duration) -> String {
    format!("{}m {:02}s", d.as_secs() / 60, d.as_secs() % 60)
}

fn record(l: &Layout, s: &Step, event: &str) -> Result<()> {
    use std::io::Write;
    let mut line = serde_json::json!({"at": chrono::Local::now().to_rfc3339(), "step": s.id, "event": event});
    if event == "started" {
        line["may_create"] = serde_json::json!(s.creates);
        line["irreversible"] = serde_json::json!(s.irreversible);
    }
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(l.manifest())?;
    writeln!(f, "{line}")?;
    Ok(())
}

// --- printing ---------------------------------------------------------------------------------

pub fn describe(spec: &Spec, l: &Layout, steps: &[Step], state: &State) -> String {
    let n = spec.personas.count;
    if spec.existing() {
        let head = format!(
            "range {} (existing, {}): {}, domain {} (adopted)\n  personas: {n}, one on each of {}, generated by the planner at apply\n  creates no VMs; snapshots every range VM first\n",
            l.dir.display(),
            spec.range.hypervisor.kind,
            spec.range.subnet,
            spec.range.domain,
            spec.personas.desktops.join(", "),
        );
        return head + &steps_text(steps, state);
    }
    let servers = spec.servers();
    let mb: u32 = servers.iter().map(|s| s.2).sum::<u32>() + n * PERSONA_MB;
    let (first, last) = (spec.persona_slot(0), spec.persona_slot(n - 1));
    let mut out = format!(
        "range {} ({}, {}): {} on {}, domain {}\n  servers: {}\n  personas: {n}, VMs {}-{} at {}-{}, generated by the planner at apply\n  RAM ~{} GB while running; the first build also makes the two images (about 21 min on pve1); later ranges reuse them\n",
        l.dir.display(),
        spec.range.mode,
        spec.range.hypervisor.kind,
        spec.range.subnet,
        spec.range.hypervisor.bridge,
        spec.range.domain,
        servers.iter().map(|(id, name, mb)| format!("{name} ({id}, {} GB)", mb / 1024)).collect::<Vec<_>>().join(", "),
        first.0,
        last.0,
        first.1,
        last.1,
        mb.div_ceil(1024),
    );
    out.push_str(&steps_text(steps, state));
    out
}

fn steps_text(steps: &[Step], state: &State) -> String {
    let mut out = String::from("steps:\n");
    for s in steps {
        let mark = if state.done.iter().any(|d| d == s.id) { "done" } else { "todo" };
        out.push_str(&format!("  [{mark}] {:<12} {}\n", s.id, s.what));
        for i in &s.irreversible {
            out.push_str(&format!("                      irreversible: {i}\n"));
        }
    }
    out
}

// --- apply ------------------------------------------------------------------------------------

pub fn apply(spec_path: &Path, root: &Path) -> Result<()> {
    let spec_text = std::fs::read(spec_path).with_context(|| spec_path.display().to_string())?;
    let spec = Spec::load(spec_path)?;
    let l = Layout::new(spec_path);
    let steps = plan(&spec, &l);
    let mut state = State::load(&l)?;
    for id in state.reconcile(&spec, &spec_text, &steps)? {
        println!("spec changed: {id} runs again");
    }
    state.save(&l)?;
    print!("{}", describe(&spec, &l, &steps, &state));
    std::fs::create_dir_all(l.logs())?;

    // children: Ansible from its venv, the vault password from the secrets dir, a one-off Packer build admin
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());
    let path = format!("{home}/ansible/bin:{}", std::env::var("PATH").unwrap_or_default());
    let vault_pass = std::env::var("ANSIBLE_VAULT_PASSWORD_FILE").unwrap_or_else(|_| secrets_dir().join("ansible-vault").display().to_string());
    // the base template keeps the build admin until stage 2 disables it: a resumed apply must reuse it
    let build_admin_file = secrets_dir().join("packer-build-admin");
    if !build_admin_file.exists() {
        write_secret(&build_admin_file, &password(24))?;
    }
    let build_admin = std::fs::read_to_string(&build_admin_file)?.trim().to_string();
    // persona and narrative generation below call the planner from this process
    if spec.ai.planner.provider == "custom" {
        unsafe { std::env::set_var("LUDIUS_PLANNER_URL", &spec.ai.planner.url) };
    }

    // the narrative's channel check reads the adopted team's channels (the gate's chat.json), not data/chat.yaml
    if spec.chat_adopt() {
        unsafe { std::env::set_var("LUDIUS_CHAT_CHANNELS_FILE", l.dir.join("chat.json")) };
    }

    let mut took: Vec<(&str, std::time::Duration)> = vec![];
    for s in &steps {
        if state.done.iter().any(|d| d == s.id) {
            continue;
        }
        println!("\n== {} : {}", s.id, s.what);
        let adopted = match &s.exists {
            Some(probe) => Command::new("bash").args(["-c", probe]).current_dir(root).status().is_ok_and(|st| st.success()),
            None => false,
        };
        // the start record comes first, so a step that fails half-way still leaves what it may have created
        record(&l, s, if adopted { "adopted" } else { "started" })?;
        let t0 = std::time::Instant::now();
        let ran = if adopted {
            println!("   already there, adopted");
            Ok(())
        } else {
            run_step(s, &spec, &l, root, &path, &vault_pass, &build_admin)
        };
        if let Err(e) = ran {
            record(&l, s, "failed")?;
            return Err(e);
        }
        if !adopted {
            record(&l, s, "done")?;
            took.push((s.id, t0.elapsed()));
            println!("   done in {}", mmss(t0.elapsed()));
        }
        state.done.push(s.id.into());
        state.save(&l)?;
    }
    if !took.is_empty() {
        // this run only: adopted steps and steps finished in an earlier run are not timed
        let sum = |verify: bool| took.iter().filter(|t| (t.0 == "verify") == verify).map(|t| t.1).sum::<std::time::Duration>();
        println!("\ntimings (this run): {}", took.iter().map(|(id, d)| format!("{id} {}", mmss(*d))).collect::<Vec<_>>().join(", "));
        println!("provisioning {}, verification {}", mmss(sum(false)), mmss(sum(true)));
    }
    println!("\nrange applied. Controller settings: {} (add them to the checkout's .env)", l.controller_env().display());
    Ok(())
}

fn run_step(s: &Step, spec: &Spec, l: &Layout, root: &Path, path: &str, vault_pass: &str, build_admin: &str) -> Result<()> {
    if let Some(sh) = &s.sh {
        let log = l.logs().join(format!("{}.log", s.id));
        println!("   $ {sh}\n   log: {}", log.display());
        let st = Command::new("bash")
            .args(["-o", "pipefail", "-c", &format!("({sh}) 2>&1 | tee -a {}", shell_quote(&log.display().to_string()))])
            .current_dir(root)
            .env("PATH", path)
            .env("LUDIUS_ROOT", root)
            .env("ANSIBLE_VAULT_PASSWORD_FILE", vault_pass)
            .env("PKR_VAR_admin_password", build_admin)
            .status()
            .with_context(|| format!("{}: bash", s.id))?;
        ensure!(st.success(), "step {} failed ({st}); see {}. Fix it and run apply again: finished steps are kept", s.id, log.display());
        return Ok(());
    }
    match s.id {
        "vault" => vault(root, vault_pass, path),
        "personas" => personas(spec, l),
        "vars" => Ok(std::fs::write(l.vars(), ansible_vars(spec, l)?)?),
        "chat" => {
            persona_suffix(root, vault_pass, path)?;
            chat_adopt(spec, l)
        }
        "controller" => {
            persona_suffix(root, vault_pass, path)?;
            Ok(std::fs::write(l.controller_env(), controller_env(spec, l, root))?)
        }
        other => bail!("no runner for step {other}"),
    }
}

fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// Alphanumeric with upper, lower and digit (Samba, Exchange and the vault example all accept it).
pub fn password(len: usize) -> String {
    const A: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
    let mut rng = rand::rngs::StdRng::from_rng(&mut rand::rng());
    loop {
        let p: String = (0..len).map(|_| A[rng.random_range(0..A.len())] as char).collect();
        if p.chars().any(|c| c.is_ascii_uppercase()) && p.chars().any(|c| c.is_ascii_lowercase()) && p.chars().any(|c| c.is_ascii_digit()) {
            return p;
        }
    }
}

fn write_secret(path: &Path, value: &str) -> Result<()> {
    if let Some(d) = path.parent() {
        std::fs::create_dir_all(d)?;
    }
    let mut o = std::fs::OpenOptions::new();
    o.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut o, 0o600);
    use std::io::Write;
    o.open(path).with_context(|| path.display().to_string())?.write_all(value.as_bytes())?;
    Ok(())
}

/// group_vars/all/vault.yml: kept when it exists (it may hold the owner's Office key); otherwise generated and encrypted.
fn vault(root: &Path, vault_pass: &str, path: &str) -> Result<()> {
    let pass = Path::new(vault_pass);
    let vault = root.join("infra/ansible/group_vars/all/vault.yml");
    if vault.exists() {
        ensure!(pass.exists(), "{} exists but the vault password file {} does not: set ANSIBLE_VAULT_PASSWORD_FILE", vault.display(), pass.display());
        println!("   {} exists, kept", vault.display());
        return Ok(());
    }
    if !pass.exists() {
        write_secret(pass, &password(32))?;
    }
    let body = format!(
        "vault_range_admin_password: {}\nvault_dc_admin_password: {}\nvault_grommunio_admin_password: {}\nvault_user_password_suffix: {}\n\
         vault_office_pidkey: \"\"\nvault_office_kms_host: \"\"\nvault_ews_password: {}\nvault_imap_master_password: {}\n",
        password(20),
        password(20),
        password(20),
        password(8),
        password(20),
        password(24)
    );
    write_secret(&vault, &body)?;
    let st = Command::new("ansible-vault").args(["encrypt", "--vault-password-file", vault_pass]).arg(&vault).env("PATH", path).status().context("ansible-vault")?;
    if !st.success() {
        let _ = std::fs::remove_file(&vault); // never leave the passwords in plain text
        bail!("ansible-vault encrypt failed ({st})");
    }
    Ok(())
}

/// The persona password suffix from the vault into secrets (0600): the canary and day runs unlock with user + suffix,
/// so no password goes into controller.env.
fn persona_suffix(root: &Path, vault_pass: &str, path: &str) -> Result<()> {
    let out = Command::new("ansible-vault")
        .args(["view", "--vault-password-file", vault_pass])
        .arg(root.join("infra/ansible/group_vars/all/vault.yml"))
        .env("PATH", path)
        .output()
        .context("ansible-vault view")?;
    ensure!(out.status.success(), "ansible-vault view failed ({})", out.status);
    let v: serde_yaml::Value = serde_yaml::from_slice(&out.stdout)?;
    let suffix = v["vault_user_password_suffix"].as_str().context("vault_user_password_suffix missing from the vault")?;
    let file = secrets_dir().join(PERSONA_SUFFIX);
    let _ = std::fs::remove_file(&file); // a rerun replaces it (the vault may have been regenerated)
    write_secret(&file, suffix)
}

const PERSONA_SUFFIX: &str = "persona-password-suffix";

// --- personas ---------------------------------------------------------------------------------

#[derive(Deserialize)]
struct Generated {
    personas: Vec<GenPersona>,
}

#[derive(Deserialize)]
struct GenPersona {
    name: String,
    user: String,
    role: String,
    department: String,
    hours: (String, String),
    style: String,
}

impl Check for Generated {
    fn check(&self) -> Result<()> {
        let user = re(r"^[a-z][a-z0-9]{1,9}$");
        let hhmm = re(r"^([01][0-9]|2[0-3]):[0-5][0-9]$");
        let name = re(r"^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ .'-]*$");
        let mut seen = std::collections::BTreeSet::new();
        for p in &self.personas {
            ensure!(user.is_match(&p.user), "user {:?}: 2-10 lower-case letters or digits, starting with a letter", p.user);
            ensure!(seen.insert(&p.user), "user {:?} twice", p.user);
            ensure!(name.is_match(&p.name), "name {:?}: letters, spaces, dots, apostrophes, dashes", p.name);
            ensure!(hhmm.is_match(&p.hours.0) && hhmm.is_match(&p.hours.1) && p.hours.0 < p.hours.1, "hours {:?}: [\"HH:MM\", \"HH:MM\"], start before end", p.hours);
            ensure!(!p.role.trim().is_empty() && !p.department.trim().is_empty(), "{}: role and department are required", p.user);
        }
        Ok(())
    }
}

const PERSONA_SYSTEM: &str = "You invent the employees of a fictional, ordinary company for a cyber-defence training range. \
They must read as mundane and plausible office workers. No real people, no security staff.";

fn brief(spec: &Spec) -> String {
    format!("Company: {} ({}). Departments: {}.", spec.org.name, spec.org.industry, spec.org.departments.join(", "))
}

fn personas(spec: &Spec, l: &Layout) -> Result<()> {
    let key = std::fs::read_to_string(key_path(&spec.ai.planner.key)).with_context(|| format!("AI key {}", spec.ai.planner.key))?;
    let llm = LlmClient::new(key.trim(), Some(&spec.ai.planner.model))?;
    let n = spec.personas.count as usize;
    std::fs::create_dir_all(l.personas())?;
    let mut have: Vec<Persona> = crate::paths::yaml_files(&l.personas())?.iter().map(|p| crate::tasks::load_yaml(p)).collect::<Result<_>>()?;
    if have.is_empty() {
        let roles = if spec.personas.roles.is_empty() { String::new() } else { format!(" Role mix: {}.", spec.personas.roles.join("; ")) };
        let user = format!(
            "{} Variety seed: {}.{roles}\nReturn JSON {{\"personas\": [...]}} with exactly {n} desk workers who use Windows, Outlook, Word and Excel daily. \
             Each: name (first and last), user (account: first initial + last name, lower case, 2-10 letters or digits), role (job title), \
             department (one of the departments), hours ([\"HH:MM\", \"HH:MM\"] working day), style (2-3 sentences: how they write notes and mails, \
             how they name files, one work habit).",
            brief(spec),
            spec.org.seed
        );
        let g: Generated = llm.json(PERSONA_SYSTEM, &user, 0.9, 3)?;
        ensure!(g.personas.len() >= n, "the planner returned {} personas, {n} asked", g.personas.len());
        let mut rng = rand::rngs::StdRng::seed_from_u64(spec.org.seed);
        for p in g.personas.into_iter().take(n) {
            let persona = Persona {
                name: p.name,
                user: p.user,
                role: p.role,
                department: p.department,
                hours: p.hours,
                style: p.style,
                typing_mean_ms: rng.random_range(110..=170),
                typing_jitter_ms: rng.random_range(50..=80),
                think_mean_s: f64::from(rng.random_range(12..=25_u32)) / 10.0,
                pointer_ms: (rng.random_range(180..=240), rng.random_range(480..=620)),
                risk_profile: vec![],
                tz: spec.personas.tz.clone(),
            };
            std::fs::write(l.personas().join(format!("{}.yaml", persona.user)), serde_yaml::to_string(&persona)?)?;
            println!("   {} ({}): {}, {}", persona.name, persona.user, persona.role, persona.department);
            have.push(persona);
        }
    } else {
        println!("   {} personas already generated, kept", have.len());
    }
    ensure!(have.len() == n, "{} has {} personas, the spec asks {n}", l.personas().display(), have.len());
    if !l.narrative().exists() {
        let n = crate::planner::make_narrative(&llm, &have, chrono::Local::now().date_naive(), &brief(spec))?;
        let path = crate::planner::World::new(&l.runs()).save_narrative(&n)?;
        println!("   {} ({}): {} people -> {}", n.company, n.city, n.people.len(), path.display());
    }
    println!("   planner cost ${:.4}", llm.cost.get());
    Ok(())
}

// --- generated files --------------------------------------------------------------------------

/// The generated personas' users in file order; an existing range's desktops are given out in this order.
fn persona_users(l: &Layout) -> Result<Vec<String>> {
    Ok(crate::paths::yaml_files(&l.personas())?.iter().filter_map(|p| p.file_stem().map(|s| s.to_string_lossy().into_owned())).collect())
}

pub fn ansible_vars(spec: &Spec, l: &Layout) -> Result<String> {
    let h = &spec.range.hypervisor;
    let users = persona_users(l)?;
    ensure!(users.len() == spec.personas.count as usize, "{}: {} persona files, the spec asks {}", l.personas().display(), users.len(), spec.personas.count);
    if spec.existing() {
        let personas: Vec<serde_json::Value> = users.iter().zip(&spec.personas.desktops).map(|(u, vm)| serde_json::json!({"user": u, "vm": vm})).collect();
        let v = serde_json::json!({
            "domain": spec.range.domain,
            "mail_backend": "",
            "mail_adopt": spec.mail_adopt(),
            "imap_adopt": spec.imap_adopt(),
            "imap_server": spec.imap_hosts().0,
            "smtp_server": spec.imap_hosts().1.rsplit_once(':').map(|h| h.0.to_string()).unwrap_or_default(),
            "smtp_port": spec.imap_hosts().1.rsplit_once(':').and_then(|h| h.1.parse::<u16>().ok()).unwrap_or(587),
            "ews_url": ews_url(spec),
            "ews_password_file": ews_secret(spec).display().to_string(),
            "mail_facts_file": l.dir.join("exchange.json").display().to_string(),
            "chat_enabled": false,
            "chat_adopt": spec.chat_adopt(),
            "chat_url": spec.chat_addon().url.trim_end_matches('/'),
            "range_pool": h.pool,
            "range_net": spec.range_net(),
            "range_personas": personas,
            "discovery_file": l.dir.join("discovery.json").display().to_string(),
            "dc_admin_file": key_path(&spec.addons.ad.as_ref().map(|a| a.admin.clone()).unwrap_or_default()).display().to_string(),
            "persona_dir": l.personas().display().to_string(),
            "narrative_file": l.narrative().display().to_string(),
        });
        return Ok(format!("# generated by ludius setup from {}; edit the spec, not this file\n{}", l.dir.join("range.toml").display(), serde_yaml::to_string(&v)?));
    }
    let b = h.vmid_base;
    let personas: Vec<serde_json::Value> = users
        .iter()
        .enumerate()
        .map(|(i, u)| {
            let (vmid, ip) = spec.persona_slot(i as u32);
            serde_json::json!({"user": u, "vmid": vmid, "ip": ip})
        })
        .collect();
    let v = serde_json::json!({
        "domain": spec.range.domain,
        "mail_backend": spec.mail(),
        "chat_enabled": spec.chat(),
        "range_bridge": h.bridge,
        "build_bridge": h.build_bridge,
        "extra_range_bridges": [],
        "range_net": spec.range_net(),
        "range_pool": h.pool,
        "storage": h.storage,
        "vm_prefix": h.vm_prefix,
        "template_vmid": TEMPLATE_VMID,
        "srv01_vmid": b + 10,
        "ex01_vmid": b + 11,
        "chat01_vmid": b + 12,
        "range_personas": personas,
        "persona_dir": l.personas().display().to_string(),
        "narrative_file": l.narrative().display().to_string(),
    });
    Ok(format!("# generated by ludius setup from {}; edit the spec, not this file\n{}", l.dir.join("range.toml").display(), serde_yaml::to_string(&v)?))
}

pub fn controller_env(spec: &Spec, l: &Layout, root: &Path) -> String {
    let mut e = vec![
        format!("LUDIUS_SCENARIO={}", spec.range.scenario),
        format!("LUDIUS_RANGE_DOMAIN={}", spec.range.domain),
        format!("LUDIUS_RUNS={}", l.runs().display()),
        format!("LUDIUS_PERSONAS={}", l.personas().display()),
        format!("OPENROUTER_KEY_FILE={}", key_path(&spec.ai.planner.key).display()),
        format!("LUDIUS_PERSONA_PASSWORD_SUFFIX_FILE={}", secrets_dir().join(PERSONA_SUFFIX).display()),
        format!("LUDIUS_PLANNER_MODEL={}", spec.ai.planner.model),
        format!("LUDIUS_DECISION_MODEL={}", spec.ai.decider.model),
    ];
    if spec.ai.planner.provider == "custom" {
        e.push(format!("LUDIUS_PLANNER_URL={}", spec.ai.planner.url));
    }
    if spec.ai.decider.provider == "custom" {
        e.push(format!("LUDIUS_DECISIONS_URL={}", spec.ai.decider.url));
    }
    if spec.existing() {
        // the adopted desktops keep their names; rangerun::vm_of reads these
        let users = persona_users(l).unwrap_or_default();
        e.extend(users.iter().zip(&spec.personas.desktops).map(|(u, vm)| format!("LUDIUS_VM_{}={vm}", u.to_uppercase())));
    }
    let net = spec.range_net();
    let ca = root.join("infra/ansible/build/range-ca.crt").display().to_string();
    match spec.mail() {
        "adopt" if spec.imap_adopt() => {
            // the gate's discovery (addresses, pinned certificate); no LUDIUS_IMAP_USER: each account signs in as itself
            let d = std::fs::read_to_string(l.dir.join("discovery.json")).ok().and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok()).unwrap_or_default();
            let get = |k: &str, f: &str| d[k][f].as_str().unwrap_or_default().to_string();
            let (imap, smtp) = spec.imap_hosts();
            e.extend([
                "LUDIUS_MAIL_BACKEND=imap".into(),
                format!("LUDIUS_MAIL_DOMAIN={}", spec.range.domain),
                format!("LUDIUS_IMAP_HOST={imap}"),
                format!("LUDIUS_IMAP_ADDR={}", get("imap", "ip")),
                format!("LUDIUS_IMAP_CA={}", get("imap", "ca")),
                format!("LUDIUS_SMTP_SUBMIT={smtp}"),
                format!("LUDIUS_SMTP_ADDR={}", get("smtp", "ip")),
            ]);
        }
        "adopt" if spec.existing() => {
            // the gate's discovery (EWS address, pinned certificate) and the adopt play's exchange.json (mail domain)
            let read = |f: &str| std::fs::read_to_string(l.dir.join(f)).ok().and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok()).unwrap_or_default();
            let (d, x) = (read("discovery.json"), read("exchange.json"));
            let get = |v: &serde_json::Value, k: &str| v[k].as_str().unwrap_or_default().to_string();
            e.extend([
                "LUDIUS_MAIL_BACKEND=exchange".into(),
                format!("LUDIUS_MAIL_DOMAIN={}", get(&x, "mail_domain")),
                format!("LUDIUS_EWS_URL={}", ews_url(spec)),
                format!("LUDIUS_EWS_ADDR={}", get(&d["ews"], "ip")),
                format!("LUDIUS_EWS_USER={EWS_USER}@{}", spec.range.domain),
                format!("LUDIUS_EWS_PASSWORD_FILE={}", ews_secret(spec).display()),
                format!("LUDIUS_EWS_CA={}", get(&d["ews"], "ca")),
            ]);
        }
        "exchange" => e.extend([
            "LUDIUS_MAIL_BACKEND=exchange".into(),
            "LUDIUS_MAIL_DOMAIN=ludius.internal".into(),
            format!("LUDIUS_EWS_URL=https://{net}.12/EWS/Exchange.asmx"),
            format!("LUDIUS_EWS_USER=ludiussvc@{}", spec.range.domain),
            "LUDIUS_EWS_PASSWORD_FILE=/root/.ludius-ews".into(),
            format!("LUDIUS_EWS_CA={ca}"),
            "LUDIUS_SHARE_HOST=dc01".into(),
        ]),
        "imap" => e.extend([
            "LUDIUS_MAIL_BACKEND=imap".into(),
            format!("LUDIUS_MAIL_DOMAIN={}", spec.range.domain),
            format!("LUDIUS_IMAP_HOST={net}.11"),
            "LUDIUS_IMAP_USER=ludius-master".into(),
            "LUDIUS_IMAP_PASSWORD_FILE=/root/.ludius-imap".into(),
            format!("LUDIUS_IMAP_CA={ca}"),
        ]),
        _ => e.push(format!("LUDIUS_MAIL_DOMAIN={}", spec.range.domain)),
    }
    if spec.chat_adopt() {
        // the gate's discovery: the chat's address in the range, its pinned certificate, the team's channel list
        let d = std::fs::read_to_string(l.dir.join("discovery.json")).ok().and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok()).unwrap_or_default();
        let get = |k: &str| d["chat"][k].as_str().unwrap_or_default().to_string();
        let c = spec.chat_addon();
        e.extend([
            format!("LUDIUS_CHAT_URL={}", c.url.trim_end_matches('/').trim_start_matches("https://")),
            format!("LUDIUS_CHAT_API={}", c.url.trim_end_matches('/')),
            format!("LUDIUS_CHAT_ADDR={}", get("ip")),
            format!("LUDIUS_CHAT_CA={}", get("ca")),
            format!("LUDIUS_CHAT_ADMIN_USER={}", c.user),
            format!("LUDIUS_CHAT_ADMIN_PASSWORD_FILE={}", key_path(&c.admin).display()),
            format!("LUDIUS_CHAT_TEAM={}", c.team),
            format!("LUDIUS_CHAT_CHANNELS_FILE={}", get("channels_file")),
        ]);
    } else if spec.chat() {
        e.push(format!("LUDIUS_CHAT_URL=chat.{}", spec.range.domain));
        e.push(format!("LUDIUS_CHAT_HOST={net}.13"));
    }
    format!("# generated by ludius setup; add to the checkout's .env\n{}\n", e.join("\n"))
}

/// infra/ansible group_vars: the EWS account and the marker its impersonation scope filters on.
const EWS_USER: &str = "ludiussvc";
const EWS_MARKER: &str = "ludius-noise";
const EWS_SCOPE: &str = "Ludius noise mailboxes";
const EWS_ASSIGNMENT: &str = "Ludius EWS impersonation";

fn ews_url(spec: &Spec) -> String {
    spec.addons.mail.as_ref().map(|m| m.ews.clone()).unwrap_or_default()
}

/// The adopted Exchange's EWS account password (the adopt play generates it once).
fn ews_secret(spec: &Spec) -> PathBuf {
    secrets_dir().join(format!("ews-{}", spec.range.domain))
}

/// The chat step: accounts, memberships and team channels on the adopted Mattermost (setup/chat.rs).
fn chat_adopt(spec: &Spec, l: &Layout) -> Result<()> {
    let c = spec.chat_addon();
    let d: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(l.dir.join("discovery.json"))?)?;
    let (ip, ca) = (d["chat"]["ip"].as_str().context("discovery.json: no chat (gate)")?, d["chat"]["ca"].as_str().context("discovery.json: no chat ca")?);
    let mut s = chat::connect(&c.url, ip.parse()?, &std::fs::read(ca)?)?;
    s.login(&c.user, std::fs::read_to_string(key_path(&c.admin))?.trim())?;
    let n: crate::planner::Narrative = serde_yaml::from_str(&std::fs::read_to_string(l.narrative())?)?;
    let people: Vec<(String, String)> = n.people.iter().filter(|p| !p.user.is_empty()).map(|p| (p.user.clone(), p.name.clone())).collect();
    let suffix = std::fs::read_to_string(secrets_dir().join(PERSONA_SUFFIX))?;
    chat::adopt(&s, &c.team, &spec.range.domain, &people, suffix.trim(), &n.channels)
}

pub mod chat;
pub mod discover;

pub fn main(args: &[String], root: &Path) -> Result<()> {
    const USAGE: &str = "usage: ludius setup plan|apply RANGE.toml | discover --subnet A.B.C.D/P --domain DOMAIN [--ews URL] [--out DIR]";
    if args.first().is_some_and(|a| a == "discover") {
        return discover::main(&args[1..]);
    }
    let (Some(cmd), Some(path)) = (args.first(), args.get(1)) else { bail!(USAGE) };
    let path = Path::new(path);
    match cmd.as_str() {
        "plan" => {
            let spec = Spec::load(path)?;
            let l = Layout::new(path);
            let steps = plan(&spec, &l);
            let mut state = State::load(&l)?;
            let reset = state.reconcile(&spec, &std::fs::read(path)?, &steps)?;
            print!("{}", describe(&spec, &l, &steps, &state));
            if !reset.is_empty() {
                println!("spec changed since the last apply: {} run again", reset.join(", "));
            }
            Ok(())
        }
        "apply" => apply(path, root),
        _ => bail!(USAGE),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SPEC: &str = r#"
[range]
mode = "build"
subnet = "10.80.0.0/24"
domain = "corp.example.test"
hypervisor = { kind = "proxmox", vmid_base = 200 }
windows_iso_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

[org]
name = "Example Freight"
industry = "freight forwarding"
departments = ["Operations", "Finance"]

[personas]
count = 3

[addons.ad]
backend = "samba"
[addons.mail]
backend = "imap"
[addons.chat]

[ai]
planner = { provider = "openrouter", model = "deepseek/deepseek-v4.1-flash", key = "secret:openrouter" }
decider = { provider = "openrouter", model = "liquid/d1.1", key = "secret:openrouter" }
"#;

    fn spec(edit: &str, with: &str) -> Result<Spec> {
        let s: Spec = toml::from_str(&SPEC.replace(edit, with))?;
        s.validate().map(|()| s)
    }

    #[test]
    fn validates_and_lays_out() {
        let s = spec("", "").unwrap();
        assert_eq!(s.range_net(), "10.80.0");
        assert_eq!(s.persona_slot(0), (221, "10.80.0.21".into()));
        assert_eq!(s.servers().iter().map(|x| x.0).collect::<Vec<_>>(), vec![210, 212]);
        assert!(spec("10.80.0.0/24", "8.8.8.0/24").is_err(), "public subnet");
        assert!(spec("10.80.0.0/24", "10.78.0.0/24").is_err(), "build bridge subnet");
        assert!(spec(r#"backend = "imap""#, r#"backend = "exchange""#).is_err(), "exchange on samba");
        assert!(spec("[addons.ad]\nbackend = \"samba\"", "").is_err(), "no AD");
        assert!(spec("count = 3", "count = 0").is_err());
        assert!(spec(r#"backend = "imap""#, r#"backend = "grommunio""#).is_ok());
        let six_grommunio = SPEC.replace("count = 3", "count = 6").replace(r#"backend = "imap""#, r#"backend = "grommunio""#);
        assert!(toml::from_str::<Spec>(&six_grommunio).unwrap().validate().is_err(), "grommunio licence cap");
        assert!(spec(r#"key = "secret:openrouter" }"#, r#"key = "sk-or-v1-abc" }"#).is_err(), "inline key");
        assert!(spec("mode = \"build\"", "mode = \"build\"\nbogus = 1").is_err(), "unknown field");
    }

    #[test]
    fn existing_range_adopts_and_gates_first() {
        let existing = |edit: &str, with: &str| -> Result<Spec> {
            let text = SPEC
                .replace("mode = \"build\"", "mode = \"existing\"")
                .replace("count = 3", "desktops = [\"ws-a\", \"ws-b\"]")
                .replace("backend = \"samba\"", "backend = \"adopt\"\nadmin = \"secret:dc-admin\"")
                .replace("[addons.mail]\nbackend = \"imap\"\n[addons.chat]\n", "")
                .replace(edit, with);
            let mut s: Spec = toml::from_str(&text)?;
            s.personas.count = s.personas.desktops.len() as u32; // as Spec::load does
            s.validate().map(|()| s)
        };
        let s = existing("", "").unwrap();
        assert!(s.servers().is_empty());
        let ids: Vec<&str> = plan(&s, &Layout { dir: PathBuf::from("/tmp/r") }).iter().map(|s| s.id).collect();
        assert_eq!(ids, ["preflight", "tools", "vault", "gate", "snapshot", "host", "personas", "vars", "agent", "adopt", "controller", "verify"]);
        assert_eq!(verify_apps(&s), ["word", "excel"], "no Outlook without mail");
        assert!(existing("backend = \"adopt\"", "backend = \"samba\"").is_err(), "AD must be adopted");
        assert!(existing("admin = \"secret:dc-admin\"", "admin = \"hunter2\"").is_err(), "inline admin password");
        assert!(existing("\"ws-b\"", "\"ws-a\"").is_err(), "desktop twice");
        assert!(existing("[ai]", "[addons.mail]\nbackend = \"imap\"\n[ai]").is_err(), "an existing range adopts its mail");
        assert!(existing("[ai]", "[addons.mail]\nbackend = \"adopt\"\n[ai]").is_err(), "adopted mail needs its EWS URL");
        let m = existing("[ai]", "[addons.mail]\nbackend = \"adopt\"\news = \"https://mail.corp.test/EWS/Exchange.asmx\"\n[ai]").unwrap();
        assert!(m.mail_adopt() && verify_apps(&m).contains(&"outlook"));
        let gate = plan(&m, &Layout { dir: PathBuf::from("/tmp/r") }).into_iter().find(|s| s.id == "gate").unwrap();
        assert!(gate.sh.unwrap().contains("--ews 'https://mail.corp.test/EWS/Exchange.asmx'"));
        let i = existing("[ai]", "[addons.mail]\nbackend = \"adopt\"\nimap = \"mail.corp.test\"\n[ai]").unwrap();
        assert!(i.imap_adopt() && !i.mail_adopt() && verify_apps(&i).contains(&"outlook"));
        let gate = plan(&i, &Layout { dir: PathBuf::from("/tmp/r") }).into_iter().find(|s| s.id == "gate").unwrap();
        assert!(gate.sh.unwrap().contains("--imap 'mail.corp.test' --smtp 'mail.corp.test:587'"));
        let env = controller_env(&i, &Layout { dir: PathBuf::from("/tmp/r") }, Path::new("/x"));
        assert!(env.contains("LUDIUS_MAIL_BACKEND=imap\n") && env.contains("LUDIUS_SMTP_SUBMIT=mail.corp.test:587\n") && !env.contains("LUDIUS_IMAP_USER"));
        assert!(existing("[ai]", "[addons.mail]\nbackend = \"adopt\"\nimap = \"mail.corp.test\"\news = \"https://mail.corp.test/EWS/Exchange.asmx\"\n[ai]").is_err(), "ews or imap");
        assert!(existing("[ai]", "[addons.mail]\nbackend = \"adopt\"\nimap = \"mail.corp.test\"\nsmtp = \"mail.corp.test:25\"\n[ai]").is_err(), "submission only");
        assert!(existing("[ai]", "[addons.chat]\n[ai]").is_err(), "adopted chat needs backend, url, team, user, admin");
        let c = existing("[ai]", "[addons.chat]\nbackend = \"adopt\"\nurl = \"https://chat.corp.test\"\nteam = \"corp\"\nuser = \"mmadmin\"\nadmin = \"secret:chat-admin\"\n[ai]").unwrap();
        let steps = plan(&c, &Layout { dir: PathBuf::from("/tmp/r") });
        assert!(steps.iter().map(|s| s.id).collect::<Vec<_>>().windows(2).any(|w| w == ["chat", "adopt"]));
        assert!(steps.iter().find(|s| s.id == "gate").unwrap().sh.as_ref().unwrap().contains("--chat 'https://chat.corp.test' --chat-team 'corp' --chat-user 'mmadmin'"));
        assert!(controller_env(&c, &Layout { dir: PathBuf::from("/tmp/r") }, Path::new("/x")).contains("LUDIUS_CHAT_API=https://chat.corp.test\n"));
        assert!(spec("count = 3", "count = 3\ndesktops = [\"ws-a\"]").is_err(), "desktops only for existing");
    }

    #[test]
    fn state_resets_rerun_steps_and_guards_people() {
        let s = spec("", "").unwrap();
        let l = Layout { dir: PathBuf::from("/tmp/r") };
        let steps = plan(&s, &l);
        let mut st = State::default();
        st.reconcile(&s, b"v1", &steps).unwrap();
        st.done = steps.iter().map(|s| s.id.to_string()).collect();
        let reset = st.reconcile(&s, b"v2", &steps).unwrap();
        assert!(reset.contains(&"range".to_string()) && !reset.contains(&"personas".to_string()) && !reset.contains(&"image-base".to_string()));
        let mut other = s.clone();
        other.personas.count = 4;
        assert!(st.reconcile(&other, b"v3", &steps).is_err(), "people changed after generation");
    }
}
