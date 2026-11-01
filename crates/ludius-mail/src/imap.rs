//! The lightweight mail backend: IMAPS reads as a Dovecot master user (`<address>*<master>`) and SMTP to the range's
//! Postfix for planned deliveries. No calendar: invites and responses read as unknown.
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::Arc;
use std::time::Duration;

use base64::Engine;
use mail_parser::{Address, MessageParser, MimeHeaders};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, ServerName};
use sha2::{Digest, Sha256};

use crate::{Message, casefold, env, root};

const PAGE: usize = 1000;
const LISTED: usize = 20;
const MAX_LINE: u64 = 64 << 10;
const MAX_LITERAL: usize = 50 << 20;

/// (uid, flags, internaldate, first literal) of one fetched message.
type Fetched = (u32, String, String, Vec<u8>);
type Tls = rustls::StreamOwned<rustls::ClientConnection, TcpStream>;

fn host_port(var: &str, port: u16) -> Option<(String, u16)> {
    let v = env(var);
    if v.is_empty() {
        return None;
    }
    match v.rsplit_once(':') {
        Some((h, p)) if !h.contains(':') => Some((h.to_string(), p.parse().ok()?)),
        _ => Some((v, port)),
    }
}

fn tcp(host: &str, port: u16) -> Option<TcpStream> {
    let s = (host, port).to_socket_addrs().ok()?.find_map(|a| TcpStream::connect_timeout(&a, Duration::from_secs(10)).ok())?;
    s.set_read_timeout(Some(Duration::from_secs(30))).ok()?;
    s.set_write_timeout(Some(Duration::from_secs(30))).ok()?;
    Some(s)
}

/// TLS to the range server, trusting only the range CA (LUDIUS_IMAP_CA, else the bring-up's build/range-ca.crt).
fn tls(host: &str, port: u16) -> Option<Tls> {
    let ca = std::env::var("LUDIUS_IMAP_CA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| root().join("infra/ansible/build/range-ca.crt"));
    let mut roots = rustls::RootCertStore::empty();
    for c in CertificateDer::pem_file_iter(ca).ok()? {
        roots.add(c.ok()?).ok()?;
    }
    let cfg = rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .ok()?
        .with_root_certificates(roots)
        .with_no_client_auth();
    let conn = rustls::ClientConnection::new(Arc::new(cfg), ServerName::try_from(host.to_string()).ok()?).ok()?;
    Some(rustls::StreamOwned::new(conn, tcp(host, port)?))
}

/// One server response: its text (literals cut out) and the literals in order.
#[derive(Debug, Default)]
struct Resp {
    text: String,
    lits: Vec<Vec<u8>>,
}

/// `{n}` at the end of a line: a literal of n bytes follows.
fn literal_len(line: &[u8]) -> Option<(usize, usize)> {
    let close = line.len().checked_sub(1).filter(|&i| line[i] == b'}')?;
    let open = line[..close].iter().rposition(|&b| b == b'{')?;
    let n = std::str::from_utf8(&line[open + 1..close]).ok()?.parse().ok()?;
    Some((open, n))
}

fn response(r: &mut impl BufRead) -> Option<Resp> {
    let mut out = Resp::default();
    let mut text = Vec::new();
    loop {
        let mut line = Vec::new();
        r.by_ref().take(MAX_LINE).read_until(b'\n', &mut line).ok()?;
        let line = line.strip_suffix(b"\r\n")?;
        match literal_len(line) {
            Some((open, n)) if n <= MAX_LITERAL => {
                text.extend_from_slice(&line[..open]);
                let mut lit = vec![0; n];
                r.read_exact(&mut lit).ok()?;
                out.lits.push(lit);
            }
            Some(_) => return None,
            None => {
                text.extend_from_slice(line);
                out.text = String::from_utf8_lossy(&text).into_owned();
                return Some(out);
            }
        }
    }
}

/// An IMAP string argument: quoted when plain ASCII, else a non-synchronizing literal (LITERAL+).
fn astring(s: &str) -> Vec<u8> {
    if s.bytes().all(|b| (0x20..0x7f).contains(&b)) {
        format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\"")).into_bytes()
    } else {
        [format!("{{{}+}}\r\n", s.len()).as_bytes(), s.as_bytes()].concat()
    }
}

struct Imap {
    r: BufReader<Tls>,
    tag: u32,
}

impl Imap {
    /// Logged in to `mailbox` (an address) as the master user; None = unknown.
    fn open(mailbox: &str) -> Option<Self> {
        let (host, port) = host_port("LUDIUS_IMAP_HOST", 993)?;
        let (user, secret) = (env("LUDIUS_IMAP_USER"), env("LUDIUS_IMAP_PASSWORD_FILE"));
        if user.is_empty() || secret.is_empty() || mailbox.contains(['*', '\r', '\n']) {
            return None;
        }
        let password = std::fs::read_to_string(secret).ok()?.trim().to_string();
        let mut c = Imap { r: BufReader::new(tls(&host, port)?), tag: 0 };
        response(&mut c.r)?.text.starts_with("* OK").then_some(())?;
        c.run(&[b"LOGIN ".as_slice(), &astring(&format!("{mailbox}*{user}")), b" ", &astring(&password)].concat())?;
        Some(c)
    }

    /// Untagged responses of a command that ended OK; None on NO/BAD or a broken connection.
    fn run(&mut self, cmd: &[u8]) -> Option<Vec<Resp>> {
        self.tag += 1;
        let tag = format!("a{} ", self.tag);
        let w = self.r.get_mut();
        w.write_all(&[tag.as_bytes(), cmd, b"\r\n"].concat()).ok()?;
        w.flush().ok()?;
        let mut out = Vec::new();
        loop {
            let r = response(&mut self.r)?;
            if let Some(rest) = r.text.strip_prefix(&tag) {
                return rest.starts_with("OK").then_some(out);
            }
            if r.text.starts_with('+') {
                return None;
            }
            out.push(r);
        }
    }

    /// Read-only select; the folder's UIDVALIDITY.
    fn examine(&mut self, name: &str) -> Option<u32> {
        let got = self.run(&[b"EXAMINE ".as_slice(), &astring(name)].concat())?;
        let re = regex::Regex::new(r"\[UIDVALIDITY (\d+)\]").unwrap();
        got.iter().find_map(|r| re.captures(&r.text).and_then(|c| c[1].parse().ok()))
    }

    fn search(&mut self, criteria: &[u8]) -> Option<Vec<u32>> {
        let got = self.run(&[b"UID SEARCH ".as_slice(), criteria].concat())?;
        let mut uids: Vec<u32> = got
            .iter()
            .filter_map(|r| r.text.strip_prefix("* SEARCH"))
            .flat_map(|t| t.split_whitespace().map(str::parse))
            .collect::<Result<_, _>>()
            .ok()?;
        uids.sort_unstable();
        Some(uids)
    }

    /// (uid, flags, internaldate, first literal) per message, exactly one per uid asked.
    fn fetch(&mut self, uids: &[u32], items: &str) -> Option<Vec<Fetched>> {
        let set: Vec<String> = uids.iter().map(u32::to_string).collect();
        let got = self.run(format!("UID FETCH {} ({items})", set.join(",")).as_bytes())?;
        let uid = regex::Regex::new(r"\bUID (\d+)").unwrap();
        let flags = regex::Regex::new(r"\bFLAGS \(([^)]*)\)").unwrap();
        let date = regex::Regex::new(r#"\bINTERNALDATE "([^"]+)""#).unwrap();
        let mut out = Vec::new();
        for mut r in got.into_iter().filter(|r| r.text.starts_with("* ") && r.text.contains(" FETCH ")) {
            let u: u32 = uid.captures(&r.text)?[1].parse().ok()?;
            let f = flags.captures(&r.text).map(|c| c[1].to_string()).unwrap_or_default();
            let d = date.captures(&r.text).map(|c| c[1].to_string()).unwrap_or_default();
            let lit = if r.lits.is_empty() { vec![] } else { r.lits.swap_remove(0) };
            out.push((u, f, d, lit));
        }
        out.sort_by_key(|x| x.0);
        let mut want = uids.to_vec();
        want.sort_unstable();
        (out.iter().map(|x| x.0).collect::<Vec<_>>() == want).then_some(out)
    }

    /// (attributes, delimiter, name as on the server) of every mailbox; a line that does not parse makes it unknown.
    fn list(&mut self, extra: &str) -> Option<Vec<(String, String, String)>> {
        let got = self.run(format!("LIST {extra}\"\" \"*\"").as_bytes())?;
        let re = regex::Regex::new(r#"^\* LIST \(([^)]*)\) (?:"((?:[^"\\]|\\.)*)"|NIL) (?:"((?:[^"\\]|\\.)*)"|([^\s"(){}]+)|)$"#).unwrap();
        let unq = |s: &str| s.replace("\\\"", "\"").replace("\\\\", "\\");
        got.iter()
            .filter(|r| r.text.starts_with("* LIST "))
            .map(|r| {
                let c = re.captures(&r.text)?;
                let name = match c.get(3).or(c.get(4)) {
                    Some(n) => unq(n.as_str()),
                    None => String::from_utf8(r.lits.first()?.clone()).ok()?,
                };
                Some((c[1].to_string(), c.get(2).map(|d| unq(d.as_str())).unwrap_or_default(), name))
            })
            .collect()
    }

    /// The server folder for a check folder: inbox, sent (the \Sent special-use mailbox); None for anything else.
    fn folder(&mut self, folder: &str) -> Option<String> {
        match folder {
            "inbox" => Some("INBOX".into()),
            "sent" => {
                let sent: Vec<String> = self.list("(SPECIAL-USE) ")?.into_iter().filter(|(a, _, _)| a.split(' ').any(|x| x == "\\Sent")).map(|x| x.2).collect();
                (sent.len() == 1).then(|| sent[0].clone())
            }
            _ => None,
        }
    }

    fn logout(mut self) {
        let _ = self.run(b"LOGOUT");
    }
}

pub fn key(uidvalidity: u32, uid: u32) -> u64 {
    let h = Sha256::digest(format!("{uidvalidity}:{uid}").as_bytes());
    u64::from_be_bytes(h[..8].try_into().unwrap()) >> 4
}

fn hex(data: &[u8]) -> String {
    Sha256::digest(data).iter().map(|b| format!("{b:02x}")).collect()
}

fn addresses(a: Option<&Address>) -> Vec<String> {
    a.map(|a| a.iter().map(|x| x.address().map(|s| s.trim().to_lowercase()).unwrap_or_else(|| "?".into())).collect()).unwrap_or_default()
}

/// (prefix, normalized subject) as MAPI splits them: one prefix of 1-3 letters, a colon and a space ("RE: ").
pub fn split_subject(s: &str) -> (String, String) {
    let re = regex::Regex::new(r"^(\p{L}{1,3}: )(.*)$").unwrap();
    match re.captures(s) {
        Some(c) => (c[1].to_string(), c[2].to_string()),
        None => (String::new(), s.to_string()),
    }
}

fn internal_ms(s: &str) -> i64 {
    chrono::DateTime::parse_from_str(s.trim(), "%d-%b-%Y %H:%M:%S %z").map(|d| d.timestamp_millis()).unwrap_or(0)
}

fn header(m: &mail_parser::Message, name: &str) -> String {
    m.header_raw(name).map(|v| v.split_whitespace().collect::<Vec<_>>().join(" ")).unwrap_or_default()
}

/// One fetched message; attachments only when asked (None when one is itself a message: not a file).
pub fn parse(uidvalidity: u32, uid: u32, flags: &str, internal: &str, raw: &[u8], with_attachments: bool) -> Option<Message> {
    let m = MessageParser::default().parse(raw)?;
    let mut recipients: Vec<String> = [m.to(), m.cc(), m.bcc()].into_iter().flat_map(addresses).collect();
    recipients.sort();
    let (prefix, subject) = split_subject(m.subject().unwrap_or("").trim());
    let delivered = internal_ms(internal);
    let parts: Vec<_> = m.attachments().collect();
    let attachments = with_attachments
        .then(|| {
            let mut got = Vec::new();
            for a in &parts {
                if a.is_message() {
                    return None;
                }
                got.push((casefold(a.attachment_name().unwrap_or("").trim()), hex(a.contents())));
            }
            got.sort();
            Some(got)
        })
        .flatten();
    Some(Message {
        sender: addresses(m.from()).into_iter().next().filter(|a| a != "?").unwrap_or_default(),
        submit_ms: m.date().map(|d| d.to_timestamp() * 1000).unwrap_or(0),
        delivery_ms: delivered,
        body: m.body_text(0).map(|b| b.replace("\r\n", "\n")).unwrap_or_default(),
        recipients,
        id: key(uidvalidity, uid),
        cls: "IPM.Note".into(),
        created_ms: delivered,
        subject,
        prefix,
        msgid: header(&m, "Message-ID"),
        in_reply_to: header(&m, "In-Reply-To"),
        read: flags.split_whitespace().any(|f| f.eq_ignore_ascii_case("\\Seen")),
        attachments,
        files: parts.iter().map(|a| a.attachment_name().unwrap_or("").trim().to_string()).collect(),
        ..Default::default()
    })
}

pub fn query(mailbox: &str, folder: &str, subject: Option<&str>, with_attachments: bool) -> Option<Vec<Message>> {
    let mut c = Imap::open(mailbox)?;
    let name = c.folder(folder)?;
    let validity = c.examine(&name)?;
    let uids = match subject {
        Some(s) => c.search(&[b"CHARSET UTF-8 SUBJECT ".as_slice(), &astring(s)].concat()).filter(|u| u.len() <= PAGE)?,
        None => {
            let all = c.search(b"ALL")?;
            all[all.len().saturating_sub(LISTED)..].to_vec()
        }
    };
    if uids.is_empty() {
        c.logout();
        return Some(vec![]);
    }
    let got = c.fetch(&uids, "UID FLAGS INTERNALDATE BODY.PEEK[]")?;
    c.logout();
    let mut out = Vec::new();
    for (uid, flags, date, raw) in got {
        let m = parse(validity, uid, &flags, &date, &raw, with_attachments)?;
        if subject.is_none_or(|s| m.subject == s) {
            out.push(m);
        }
    }
    out.reverse();
    Some(out)
}

/// A mailbox name in IMAP's modified UTF-7 (RFC 3501 5.1.3: `&...-` is base64 UTF-16 with `,` for `/`).
pub fn mutf7(s: &str) -> Option<String> {
    let mut out = String::new();
    let mut rest = s;
    while let Some(i) = rest.find('&') {
        out.push_str(&rest[..i]);
        let end = rest[i..].find('-')? + i;
        let enc = &rest[i + 1..end];
        if enc.is_empty() {
            out.push('&');
        } else {
            let raw = base64::engine::general_purpose::STANDARD_NO_PAD.decode(enc.replace(',', "/")).ok()?;
            let (pairs, odd) = raw.as_chunks::<2>();
            if !odd.is_empty() {
                return None;
            }
            let units: Vec<u16> = pairs.iter().map(|c| u16::from_be_bytes(*c)).collect();
            out.push_str(&String::from_utf16(&units).ok()?);
        }
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    Some(out)
}

/// Message-ids in the inbox, or in the one folder named `folder` anywhere under it (none there: empty).
pub fn msgids(mailbox: &str, folder: &str) -> Option<Vec<String>> {
    let mut c = Imap::open(mailbox)?;
    let name = if folder == "inbox" {
        "INBOX".to_string()
    } else {
        let all = c.list("")?;
        let under: Vec<String> = all
            .into_iter()
            .filter(|(attrs, d, n)| {
                !d.is_empty()
                    && !attrs.contains("\\Noselect")
                    && n.starts_with(&format!("INBOX{d}"))
                    && mutf7(n.rsplit(d.as_str()).next().unwrap_or("")).is_some_and(|leaf| casefold(&leaf) == casefold(folder))
            })
            .map(|x| x.2)
            .collect();
        match under.len() {
            0 => {
                c.logout();
                return Some(vec![]);
            }
            1 => under[0].clone(),
            _ => return None,
        }
    };
    c.examine(&name)?;
    let uids = c.search(b"ALL").filter(|u| u.len() <= PAGE)?;
    if uids.is_empty() {
        c.logout();
        return Some(vec![]);
    }
    let got = c.fetch(&uids, "UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")?;
    c.logout();
    // No Message-ID header: the server returns a blank block, which is "no id", not unreadable.
    got.iter()
        .map(|(_, _, _, raw)| if raw.trim_ascii().is_empty() { Some(String::new()) } else { MessageParser::default().parse_headers(raw).map(|m| header(&m, "Message-ID")) })
        .collect()
}

pub fn read_state(mailbox: &str, k: u64, folder: &str) -> Option<bool> {
    let mut c = Imap::open(mailbox)?;
    let name = c.folder(folder)?;
    let validity = c.examine(&name)?;
    let uids = c.search(b"ALL").filter(|u| u.len() <= PAGE)?;
    let hit: Vec<u32> = uids.into_iter().filter(|u| key(validity, *u) == k).collect();
    if hit.len() != 1 {
        return None;
    }
    let got = c.fetch(&hit, "UID FLAGS")?;
    c.logout();
    Some(got[0].1.split_whitespace().any(|f| f.eq_ignore_ascii_case("\\Seen")))
}

fn mime_word(s: &str) -> String {
    if s.bytes().all(|b| (0x20..0x7f).contains(&b)) {
        s.to_string()
    } else {
        format!("=?UTF-8?B?{}?=", base64::engine::general_purpose::STANDARD.encode(s))
    }
}

fn mailbox(name: &str, addr: &str) -> String {
    let n = name.replace(['"', '\\', '\r', '\n'], "");
    if n.is_empty() { format!("<{addr}>") } else if n.is_ascii() { format!("\"{n}\" <{addr}>") } else { format!("{} <{addr}>", mime_word(&n)) }
}

/// The RFC 5322 text of a planned mail; Postfix adds Date (its clock, the range's) and Received.
pub fn compose(sender: &str, sender_name: &str, to: &str, to_name: &str, subject: &str, body: &str, msgid: &str) -> String {
    let b64 = base64::engine::general_purpose::STANDARD.encode(body.replace("\r\n", "\n").replace('\n', "\r\n"));
    let lines: Vec<&str> = b64.as_bytes().chunks(76).map(|c| std::str::from_utf8(c).unwrap()).collect();
    format!(
        "From: {}\r\nTo: {}\r\nSubject: {}\r\nMessage-ID: {}\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n{}\r\n",
        mailbox(sender_name, sender),
        mailbox(to_name, to),
        mime_word(&subject.replace(['\r', '\n'], " ")),
        msgid.replace(['\r', '\n'], ""),
        lines.join("\r\n")
    )
}

fn smtp_reply(r: &mut impl BufRead) -> Option<u16> {
    loop {
        let mut line = String::new();
        r.by_ref().take(MAX_LINE).read_line(&mut line).ok()?;
        let code: u16 = line.get(..3)?.parse().ok()?;
        if line.as_bytes().get(3) != Some(&b'-') {
            return Some(code);
        }
    }
}

/// Deliver a planned mail through the range's Postfix (LUDIUS_SMTP_HOST, else the IMAP host on port 25). The caller
/// has checked both addresses are in the range domain; Postfix itself delivers only to that domain.
pub fn send(sender: &str, sender_name: &str, to: &str, to_name: &str, subject: &str, body: &str, msgid: &str) -> bool {
    let Some((host, port)) = host_port("LUDIUS_SMTP_HOST", 25).or_else(|| host_port("LUDIUS_IMAP_HOST", 993).map(|(h, _)| (h, 25))) else { return false };
    if [sender, to].iter().any(|a| a.contains(['<', '>', '\r', '\n', ' '])) {
        return false;
    }
    let Some(s) = tcp(&host, port) else { return false };
    let Ok(w) = s.try_clone() else { return false };
    let (mut r, mut w) = (BufReader::new(s), w);
    let data = compose(sender, sender_name, to, to_name, subject, body, msgid);
    let mut step = |cmd: &str, want: u16| -> Option<()> {
        if !cmd.is_empty() {
            w.write_all(cmd.as_bytes()).ok()?;
        }
        (smtp_reply(&mut r)? == want).then_some(())
    };
    let ok = (|| {
        step("", 220)?;
        step("EHLO ludius-controller\r\n", 250)?;
        step(&format!("MAIL FROM:<{sender}>\r\n"), 250)?;
        step(&format!("RCPT TO:<{to}>\r\n"), 250)?;
        step("DATA\r\n", 354)?;
        step(&format!("{data}.\r\n"), 250)
    })()
    .is_some();
    let _ = w.write_all(b"QUIT\r\n");
    ok
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literals_and_tags() {
        let mut r = std::io::Cursor::new(b"* 1 FETCH (UID 7 FLAGS (\\Seen) BODY[] {5}\r\nhello)\r\na1 OK done\r\n".to_vec());
        let first = response(&mut r).unwrap();
        assert_eq!(first.text, "* 1 FETCH (UID 7 FLAGS (\\Seen) BODY[] )");
        assert_eq!(first.lits, vec![b"hello".to_vec()]);
        assert_eq!(response(&mut r).unwrap().text, "a1 OK done");
        assert_eq!(astring("a\"b"), b"\"a\\\"b\"");
        assert_eq!(astring("Möte"), "{5+}\r\nMöte".as_bytes());
    }

    #[test]
    fn subjects_split_like_mapi() {
        assert_eq!(split_subject("RE: Lead times"), ("RE: ".into(), "Lead times".into()));
        assert_eq!(split_subject("Agenda: Q3"), ("".into(), "Agenda: Q3".into()));
        assert_eq!(mutf7("M&APY-te &- Q3").as_deref(), Some("Möte & Q3"));
    }

    #[test]
    fn composed_mail_parses_back() {
        let raw = compose("bek@x.test", "Bea Ek", "aberg@x.test", "", "Möte – Q3", "Hej\nsee you", "<a1@x.test>");
        let m = parse(3, 9, "\\Seen", "01-Nov-2026 10:00:00 +0000", raw.as_bytes(), true).unwrap();
        assert_eq!((m.sender.as_str(), m.recipients.clone()), ("bek@x.test", vec!["aberg@x.test".to_string()]));
        assert_eq!((m.subject.as_str(), m.msgid.as_str()), ("Möte – Q3", "<a1@x.test>"));
        assert_eq!(m.body.trim_end(), "Hej\nsee you");
        assert_eq!(m.delivery_ms, 1_793_527_200_000);
        assert!(m.read);
        assert_eq!(m.attachments, Some(vec![]));
        assert_eq!(m.id, key(3, 9));
    }
}
