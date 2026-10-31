//! planner.validate: the checks a freshly written plan passes before it is saved (load-time checks plus
//! history, near-duplicates, threads, invitations, follow-ups and name collisions).
use std::collections::{BTreeMap, BTreeSet, HashMap};

use anyhow::Result;

use super::check::{asked_files, content_values, item_problem, meeting_day, owned_problem, recipient, sender, taken_names, targets, uses_own_file};
use super::{PlanItem, Scenario};
use crate::policy::Policy;
use crate::tasks::{ATTACHABLE, Template};

const NEAR_DUPLICATE: f64 = 0.85;

/// What a plan is checked against; empty fields mean "none known".
#[derive(Default)]
pub struct Context<'a> {
    pub templates: BTreeMap<String, Template>,
    pub history: Vec<String>,
    pub policy: Policy,
    pub colleagues: BTreeSet<String>,
    pub scenario: Option<&'a Scenario>,
    pub followups: BTreeSet<String>,
    pub existing: Vec<String>,
    /// follow-up id -> (sender account, subject) of the mail it answers
    pub mail_threads: BTreeMap<String, (String, String)>,
    pub people: BTreeMap<String, String>,
    pub user: String,
    pub day: String,
    /// (sender account, subject)
    pub inbox: BTreeSet<(String, String)>,
    pub teams: Vec<String>,
    pub owned: BTreeMap<String, String>,
    pub shares: Vec<String>,
    /// (organizer account, subject)
    pub invites: BTreeSet<(String, String)>,
}

fn account(addr: &str) -> String {
    let a = addr.trim().to_lowercase();
    a.split_once('@').map(|(u, _)| u.to_string()).unwrap_or(a)
}

/// (item index, why) for every rejected item; accepted items' content and names count against later ones.
pub fn validate(items: &[PlanItem], c: &Context) -> Result<Vec<(usize, String)>> {
    let mut rejects = Vec::new();
    let mut seen = c.history.clone();
    let mut taken = taken_names(c.existing.iter().map(String::as_str), true);
    for (i, it) in items.iter().enumerate() {
        let t = c.templates.get(&it.template);
        let cap = t.map(|t| t.cap()).unwrap_or("");
        let mut why = check_item(it, c, &seen)?;
        let mut next = |f: &dyn Fn() -> Option<String>| {
            if why.is_none() {
                why = f();
            }
        };
        next(&|| recipient(it, &c.colleagues, &c.people));
        next(&|| meeting_day(it, &c.day));
        next(&|| (cap == "mail.reply").then(|| reply_thread(it, &c.mail_threads, &c.inbox)).flatten());
        next(&|| (cap == "calendar.respond").then(|| invite(it, &c.invites)).flatten());
        next(&|| {
            let name = it.params.get("file_name").map(|n| n.to_lowercase()).unwrap_or_default();
            (cap == "mail.save" && !ATTACHABLE.iter().any(|x| name.ends_with(x))).then(|| format!("file_name: the attachment's exact name, a {} file", ATTACHABLE.join(", ")))
        });
        next(&|| ["mail.read", "mail.save", "mail.file"].contains(&cap).then(|| read_mail(it, &c.colleagues, &c.inbox, &items[..i])).flatten());
        next(&|| {
            let t = t.filter(|t| uses_own_file(t))?;
            let mut owned = c.owned.clone();
            owned.extend(asked_files(it, items, &c.templates, &c.day));
            owned_problem(it, t, &owned, &c.day, items)
        });
        next(&|| (!it.handoff.is_empty() && !c.colleagues.contains(&it.handoff)).then(|| format!("handoff {:?} is not a colleague's account", it.handoff)));
        next(&|| (!it.followup.is_empty() && cap == "chat.read").then(|| "reading the chat does not settle a follow-up: set followup on the asked work".into()));
        next(&|| (!it.followup.is_empty() && !c.followups.contains(&it.followup)).then(|| format!("followup {:?} is not an open follow-up", it.followup)));
        next(&|| items[..i].iter().any(|x| x.at == it.at && x.template == it.template).then(|| format!("another item is already {} at {}", it.template, it.at)));
        next(&|| (!it.followup.is_empty() && items[..i].iter().any(|x| x.followup == it.followup)).then(|| format!("followup {:?} is already scheduled", it.followup)));
        next(&|| {
            let (s, t) = (c.scenario?, t?);
            (!t.resembles.is_empty() && s.start <= it.at && it.at <= s.end).then(|| format!("{}-like work must not overlap the scenario window {}-{}", t.resembles, s.start, s.end))
        });
        next(&|| {
            targets(it, &c.templates).into_iter().find(|(k, v)| !taken_names([v.as_str()], k != "folder_name").is_disjoint(&taken)).map(|(k, v)| format!("{k}: {v:?} already exists or is used by another item, choose a different name"))
        });
        match why {
            Some(why) => rejects.push((i, why)),
            None => {
                seen.extend(content_values(it, &c.templates));
                for (k, v) in targets(it, &c.templates) {
                    taken.extend(taken_names([v.as_str()], k != "folder_name"));
                }
            }
        }
    }
    Ok(rejects)
}

fn check_item(it: &PlanItem, c: &Context, seen: &[String]) -> Result<Option<String>> {
    if let Some(why) = item_problem(it, &c.templates, &c.policy, &c.user, &c.teams, &c.shares)? {
        return Ok(Some(why));
    }
    Ok(content_values(it, &c.templates).iter().find_map(|v| near_duplicate(v, seen)).map(|dup| format!("too similar to earlier content {:?}", dup.chars().take(60).collect::<String>())))
}

fn reply_thread(it: &PlanItem, threads: &BTreeMap<String, (String, String)>, inbox: &BTreeSet<(String, String)>) -> Option<String> {
    let sender = account(it.value("to").map(String::as_str).unwrap_or(""));
    let subject = it.value("subject").map(|s| s.trim().to_string()).unwrap_or_default();
    if it.followup.is_empty() && (inbox.contains(&(sender.clone(), subject.clone())) || (subject.is_empty() && inbox.iter().any(|(s, _)| *s == sender))) {
        return None;
    }
    match threads.get(&it.followup) {
        None => Some("a reply answers a colleague's mail: set followup to that mail's open follow-up, or a listed inbox mail".into()),
        Some(t) if *t != (sender, subject) => Some(format!("followup {:?} is {:?}'s mail {:?}: reply to that, with that subject", it.followup, t.0, t.1)),
        Some(_) => None,
    }
}

fn invite(it: &PlanItem, invites: &BTreeSet<(String, String)>) -> Option<String> {
    if !matches!(it.value("response").map(String::as_str), Some("accept" | "decline")) {
        return Some("response: accept or decline".into());
    }
    let key = (account(it.value("to").map(String::as_str).unwrap_or("")), it.value("subject").cloned().unwrap_or_default());
    (!invites.contains(&key)).then(|| "answer only a listed meeting invitation: to is its organizer, subject its exact subject".into())
}

fn read_mail(it: &PlanItem, colleagues: &BTreeSet<String>, inbox: &BTreeSet<(String, String)>, earlier: &[PlanItem]) -> Option<String> {
    if let Some(why) = sender(it, colleagues) {
        return Some(why);
    }
    let Some(from) = it.value("sender") else { return Some("sender: missing".into()) };
    let key = (account(from), it.value("subject").cloned().unwrap_or_default());
    if !inbox.contains(&key) {
        return Some("a read opens a mail listed under Inbox on the mail server: its sender and exact subject".into());
    }
    earlier
        .iter()
        .any(|x| x.template == it.template && x.value("subject") == Some(&key.1))
        .then(|| format!("the mail {:?} is already read earlier in the plan", key.1))
}

/// The first earlier text whose normalized form is at least NEAR_DUPLICATE similar (difflib ratio, no autojunk).
pub fn near_duplicate<'a>(text: &str, earlier: &'a [String]) -> Option<&'a String> {
    let a: Vec<char> = norm(text).chars().collect();
    earlier.iter().find(|b| {
        let b: Vec<char> = norm(b).chars().collect();
        let total = (a.len() + b.len()) as f64;
        // real_quick_ratio: an upper bound from the lengths alone
        total > 0.0 && 2.0 * a.len().min(b.len()) as f64 / total >= NEAR_DUPLICATE && 2.0 * matched(&a, &b) as f64 / total >= NEAR_DUPLICATE
    })
}

fn norm(s: &str) -> String {
    super::re("[a-z0-9]+").find_iter(&s.to_lowercase()).map(|m| m.as_str()).collect::<Vec<_>>().join(" ")
}

/// difflib.SequenceMatcher(None, a, b, autojunk=False): the total size of the matching blocks.
pub(crate) fn matched(a: &[char], b: &[char]) -> usize {
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (j, c) in b.iter().enumerate() {
        b2j.entry(*c).or_default().push(j);
    }
    let longest = |alo: usize, ahi: usize, blo: usize, bhi: usize| -> (usize, usize, usize) {
        let (mut besti, mut bestj, mut best) = (alo, blo, 0);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for i in alo..ahi {
            let mut next = HashMap::new();
            for &j in b2j.get(&a[i]).map(Vec::as_slice).unwrap_or(&[]) {
                if j < blo {
                    continue;
                }
                if j >= bhi {
                    break;
                }
                let k = j.checked_sub(1).and_then(|p| j2len.get(&p)).copied().unwrap_or(0) + 1;
                next.insert(j, k);
                if k > best {
                    (besti, bestj, best) = (i + 1 - k, j + 1 - k, k);
                }
            }
            j2len = next;
        }
        (besti, bestj, best)
    };
    let (mut total, mut queue) = (0, vec![(0, a.len(), 0, b.len())]);
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest(alo, ahi, blo, bhi);
        if k > 0 {
            total += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    total
}
