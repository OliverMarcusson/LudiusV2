//! Mail outcome checks: what a mailbox holds, read state, and planned deliveries (EWS backend).
use std::collections::BTreeSet;
use std::sync::OnceLock;

pub use crate::ews::Message;
use crate::ews;
use crate::template::casefold;

pub const MINUTE_MS: i64 = 60_000;
pub const MEETING_REQUEST: &str = "IPM.Schedule.Meeting.Request";
pub const APPOINTMENT: &str = "IPM.Appointment";

/// (user, folder, item id) seen before a task ran.
pub type Seen = BTreeSet<(String, String, u64)>;

/// LUDIUS_MAIL_DOMAIN, else the checkout's .env, else ludius.internal.
pub fn domain() -> &'static str {
    static D: OnceLock<String> = OnceLock::new();
    D.get_or_init(|| {
        std::env::var("LUDIUS_MAIL_DOMAIN")
            .ok()
            .filter(|d| !d.is_empty())
            .or_else(|| {
                let env = std::fs::read_to_string(crate::paths::root().join(".env")).ok()?;
                env.lines().filter_map(|l| l.split_once('=')).find(|(k, _)| k.trim() == "LUDIUS_MAIL_DOMAIN").map(|(_, v)| v.trim().to_string())
            })
            .filter(|d| !d.is_empty())
            .unwrap_or_else(|| "ludius.internal".into())
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

fn exchange() -> bool {
    std::env::var("LUDIUS_MAIL_BACKEND").unwrap_or_else(|_| "grommunio".into()) == "exchange"
}

fn user_ok(user: &str) -> bool {
    !user.is_empty() && user.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
}

fn address(user: &str) -> String {
    format!("{user}@{}", domain())
}

pub fn read_state(user: &str, message_id: u64, folder: &str) -> Option<bool> {
    if !user_ok(user) || message_id == 0 || !exchange() {
        // ponytail: the grommunio (Hyper-V) backend is not ported; it reads as unknown.
        return None;
    }
    ews::read_state(&address(user), message_id, folder)
}

pub fn placed(user: &str, msgid: &str, folder: &str) -> Option<(usize, usize)> {
    if !user_ok(user) || msgid.is_empty() || !exchange() {
        return None;
    }
    let inbox = ews::msgids(&address(user), "inbox")?;
    let there = ews::msgids(&address(user), folder)?;
    Some((inbox.iter().filter(|m| *m == msgid).count(), there.iter().filter(|m| *m == msgid).count()))
}

fn query(user: &str, folder: &str, subject: Option<&str>, attachments: bool) -> Option<Vec<Message>> {
    if !user_ok(user) || !exchange() {
        return None;
    }
    ews::query(&address(user), folder, subject, attachments)
}

pub fn messages(user: &str, folder: &str, subject: &str, attachments: bool) -> Vec<Message> {
    query(user, folder, Some(subject), attachments).unwrap_or_default()
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

fn above_quote(body: &str) -> &str {
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
) -> bool {
    let address = address.trim().to_lowercase();
    let (user, dom) = local(&address);
    let (me, my_dom) = local(sender);
    let want = letters(body);
    let Some(seen) = before else { return false };
    if dom != domain() || my_dom != domain() || want.is_empty() || since_ms <= 0 {
        return false;
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
    if !messages(&user, folder, subject, attachment.is_some()).iter().any(|m| ours(m, &user, folder)) {
        return false;
    }
    let sent: Vec<Message> = messages(&me, "sent", subject, false).into_iter().filter(|m| ours(m, &me, "sent")).collect();
    !sent.is_empty() && sent.iter().all(|m| m.recipients == [address.clone()])
}

#[allow(clippy::too_many_arguments)]
pub fn invited(address: &str, subject: &str, body: &str, organizer: &str, since_ms: i64, start_ms: i64, end_ms: i64, before: Option<&Seen>) -> bool {
    let (address, organizer) = (address.trim().to_lowercase(), organizer.trim().to_lowercase());
    let (user, dom) = local(&address);
    let (me, my_dom) = local(&organizer);
    let want = letters(body);
    let Some(seen) = before else { return false };
    if dom != domain() || my_dom != domain() || user == me || since_ms <= 0 || start_ms >= end_ms || (!body.is_empty() && want.is_empty()) {
        return false;
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
    let request = messages(&user, "inbox", subject, false).iter().any(|m| {
        ours(m, &user, "inbox")
            && m.cls == MEETING_REQUEST
            && organizer == m.sender
            && m.sender == m.organizer
            && recent(m, since_ms)
            && letters(&m.body).contains(&want)
    });
    request
        && messages(&me, "calendar", subject, false).iter().any(|m| {
            ours(m, &me, "calendar") && m.cls == APPOINTMENT && m.organizer == organizer && m.created_ms / MINUTE_MS >= since_ms / MINUTE_MS
        })
}

pub fn responded(address: &str, subject: &str, sender: &str, response: &str, since_ms: i64, before: Option<&Seen>) -> bool {
    let (address, sender) = (address.trim().to_lowercase(), sender.trim().to_lowercase());
    let (user, dom) = local(&address);
    let Some(seen) = before else { return false };
    if dom != domain() || local(&sender).1 != domain() || address == sender || since_ms <= 0 {
        return false;
    }
    let new: Vec<String> = messages(&user, "inbox", subject, false)
        .into_iter()
        .filter(|m| !seen.contains(&(user.clone(), "inbox".into(), m.id)) && RESPONSES.contains(&m.cls.as_str()) && m.sender == sender && recent(m, since_ms))
        .map(|m| m.cls)
        .collect();
    response_class(response).is_some_and(|c| new == [c])
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
    exchange() && ews::send(&addrs[0], sender_name, &addrs[1], to_name, subject, body, msgid)
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
    fn delivered_needs_a_baseline() {
        assert!(!delivered("a@x", "inbox", "s", "b", "c@x", 1, false, None, "", None));
    }
}
