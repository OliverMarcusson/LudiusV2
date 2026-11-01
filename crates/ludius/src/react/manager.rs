//! The manager acts: a mail the persona sent its manager today gets the manager's answer, server-side (managers have
//! mailboxes but no VM). Before a slot: the oldest unanswered mail, a Decider gate, the reply text in the manager's
//! name, then mail::deliver as the manager. Unreadable mailbox is unknown: nothing sent. At most PER_DAY replies a
//! day and one decision per mail, from the day's ground truth.
use std::collections::HashMap;

use anyhow::Result;
use serde_json::{Value, json};

use super::{Reactor, noul};
use crate::calls::CapReached;
use crate::mail;
use crate::planner::Narrative;
use crate::reply;

const PER_DAY: usize = 3;
const ATTEMPTS: usize = 2;
const SHOWN: usize = 2000;
const GATE: f64 = 0.5;
const EVENT: &str = "manager_reply";

impl Reactor<'_> {
    /// Records per persona mail today, and how many replies were sent.
    fn manager_counts(&self) -> Result<(HashMap<String, (usize, bool)>, usize)> {
        let (mut tries, mut sent) = (HashMap::<String, (usize, bool)>::new(), 0);
        for r in self.records()? {
            let s = |k: &str| r.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
            if s("event") == EVENT && !s("msgid").is_empty() {
                let e = tries.entry(s("msgid")).or_default();
                e.0 += 1;
                // a decision (reply or none) is final; only an error is tried again
                e.1 |= r.get("final").and_then(Value::as_bool).unwrap_or(false);
                sent += usize::from(r.get("sent").and_then(Value::as_bool).unwrap_or(false));
            }
        }
        Ok((tries, sent))
    }

    pub(super) fn react_manager(&mut self, day: &str) -> Result<()> {
        // no Decider: no reply (a missed answer is noise; an unwanted one is a wrong effect)
        let Some(decider) = &self.decider else { return Ok(()) };
        let me = self.persona.user.clone();
        let world = match Narrative::load(&self.narrative) {
            Ok(w) => w,
            Err(e) => return self.write(json!({"event": EVENT, "error": format!("narrative unreadable: {e}")})),
        };
        let Some(boss) = world.boss(&me).into_iter().next() else { return Ok(()) };
        let Some(manager) = world.people.iter().find(|p| p.user == boss).cloned() else { return Ok(()) };
        let (tries, sent) = self.manager_counts()?;
        if sent >= PER_DAY {
            return Ok(());
        }
        let Some(inbox) = (self.inbox)(&boss) else { return self.write(json!({"event": EVENT, "manager": boss, "unknown": true})) };
        let since = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")?.and_hms_opt(0, 0, 0).unwrap_or_default().and_utc().timestamp_millis();
        let mine = format!("{me}@{}", mail::domain());
        let mut new: Vec<_> = inbox
            .into_iter()
            .filter(|m| {
                let (n, done) = tries.get(&m.msgid).copied().unwrap_or_default();
                m.cls == "IPM.Note"
                    && m.sender.eq_ignore_ascii_case(&mine)
                    && !m.subject.trim().is_empty()
                    && !m.msgid.is_empty()
                    && m.delivery_ms >= since
                    && !done
                    && n < ATTEMPTS
                    && !self.policy.mail_reserved(&m.sender, &m.subject, &m.body)
            })
            .collect();
        new.sort_by_key(|m| m.delivery_ms);
        // ponytail: one mail per slot; the rest wait for the next slot
        let Some(m) = new.into_iter().next() else { return Ok(()) };
        let rec = |extra: Value| {
            let mut r = json!({"event": EVENT, "manager": boss, "msgid": m.msgid, "subject": m.subject});
            r.as_object_mut().unwrap().extend(extra.as_object().cloned().unwrap_or_default());
            r
        };
        let shown: String = mail::above_quote(&m.body).trim().chars().take(SHOWN).collect();
        let q = noul(
            "Would a manager answer this mail from someone who reports to them, now, with a short mail?",
            "It asks the manager something, hands in work, or reports something a manager acknowledges.",
            "Nothing for the manager to answer: a forward, a thanks, or a note that needs no reply.",
        );
        let answer = decider(&json!({"mail": {"from": self.persona.name, "to": manager.name, "subject": m.subject, "body": shown}}), &json!({"reply": q}))
            .and_then(|a| Ok((a.noul("reply")?, a.cost)));
        let (yes, cost) = match answer {
            Ok(y) => y,
            Err(e) if e.is::<CapReached>() => return Err(e),
            Err(e) => return self.write(rec(json!({"error": e.to_string().chars().take(300).collect::<String>()}))),
        };
        let yes = (yes * 1000.0).round() / 1000.0;
        if yes < GATE {
            return self.write(rec(json!({"reply": yes, "cost": cost, "final": true, "sent": false})));
        }
        let user = reply::prompt((&manager.name, &manager.role, &manager.department, ""), &self.persona.name, &m, &shown);
        let text = match (self.draft)(&user) {
            Ok(t) => t,
            Err(e) if e.is::<CapReached>() => return Err(e),
            Err(e) => return self.write(rec(json!({"reply": yes, "error": format!("reply text: {e}")}))),
        };
        let people = world.people_names();
        if let Some(why) = reply::problem(&text, &people, &self.policy) {
            return self.write(rec(json!({"reply": yes, "text": text, "error": format!("reply text: {why}")})));
        }
        let subject = if m.subject.to_lowercase().starts_with("re:") { m.subject.clone() } else { format!("Re: {}", m.subject) };
        // derived from the mail it answers: a retry after an unconfirmed send cannot deliver a second copy
        let msgid = format!("<re.{}>", m.msgid.trim_matches(['<', '>']));
        let boss_addr = format!("{boss}@{}", mail::domain());
        let ok = (self.send)(&boss_addr, &manager.name, &mine, &self.persona.name, &subject, &text, &msgid);
        // a failed send is not final: the next slot tries once more with the same msgid
        self.write(rec(json!({"reply": yes, "cost": cost, "text": text, "reply_msgid": msgid, "sent": ok, "final": ok})))
    }
}
