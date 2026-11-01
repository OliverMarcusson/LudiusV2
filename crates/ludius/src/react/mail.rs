//! Mail reactions: before a slot the persona reads one new mail a colleague sent today and answers it when it
//! asks something of them. Only unread mail is new: a mail read once (here or in a planned task) is not taken again.
//! An unreadable Inbox is unknown: no reaction. The manager's request mails are left to the nightly plan, which maps
//! them; the manager's other mail (a reply, a note) is read like a colleague's.
use std::collections::HashMap;

use anyhow::Result;
use serde_json::{Value, json};

use super::{Reactor, noul};
use crate::calls::CapReached;
use crate::mail;
use crate::planner::{Narrative, PlanItem};

pub const READ: &str = "outlook_read_mail";
pub const REPLY: &str = "outlook_reply_mail";
const PER_SENDER: usize = 3;
const ATTEMPTS: usize = 2;
const SHOWN: usize = 1500;
const REPLY_GATE: f64 = 0.5;

impl Reactor<'_> {
    /// Mail notifications per sender today and attempts per mail (from this day's ground truth).
    fn mail_counts(&self) -> Result<(HashMap<String, usize>, HashMap<String, usize>)> {
        let (mut senders, mut tries) = (HashMap::new(), HashMap::new());
        for r in self.records()? {
            let s = |k: &str| r.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
            if s("event") == "mail_notification" && !s("msgid").is_empty() {
                *senders.entry(s("sender")).or_default() += 1;
                *tries.entry(s("msgid")).or_default() += 1;
            }
        }
        Ok((senders, tries))
    }

    pub(super) fn react_mail(&mut self, day: &str, at: &str, run: &mut dyn FnMut(&PlanItem) -> Result<String>) -> Result<()> {
        if !self.templates.contains_key(READ) {
            return Ok(());
        }
        let me = self.persona.user.clone();
        let Some(inbox) = (self.inbox)(&me) else { return self.write(json!({"event": "mail_notification", "unknown": true})) };
        let world = match Narrative::load(&self.narrative) {
            Ok(w) => w,
            Err(e) => return self.write(json!({"event": "mail_notification", "error": format!("narrative unreadable: {e}")})),
        };
        // the manager's request mails are mapped by the nightly plan (a reaction would answer them twice); their other mail is read
        let (people, colleagues) = (world.people_names(), &world.colleagues(&me) | &world.boss(&me));
        let boss = world.boss(&me);
        let root = crate::requests::root();
        let request = |m: &mail::Message| !matches!(crate::requests::find(&m.msgid, &root), Ok(None));
        let since = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")?.and_hms_opt(0, 0, 0).unwrap_or_default().and_utc().timestamp_millis();
        let (senders, tries) = self.mail_counts()?;
        let domain = format!("@{}", mail::domain());
        let mut new: Vec<_> = inbox
            .into_iter()
            .filter(|m| {
                let user = m.sender.strip_suffix(&domain).unwrap_or_default().to_string();
                m.cls == "IPM.Note"
                    && !m.read
                    && colleagues.contains(&user)
                    && !m.subject.trim().is_empty()
                    && !m.msgid.is_empty()
                    && m.delivery_ms >= since
                    && tries.get(&m.msgid).copied().unwrap_or(0) < ATTEMPTS
                    && senders.get(&user).copied().unwrap_or(0) < PER_SENDER
                    && !self.policy.mail_reserved(&m.sender, &m.subject, &m.body)
                    && !(boss.contains(&user) && request(m))
            })
            .collect();
        new.sort_by_key(|m| m.delivery_ms);
        // ponytail: one mail per slot; the rest wait for the next slot (the per-sender cap still holds)
        let Some(m) = new.into_iter().next() else { return Ok(()) };
        let user = m.sender.strip_suffix(&domain).unwrap_or_default().to_string();
        let name = people.get(&user).cloned().unwrap_or_else(|| user.clone());
        self.write(json!({"event": "mail_notification", "sender": user, "msgid": m.msgid, "subject": m.subject}))?;
        let read = PlanItem {
            at: at.into(),
            template: READ.into(),
            params: [("sender".to_string(), m.sender.clone()), ("subject".to_string(), m.subject.clone())].into_iter().collect(),
            context: format!("A mail notification: new mail from {name}"),
            ..Default::default()
        };
        if run(&read)? != "done" || !self.templates.contains_key(REPLY) {
            return Ok(());
        }
        // no Decider answer: no reply (a missed answer is noise; an unwanted one is a wrong effect)
        let Some(decider) = &self.decider else { return Ok(()) };
        let body: String = mail::above_quote(&m.body).trim().chars().take(SHOWN).collect();
        let q = noul(
            "Does this mail from a colleague ask the reader (You) a question or for an answer, so that a coworker would reply to it now?",
            "A question or request to the reader that a short reply answers.",
            "Only information, thanks, a done-note, or nothing that needs an answer from the reader.",
        );
        let yes = match decider(&json!({"mail": {"from": name, "subject": m.subject, "body": body}}), &json!({"reply": q})).and_then(|a| Ok((a.noul("reply")?, a.cost))) {
            Ok(y) => y,
            Err(e) if e.is::<CapReached>() => return Err(e),
            Err(e) => return self.write(json!({"event": "mail_triage", "msgid": m.msgid, "error": e.to_string().chars().take(300).collect::<String>()})),
        };
        self.write(json!({"event": "mail_triage", "msgid": m.msgid, "reply": (yes.0 * 1000.0).round() / 1000.0, "cost": yes.1}))?;
        if yes.0 < REPLY_GATE {
            return Ok(());
        }
        let reply = PlanItem {
            at: at.into(),
            template: REPLY.into(),
            params: [("to".to_string(), m.sender.clone()), ("to_name".to_string(), name), ("subject".to_string(), m.subject.clone())].into_iter().collect(),
            context: "Answering the mail just read".into(),
            ..Default::default()
        };
        run(&reply).map(drop)
    }
}
