//! Exchange Web Services (impersonation) for mail outcome checks and planned deliveries.
use std::time::Duration;

use base64::Engine;
use roxmltree::{Document, Node};
use sha2::{Digest, Sha256};

use crate::{Message, casefold, env, root};

const T: &str = "http://schemas.microsoft.com/exchange/services/2006/types";
const M: &str = "http://schemas.microsoft.com/exchange/services/2006/messages";
const PAGE: usize = 1000;
const LISTED: usize = 20;
const FIELDS: [&str; 20] = [
    "item:ItemClass", "item:Subject", "item:DateTimeReceived", "item:DateTimeSent", "item:DateTimeCreated",
    "item:TextBody", "item:InReplyTo", "message:Sender", "message:ToRecipients", "message:CcRecipients",
    "message:BccRecipients", "message:InternetMessageId", "message:IsRead", "calendar:Start", "calendar:End",
    "calendar:Organizer", "calendar:RequiredAttendees", "calendar:OptionalAttendees", "calendar:Resources",
    "item:Attachments",
];

/// None for any other folder: unknown, never silently the inbox.
fn folder_id(folder: &str) -> Option<&'static str> {
    match folder {
        "inbox" => Some("inbox"),
        "sent" => Some("sentitems"),
        "calendar" => Some("calendar"),
        _ => None,
    }
}

pub fn esc(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

fn quoteattr(s: &str) -> String {
    format!("\"{}\"", esc(s).replace('"', "&quot;").replace('\n', "&#10;").replace('\r', "&#13;").replace('\t', "&#9;"))
}

/// The SOAP response text when every response message is Success; None = unknown.
pub fn call(mailbox: &str, body: &str) -> Option<String> {
    let (url, user, secret) = (env("LUDIUS_EWS_URL"), env("LUDIUS_EWS_USER"), env("LUDIUS_EWS_PASSWORD_FILE"));
    if !url.starts_with("https://") || user.is_empty() || secret.is_empty() {
        return None;
    }
    let ca = std::env::var("LUDIUS_EWS_CA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| root().join("infra/ansible/build/range-ca.crt"));
    let envelope = format!(
        r#"<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:t="{T}" xmlns:m="{M}"><soap:Header><t:RequestServerVersion Version="Exchange2016"/><t:ExchangeImpersonation><t:ConnectingSID><t:PrimarySmtpAddress>{}</t:PrimarySmtpAddress></t:ConnectingSID></t:ExchangeImpersonation></soap:Header><soap:Body>{body}</soap:Body></soap:Envelope>"#,
        esc(mailbox)
    );
    let password = std::fs::read_to_string(secret).ok()?.trim().to_string();
    let cert = reqwest::Certificate::from_pem(&std::fs::read(ca).ok()?).ok()?;
    let client = reqwest::blocking::Client::builder()
        .tls_certs_only([cert])
        .timeout(Duration::from_secs(30))
        .build()
        .ok()?;
    let r = client
        .post(url)
        .basic_auth(user, Some(password))
        .header("Content-Type", "text/xml; charset=utf-8")
        .body(envelope)
        .send()
        .ok()?;
    if r.status() != 200 {
        return None;
    }
    let text = r.text().ok()?;
    let ok = {
        let doc = Document::parse(&text).ok()?;
        let msgs = doc.descendants().find(|n| is(n, M, "ResponseMessages"))?;
        let all: Vec<_> = elements(msgs).collect();
        !all.is_empty() && all.iter().all(|m| m.attribute("ResponseClass") == Some("Success"))
    };
    ok.then_some(text)
}

fn is(n: &Node, ns: &str, name: &str) -> bool {
    n.is_element() && n.tag_name().namespace() == Some(ns) && n.tag_name().name() == name
}

fn elements<'a, 'i>(n: Node<'a, 'i>) -> impl Iterator<Item = Node<'a, 'i>> {
    n.children().filter(Node::is_element)
}

fn child<'a, 'i>(n: Node<'a, 'i>, ns: &str, name: &str) -> Option<Node<'a, 'i>> {
    n.children().find(|c| is(c, ns, name))
}

fn text(n: Node, ns: &str, name: &str) -> String {
    child(n, ns, name).and_then(|c| c.text()).unwrap_or("").to_string()
}

/// The response messages of a validated `call` result.
fn messages<'a, 'i>(doc: &'a Document<'i>) -> Vec<Node<'a, 'i>> {
    doc.descendants().find(|n| is(n, M, "ResponseMessages")).map(|m| elements(m).collect()).unwrap_or_default()
}

/// FindItem; returns the response text (items are re-found by the caller) or None.
fn find(mailbox: &str, folder: &str, subject: Option<&str>, fields: &str, whole: bool, parent: &str) -> Option<String> {
    let restrict = subject.map(|s| format!(
        r#"<m:Restriction><t:Contains ContainmentMode="Substring" ContainmentComparison="IgnoreCase"><t:FieldURI FieldURI="item:Subject"/><t:Constant Value={}/></t:Contains></m:Restriction>"#,
        quoteattr(s))).unwrap_or_default();
    let whole = whole || subject.is_some();
    let size = if whole { PAGE } else { LISTED };
    let parent = if parent.is_empty() { format!(r#"<t:DistinguishedFolderId Id="{}"/>"#, folder_id(folder)?) } else { parent.to_string() };
    let text = call(mailbox, &format!(
        r#"<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>{fields}</m:ItemShape><m:IndexedPageItemView MaxEntriesReturned="{size}" Offset="0" BasePoint="Beginning"/>{restrict}<m:SortOrder><t:FieldOrder Order="Descending"><t:FieldURI FieldURI="item:DateTimeReceived"/></t:FieldOrder></m:SortOrder><m:ParentFolderIds>{parent}</m:ParentFolderIds></m:FindItem>"#))?;
    let ok = {
        let doc = Document::parse(&text).ok()?;
        let root = child(*messages(&doc).first()?, M, "RootFolder")?;
        !whole || root.attribute("IncludesLastItemInRange") == Some("true")
    };
    ok.then_some(text)
}

fn found_items<'a, 'i>(doc: &'a Document<'i>) -> Vec<Node<'a, 'i>> {
    messages(doc)
        .first()
        .and_then(|m| child(*m, M, "RootFolder"))
        .and_then(|r| child(r, T, "Items"))
        .map(|i| elements(i).collect())
        .unwrap_or_default()
}

pub fn key(item_id: &str) -> u64 {
    let h = Sha256::digest(item_id.as_bytes());
    let hex: String = h.iter().map(|b| format!("{b:02x}")).collect();
    u64::from_str_radix(&hex[..15], 16).unwrap()
}

fn item_id(el: Node) -> String {
    child(el, T, "ItemId").and_then(|i| i.attribute("Id")).unwrap_or("").to_string()
}

fn ms(text: &str) -> i64 {
    chrono::DateTime::parse_from_rfc3339(text).map(|d| d.timestamp_millis()).unwrap_or(0)
}

fn addresses(el: Option<Node>) -> Vec<String> {
    el.map(|e| {
        e.descendants()
            .filter(|n| is(n, T, "Mailbox"))
            .map(|mb| {
                let addr = text(mb, T, "EmailAddress");
                let kind = child(mb, T, "RoutingType").and_then(|c| c.text()).unwrap_or("SMTP");
                if !addr.is_empty() && kind == "SMTP" { addr.trim().to_lowercase() } else { "?".into() }
            })
            .collect()
    })
    .unwrap_or_default()
}

pub fn parse(el: Node) -> Message {
    let mut ext = std::collections::HashMap::new();
    for x in el.children().filter(|c| is(c, T, "ExtendedProperty")) {
        if let Some(tag) = child(x, T, "ExtendedFieldURI") {
            let n = u32::from_str_radix(tag.attribute("PropertyTag").unwrap_or("0").trim_start_matches("0x"), 16).unwrap_or(0);
            ext.insert(format!("0x{n:04X}"), text(x, T, "Value"));
        }
    }
    let mut recipients: Vec<String> = ["ToRecipients", "CcRecipients", "BccRecipients", "RequiredAttendees", "OptionalAttendees", "Resources"]
        .iter()
        .flat_map(|n| addresses(child(el, T, n)))
        .collect();
    recipients.sort();
    let first = |n: &str| addresses(child(el, T, n)).into_iter().next().filter(|a| a != "?").unwrap_or_default();
    Message {
        sender: first("Sender"),
        submit_ms: ms(&text(el, T, "DateTimeSent")),
        delivery_ms: ms(&text(el, T, "DateTimeReceived")),
        body: text(el, T, "TextBody"),
        recipients,
        id: key(&item_id(el)),
        cls: text(el, T, "ItemClass"),
        start_ms: ms(&text(el, T, "Start")),
        end_ms: ms(&text(el, T, "End")),
        created_ms: ms(&text(el, T, "DateTimeCreated")),
        organizer: first("Organizer"),
        subject: ext.get("0x0E1D").cloned().unwrap_or_default(),
        prefix: ext.get("0x003D").cloned().unwrap_or_default(),
        msgid: text(el, T, "InternetMessageId").trim().into(),
        in_reply_to: text(el, T, "InReplyTo").trim().into(),
        read: text(el, T, "IsRead") == "true",
        attachments: None,
        files: child(el, T, "Attachments").map(|a| elements(a).map(|f| text(f, T, "Name").trim().to_string()).collect()).unwrap_or_default(),
    }
}

fn attachments(mailbox: &str, el: Node) -> Option<Vec<(String, String)>> {
    let found: Vec<Node> = child(el, T, "Attachments").map(|a| elements(a).collect()).unwrap_or_default();
    let mut ids = Vec::new();
    for a in &found {
        let id = child(*a, T, "AttachmentId").and_then(|i| i.attribute("Id")).filter(|i| !i.is_empty());
        match id {
            Some(id) if is(a, T, "FileAttachment") => ids.push(id.to_string()),
            _ => return None,
        }
    }
    if ids.is_empty() {
        return Some(vec![]);
    }
    let body: String = ids.iter().map(|i| format!("<t:AttachmentId Id={}/>", quoteattr(i))).collect();
    let text = call(mailbox, &format!("<m:GetAttachment><m:AttachmentIds>{body}</m:AttachmentIds></m:GetAttachment>"))?;
    let doc = Document::parse(&text).ok()?;
    let msgs = messages(&doc);
    if msgs.len() != ids.len() {
        return None;
    }
    let mut got = Vec::new();
    for m in msgs {
        let a = child(m, M, "Attachments").and_then(|x| child(x, T, "FileAttachment"))?;
        let content = child(a, T, "Content")?;
        let data = base64::engine::general_purpose::STANDARD.decode(content.text().unwrap_or("").trim()).ok()?;
        let hex: String = Sha256::digest(&data).iter().map(|b| format!("{b:02x}")).collect();
        got.push((casefold(text_of(a).trim()), hex));
    }
    got.sort();
    Some(got)
}

fn text_of(a: Node) -> String {
    text(a, T, "Name")
}

pub fn query(mailbox: &str, folder: &str, subject: Option<&str>, with_attachments: bool) -> Option<Vec<Message>> {
    let found = find(mailbox, folder, subject, "", false, "")?;
    let doc = Document::parse(&found).ok()?;
    let items = found_items(&doc);
    let ids: Vec<String> = items.iter().map(|i| item_id(*i)).filter(|i| !i.is_empty()).collect();
    if ids.len() != items.len() {
        return None;
    }
    if ids.is_empty() {
        return Some(vec![]);
    }
    let props: String = FIELDS.iter().map(|f| format!(r#"<t:FieldURI FieldURI="{f}"/>"#)).collect::<String>()
        + &["0x0E1D", "0x003D"].iter().map(|t| format!(r#"<t:ExtendedFieldURI PropertyTag="{t}" PropertyType="String"/>"#)).collect::<String>();
    let idxml: String = ids.iter().map(|i| format!("<t:ItemId Id={}/>", quoteattr(i))).collect();
    let text = call(mailbox, &format!(
        "<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:BodyType>Text</t:BodyType><t:AdditionalProperties>{props}</t:AdditionalProperties></m:ItemShape><m:ItemIds>{idxml}</m:ItemIds></m:GetItem>"))?;
    let doc = Document::parse(&text).ok()?;
    let msgs = messages(&doc);
    if msgs.len() != ids.len() {
        return None;
    }
    let mut out = Vec::new();
    for m in msgs {
        let got: Vec<Node> = child(m, M, "Items").map(|i| elements(i).collect()).unwrap_or_default();
        if got.len() != 1 {
            return None;
        }
        let mut item = parse(got[0]);
        if with_attachments && subject.is_none_or(|s| item.subject == s) {
            item.attachments = attachments(mailbox, got[0]);
        }
        out.push(item);
    }
    Some(out.into_iter().filter(|i| subject.is_none_or(|s| i.subject == s)).collect())
}

pub fn msgids(mailbox: &str, folder: &str) -> Option<Vec<String>> {
    let mut parent = String::new();
    if folder != "inbox" {
        let text = call(mailbox, &format!(
            r#"<m:FindFolder Traversal="Deep"><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape></m:FolderShape><m:Restriction><t:IsEqualTo><t:FieldURI FieldURI="folder:DisplayName"/><t:FieldURIOrConstant><t:Constant Value={}/></t:FieldURIOrConstant></t:IsEqualTo></m:Restriction><m:ParentFolderIds><t:DistinguishedFolderId Id="inbox"/></m:ParentFolderIds></m:FindFolder>"#,
            quoteattr(folder)))?;
        let doc = Document::parse(&text).ok()?;
        let root = child(*messages(&doc).first()?, M, "RootFolder")?;
        if root.attribute("IncludesLastItemInRange") != Some("true") {
            return None;
        }
        let ids: Vec<&str> = root.descendants().filter(|n| is(n, T, "FolderId")).map(|f| f.attribute("Id").unwrap_or("")).collect();
        if ids.is_empty() {
            return Some(vec![]);
        }
        if ids.len() != 1 || ids[0].is_empty() {
            return None;
        }
        parent = format!("<t:FolderId Id={}/>", quoteattr(ids[0]));
    }
    let fields = r#"<t:AdditionalProperties><t:FieldURI FieldURI="message:InternetMessageId"/></t:AdditionalProperties>"#;
    let text = find(mailbox, "inbox", None, fields, true, &parent)?;
    let doc = Document::parse(&text).ok()?;
    Some(found_items(&doc).iter().map(|el| text_trim(*el, "InternetMessageId")).collect())
}

fn text_trim(el: Node, name: &str) -> String {
    text(el, T, name).trim().to_string()
}

pub fn read_state(mailbox: &str, k: u64, folder: &str) -> Option<bool> {
    let fields = r#"<t:AdditionalProperties><t:FieldURI FieldURI="message:IsRead"/></t:AdditionalProperties>"#;
    let text = find(mailbox, folder, None, fields, true, "")?;
    let doc = Document::parse(&text).ok()?;
    let hit: Vec<Node> = found_items(&doc).into_iter().filter(|el| key(&item_id(*el)) == k).collect();
    if hit.len() != 1 {
        return None;
    }
    match text_trim(hit[0], "IsRead").as_str() {
        "true" => Some(true),
        "false" => Some(false),
        _ => None,
    }
}

pub fn send(sender: &str, sender_name: &str, to: &str, to_name: &str, subject: &str, body: &str, msgid: &str) -> bool {
    let mailbox = |name: &str, addr: &str| format!("<t:Mailbox><t:Name>{}</t:Name><t:EmailAddress>{}</t:EmailAddress></t:Mailbox>", esc(name), esc(addr));
    call(sender, &format!(
        r#"<m:CreateItem MessageDisposition="SendOnly"><m:Items><t:Message><t:Subject>{}</t:Subject><t:Body BodyType="Text">{}</t:Body><t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0x1035" PropertyType="String"/><t:Value>{}</t:Value></t:ExtendedProperty><t:ToRecipients>{}</t:ToRecipients><t:From>{}</t:From></t:Message></m:Items></m:CreateItem>"#,
        esc(subject), esc(body), esc(msgid), mailbox(to_name, to), mailbox(sender_name, sender))).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_an_item() {
        let xml = format!(r#"<x xmlns:t="{T}"><t:Message><t:ItemId Id="abc"/><t:ItemClass>IPM.Note</t:ItemClass>
            <t:DateTimeSent>2026-10-31T10:00:00Z</t:DateTimeSent><t:TextBody>Hi</t:TextBody>
            <t:Sender><t:Mailbox><t:EmailAddress>A@X.test</t:EmailAddress></t:Mailbox></t:Sender>
            <t:ToRecipients><t:Mailbox><t:EmailAddress>b@x.test</t:EmailAddress></t:Mailbox><t:Mailbox><t:EmailAddress>c@x</t:EmailAddress><t:RoutingType>EX</t:RoutingType></t:Mailbox></t:ToRecipients>
            <t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0xe1d"/><t:Value>Subj</t:Value></t:ExtendedProperty>
            <t:IsRead>true</t:IsRead></t:Message></x>"#);
        let doc = Document::parse(&xml).unwrap();
        let m = parse(doc.root_element().first_element_child().unwrap());
        assert_eq!(m.sender, "a@x.test");
        assert_eq!(m.recipients, vec!["?", "b@x.test"]);
        assert_eq!(m.subject, "Subj");
        assert_eq!(m.submit_ms, 1_793_440_800_000);
        assert!(m.read);
        assert_eq!(m.id, key("abc"));
    }
}
