//! Mail add-on: outcome checks (what a mailbox holds, read state, invites, responses) and planned
//! deliveries. Backends are features: `ews` (Exchange) and `imap` (IMAPS reads, SMTP deliveries; no calendar). Without one, every read is unknown and nothing is sent.
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::OnceLock;

#[cfg(feature = "ews")]
pub mod ews;
#[cfg(feature = "imap")]
pub mod imap;

type Query = fn(&str, &str, Option<&str>, bool) -> Option<Vec<Message>>;

/// The selected backend's calls.
struct Backend {
    read_state: fn(&str, u64, &str) -> Option<bool>,
    msgids: fn(&str, &str) -> Option<Vec<String>>,
    query: Query,
    send: fn(&str, &str, &str, &str, &str, &str, &str) -> bool,
}

/// LUDIUS_MAIL_BACKEND, if compiled in: `exchange` (feature ews) or `imap` (feature imap). None: every read is
/// unknown, never empty, and nothing is delivered.
fn backend() -> Option<Backend> {
    match std::env::var("LUDIUS_MAIL_BACKEND").unwrap_or_default().as_str() {
        #[cfg(feature = "ews")]
        "exchange" => Some(Backend { read_state: ews::read_state, msgids: ews::msgids, query: ews::query, send: ews::send }),
        #[cfg(feature = "imap")]
        "imap" => Some(Backend { read_state: imap::read_state, msgids: imap::msgids, query: imap::query, send: imap::send }),
        _ => None,
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Message {
    pub sender: String,
    pub submit_ms: i64,
    pub delivery_ms: i64,
    pub body: String,
    pub recipients: Vec<String>,
    pub id: u64,
    pub cls: String,
    pub start_ms: i64,
    pub end_ms: i64,
    pub created_ms: i64,
    pub organizer: String,
    pub subject: String,
    pub prefix: String,
    pub msgid: String,
    pub in_reply_to: String,
    pub read: bool,
    /// (casefolded name, sha256 hex), sorted; None = not asked or unreadable.
    pub attachments: Option<Vec<(String, String)>>,
    pub files: Vec<String>,
}

#[cfg(any(feature = "ews", feature = "imap"))]
fn env(key: &str) -> String {
    std::env::var(key).unwrap_or_default()
}

/// Repo root: LUDIUS_ROOT, else the working directory (as ludius::paths::root).
fn root() -> PathBuf {
    std::env::var_os("LUDIUS_ROOT").map(PathBuf::from).unwrap_or_else(|| std::env::current_dir().unwrap_or_default())
}

fn casefold(s: &str) -> String {
    s.to_lowercase().replace('ß', "ss").replace('ς', "σ")
}

pub const MINUTE_MS: i64 = 60_000;
pub const MEETING_REQUEST: &str = "IPM.Schedule.Meeting.Request";
pub const APPOINTMENT: &str = "IPM.Appointment";

/// (user, folder, item id) seen before a task ran.
pub type Seen = BTreeSet<(String, String, u64)>;

/// LUDIUS_MAIL_DOMAIN, else the checkout's .env; unset is empty, so no address passes as a colleague's.
pub fn domain() -> &'static str {
    static D: OnceLock<String> = OnceLock::new();
    D.get_or_init(|| {
        std::env::var("LUDIUS_MAIL_DOMAIN")
            .ok()
            .filter(|d| !d.is_empty())
            .or_else(|| {
                let env = std::fs::read_to_string(root().join(".env")).ok()?;
                env.lines().filter_map(|l| l.split_once('=')).find(|(k, _)| k.trim() == "LUDIUS_MAIL_DOMAIN").map(|(_, v)| v.trim().to_string())
            })
            .filter(|d| !d.is_empty())
            .unwrap_or_default()
    })
}

pub fn response_class(response: &str) -> Option<&'static str> {
    match response.to_lowercase().as_str() {
        "accept" => Some("IPM.Schedule.Meeting.Resp.Pos"),
        "decline" => Some("IPM.Schedule.Meeting.Resp.Neg"),
        _ => None,
    }
}

const RESPONSES: [&str; 2] = ["IPM.Schedule.Meeting.Resp.Pos", "IPM.Schedule.Meeting.Resp.Neg"];

/// Whether this build and range read and deliver mail (a backend compiled in and selected); otherwise every read is unknown.
pub fn available() -> bool {
    backend().is_some()
}

/// Whether invites and responses can be checked (the Exchange backend only; IMAP has no calendar).
pub fn calendar() -> bool {
    cfg!(feature = "ews") && std::env::var("LUDIUS_MAIL_BACKEND").is_ok_and(|b| b == "exchange")
}

fn user_ok(user: &str) -> bool {
    !user.is_empty() && user.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
}

fn address(user: &str) -> String {
    format!("{user}@{}", domain())
}

pub fn read_state(user: &str, message_id: u64, folder: &str) -> Option<bool> {
    if !user_ok(user) || message_id == 0 {
        return None;
    }
    (backend()?.read_state)(&address(user), message_id, folder)
}

pub fn placed(user: &str, msgid: &str, folder: &str) -> Option<(usize, usize)> {
    if !user_ok(user) || msgid.is_empty() {
        return None;
    }
    let b = backend()?;
    let inbox = (b.msgids)(&address(user), "inbox")?;
    let there = (b.msgids)(&address(user), folder)?;
    Some((inbox.iter().filter(|m| *m == msgid).count(), there.iter().filter(|m| *m == msgid).count()))
}

fn query(user: &str, folder: &str, subject: Option<&str>, attachments: bool) -> Option<Vec<Message>> {
    if !user_ok(user) {
        return None;
    }
    (backend()?.query)(&address(user), folder, subject, attachments)
}

pub fn matching(user: &str, folder: &str, subject: &str, attachments: bool) -> Option<Vec<Message>> {
    query(user, folder, Some(subject), attachments)
}

pub fn newest(user: &str, folder: &str) -> Option<Vec<Message>> {
    query(user, folder, None, false)
}

/// Lowercase ASCII letters and digits only, after folding typographic quotes and dashes.
pub fn letters(s: &str) -> String {
    let s: String = s
        .chars()
        .map(|c| match c {
            '\u{2018}' | '\u{2019}' => '\'',
            '\u{201C}' | '\u{201D}' => '"',
            '\u{2013}' | '\u{2014}' => '-',
            c => c,
        })
        .collect();
    casefold(&s).chars().filter(|c| c.is_ascii_digit() || c.is_ascii_lowercase()).collect()
}

pub fn above_quote(body: &str) -> &str {
    let re = regex::Regex::new(r"(?m)^From:[^\n]*\r?\nSent:").unwrap();
    re.find(body).map(|m| &body[..m.start()]).unwrap_or(body)
}

fn local(addr: &str) -> (String, String) {
    let a = addr.trim().to_lowercase();
    match a.split_once('@') {
        Some((u, d)) => (u.to_string(), d.to_string()),
        None => (a, String::new()),
    }
}

pub fn present(address: &str, folder: &str, subject: &str, sender: &str, mine: &str) -> Option<Seen> {
    let (user, _) = local(address);
    let (me, _) = local(sender);
    let mut seen = Seen::new();
    for (u, f) in [(user, folder), (me, mine)] {
        for m in query(&u, f, Some(subject), false)? {
            seen.insert((u.clone(), f.to_string(), m.id));
        }
    }
    Some(seen)
}

fn recent(m: &Message, since_ms: i64) -> bool {
    m.submit_ms >= since_ms && m.delivery_ms / MINUTE_MS >= since_ms / MINUTE_MS && m.submit_ms <= m.delivery_ms + MINUTE_MS
}

#[allow(clippy::too_many_arguments)]
pub fn delivered(
    address: &str,
    folder: &str,
    subject: &str,
    body: &str,
    sender: &str,
    since_ms: i64,
    above: bool,
    before: Option<&Seen>,
    reply_to: &str,
    attachment: Option<&(String, String)>,
) -> Option<bool> {
    let address = address.trim().to_lowercase();
    let (user, dom) = local(&address);
    let (me, my_dom) = local(sender);
    let want = letters(body);
    let seen = before?;
    if dom != domain() || my_dom != domain() || want.is_empty() || since_ms <= 0 {
        return Some(false);
    }
    let sender_l = sender.to_lowercase();
    let ours = |m: &Message, u: &str, f: &str| {
        let attached = attachment.is_none() || (u, f) != (user.as_str(), folder) || m.attachments.as_deref() == attachment.map(std::slice::from_ref);
        let text = if above { above_quote(&m.body) } else { &m.body };
        let answers = reply_to.is_empty() || (m.in_reply_to == reply_to && m.prefix.trim().to_uppercase() == "RE:");
        !seen.contains(&(u.to_string(), f.to_string(), m.id))
            && m.sender == sender_l
            && recent(m, since_ms)
            && letters(text).contains(&want)
            && answers
            && attached
    };
    if !matching(&user, folder, subject, attachment.is_some())?.iter().any(|m| ours(m, &user, folder)) {
        return Some(false);
    }
    let sent: Vec<Message> = matching(&me, "sent", subject, false)?.into_iter().filter(|m| ours(m, &me, "sent")).collect();
    Some(!sent.is_empty() && sent.iter().all(|m| m.recipients == [address.clone()]))
}

#[allow(clippy::too_many_arguments)]
pub fn invited(address: &str, subject: &str, body: &str, organizer: &str, since_ms: i64, start_ms: i64, end_ms: i64, before: Option<&Seen>) -> Option<bool> {
    let (address, organizer) = (address.trim().to_lowercase(), organizer.trim().to_lowercase());
    let (user, dom) = local(&address);
    let (me, my_dom) = local(&organizer);
    let want = letters(body);
    let seen = before?;
    if dom != domain() || my_dom != domain() || user == me || since_ms <= 0 || start_ms >= end_ms || (!body.is_empty() && want.is_empty()) {
        return Some(false);
    }
    let ours = |m: &Message, u: &str, f: &str| {
        let mut others: Vec<&String> = Vec::new();
        for r in m.recipients.iter().filter(|r| **r != organizer) {
            if !others.contains(&r) {
                others.push(r);
            }
        }
        !seen.contains(&(u.to_string(), f.to_string(), m.id)) && (m.start_ms, m.end_ms) == (start_ms, end_ms) && others == [&address]
    };
    let request = matching(&user, "inbox", subject, false)?.iter().any(|m| {
        ours(m, &user, "inbox")
            && m.cls == MEETING_REQUEST
            && organizer == m.sender
            && m.sender == m.organizer
            && recent(m, since_ms)
            && letters(&m.body).contains(&want)
    });
    if !request {
        return Some(false);
    }
    Some(matching(&me, "calendar", subject, false)?.iter().any(|m| {
        ours(m, &me, "calendar") && m.cls == APPOINTMENT && m.organizer == organizer && m.created_ms / MINUTE_MS >= since_ms / MINUTE_MS
    }))
}

pub fn responded(address: &str, subject: &str, sender: &str, response: &str, since_ms: i64, before: Option<&Seen>) -> Option<bool> {
    let (address, sender) = (address.trim().to_lowercase(), sender.trim().to_lowercase());
    let (user, dom) = local(&address);
    let seen = before?;
    if dom != domain() || local(&sender).1 != domain() || address == sender || since_ms <= 0 {
        return Some(false);
    }
    let new: Vec<String> = matching(&user, "inbox", subject, false)?
        .into_iter()
        .filter(|m| !seen.contains(&(user.clone(), "inbox".into(), m.id)) && RESPONSES.contains(&m.cls.as_str()) && m.sender == sender && recent(m, since_ms))
        .map(|m| m.cls)
        .collect();
    Some(response_class(response).is_some_and(|c| new == [c]))
}

/// Deliver a planned mail inside the range (idempotent on msgid). Only within the mail domain.
pub fn deliver(sender: &str, sender_name: &str, to: &str, to_name: &str, subject: &str, body: &str, msgid: &str) -> bool {
    let addrs = [sender.trim().to_lowercase(), to.trim().to_lowercase()];
    let in_range = |a: &str| local(a).1 == domain() && user_ok(&local(a).0);
    if !addrs.iter().all(|a| in_range(a)) || addrs[0] == addrs[1] {
        return false;
    }
    let Some(have) = matching(&local(&addrs[1]).0, "inbox", subject, false) else { return false };
    if have.iter().any(|m| m.msgid == msgid) {
        return true;
    }
    backend().is_some_and(|b| (b.send)(&addrs[0], sender_name, &addrs[1], to_name, subject, body, msgid))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn letters_fold() {
        assert_eq!(letters("Hello, World’s – 42!"), "helloworlds42");
    }

    #[test]
    fn quote_split() {
        assert_eq!(above_quote("Thanks\nFrom: A\nSent: x\nold"), "Thanks\n");
        assert_eq!(above_quote("no quote"), "no quote");
    }

    #[test]
    fn unreadable_is_unknown() {
        // No baseline (it was unreadable) or no backend to read with: unknown, never "not delivered".
        assert_eq!(delivered("a@x", "inbox", "s", "b", "c@x", 1, false, None, "", None), None);
        assert_eq!(responded("a@x", "s", "c@x", "accept", 1, None), None);
        if !available() {
            let (seen, at) = (Seen::new(), |u: &str| format!("{u}@{}", domain()));
            assert_eq!(delivered(&at("a"), "inbox", "s", "b", &at("c"), 1, false, Some(&seen), "", None), None);
            assert_eq!(invited(&at("a"), "s", "", &at("c"), 1, 1, 2, Some(&seen)), None);
        }
    }
}
