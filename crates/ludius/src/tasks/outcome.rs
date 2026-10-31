//! Done checks outside the UI (mail, calendar, chat, files) and their baselines.
use super::*;

pub(crate) fn outside_ok(done: &Done, task: &mut Task) -> Result<bool> {
    if let Some(c) = &done.mail
        && !mail_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.invite
        && !invite_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.response
        && !response_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.file
        && !file_ok(c, task)?
    {
        return Ok(false);
    }
    if done.read && !(task.read_id > 0 && !task.owner.is_empty() && mail::read_state(&task.owner, task.read_id, "inbox") == Some(true)) {
        return Ok(false);
    }
    if done.filed {
        let folder = task.values.get("folder").cloned().unwrap_or_default();
        if task.owner.is_empty() || mail::placed(&task.owner, &task.file_msgid, &folder) != Some((0, 1)) {
            return Ok(false);
        }
    }
    if let Some(c) = &done.chat_file
        && !chat_file_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.chat_post
        && !chat_post_ok(c, task)?
    {
        return Ok(false);
    }
    Ok(true)
}

pub fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data).iter().map(|b| format!("{b:02x}")).collect()
}

pub(crate) fn split_addr(a: &str) -> (String, String) {
    let l = a.to_lowercase();
    match l.split_once('@') {
        Some((u, d)) => (u.into(), d.into()),
        None => (l, String::new()),
    }
}

pub(crate) fn chat_file_ok(c: &ChatFileCheck, task: &Task) -> Result<bool> {
    let (rel, to) = (task.fmt(&c.path)?, task.fmt(&c.to)?);
    let (user, dom) = split_addr(&to);
    let Some(data) = task.sources_before.get(&rel) else { return Ok(false) };
    if task.owner.is_empty() || dom != mail::domain() {
        return Ok(false);
    }
    Ok(chat::shared(&task.owner, &user, &leaf(&rel), task.started_ms, c.posted) == Some(vec![sha256_hex(data)]))
}

pub(crate) fn chat_post_ok(c: &ChatPostCheck, task: &Task) -> Result<bool> {
    let mut to = task.fmt(&c.to)?;
    if !to.starts_with('~') {
        let (u, d) = split_addr(&to);
        if d != mail::domain() {
            return Ok(false);
        }
        to = u;
    }
    Ok(!task.owner.is_empty() && chat::posted(&task.owner, &to, &task.fmt(&c.message)?, task.started_ms).unwrap_or(0) > 0)
}

pub(crate) fn mail_ok(c: &MailCheck, task: &Task) -> Result<bool> {
    let sender = if task.owner.is_empty() { String::new() } else { format!("{}@{}", task.owner, mail::domain()) };
    let mut attachment = None;
    if let Some(a) = &c.attachment {
        let rel = task.fmt(a)?;
        let Some(data) = task.sources_before.get(&rel) else { return Ok(false) };
        attachment = Some((casefold(&leaf(&rel)), sha256_hex(data)));
    }
    Ok(mail::delivered(
        &task.fmt(&c.mailbox)?,
        &c.folder,
        &task.fmt(&c.subject)?,
        &task.fmt(&c.body)?,
        &sender,
        task.started_ms,
        c.above_quote,
        task.mail_before.as_ref(),
        &task.fmt(&c.reply_to)?,
        attachment.as_ref(),
    ))
}

pub fn local_ms(date: &str, hhmm: &str, tz: &str) -> Result<i64> {
    let tz: chrono_tz::Tz = tz.parse().map_err(|e| anyhow!("{e}"))?;
    let naive = chrono::NaiveDateTime::parse_from_str(&format!("{date}T{hhmm}"), "%Y-%m-%dT%H:%M")
        .or_else(|_| chrono::NaiveDateTime::parse_from_str(&format!("{date}T{hhmm}"), "%Y-%m-%dT%H:%M:%S"))?;
    // Python's replace(tzinfo=ZoneInfo) takes the earlier offset (fold=0) on a DST overlap.
    let t = naive.and_local_timezone(tz).earliest().ok_or_else(|| anyhow!("{date} {hhmm} does not exist in {tz}"))?;
    Ok(t.timestamp_millis())
}

pub(crate) fn response_ok(c: &ResponseCheck, task: &Task) -> Result<bool> {
    Ok(!task.owner.is_empty()
        && mail::responded(
            &task.fmt(&c.mailbox)?,
            &task.fmt(&c.subject)?,
            &format!("{}@{}", task.owner, mail::domain()),
            &task.fmt(&c.response)?,
            task.started_ms,
            task.mail_before.as_ref(),
        ))
}

pub(crate) fn invite_ok(c: &InviteCheck, task: &Task) -> Result<bool> {
    if task.owner.is_empty() {
        return Ok(false);
    }
    let date = task.fmt(&c.date)?;
    Ok(mail::invited(
        &task.fmt(&c.mailbox)?,
        &task.fmt(&c.subject)?,
        &task.fmt(&c.body)?,
        &format!("{}@{}", task.owner, mail::domain()),
        task.started_ms,
        local_ms(&date, &task.fmt(&c.start)?, &task.tz)?,
        local_ms(&date, &task.fmt(&c.end)?, &task.tz)?,
        task.mail_before.as_ref(),
    ))
}

/// (mailbox, folder, subject, the sender's folder) of a subgoal's sent check.
pub(crate) fn sent_check(done: Option<&Done>) -> Option<(String, String, String, &'static str)> {
    let d = done?;
    if let Some(m) = &d.mail {
        return Some((m.mailbox.clone(), m.folder.clone(), m.subject.clone(), "sent"));
    }
    if let Some(i) = &d.invite {
        return Some((i.mailbox.clone(), "inbox".into(), i.subject.clone(), "calendar"));
    }
    d.response.as_ref().map(|r| (r.mailbox.clone(), "inbox".into(), r.subject.clone(), "sent"))
}

pub fn mail_baseline(task: &Task) -> Result<Option<mail::Seen>> {
    let sender = format!("{}@{}", task.owner, mail::domain());
    let mut seen = mail::Seen::new();
    for sg in task.template.subgoals.values() {
        if let Some((mb, folder, subject, mine)) = sent_check(sg.done.as_ref()) {
            match mail::present(&task.fmt(&mb)?, &folder, &task.fmt(&subject)?, &sender, mine) {
                Some(found) => seen.extend(found),
                None => return Ok(None),
            }
        }
    }
    Ok(Some(seen))
}

pub const RESERVED_RECIPIENT: &str = "precondition: the planned recipient is scenario-reserved";

pub fn recipient_reserved(sg: &Subgoal, task: &Task, policy: &crate::policy::Policy) -> Result<bool> {
    Ok(match sent_check(sg.done.as_ref()) {
        Some((mb, ..)) => policy.reserved(&task.fmt(&mb)?),
        None => false,
    })
}

pub fn write_paths(task: &Task) -> Result<Vec<String>> {
    task.template.writes.iter().map(|w| task.fmt(w)).collect()
}

pub fn file_reserved(user: &str, rel: &str, policy: &crate::policy::Policy) -> bool {
    let any_ext = if rel.ends_with(".*") { ".*" } else { "" };
    let full = if user.is_empty() { None } else { profile_path(user, rel.strip_suffix(".*").unwrap_or(rel)) };
    policy.path_reserved(rel) || full.is_some_and(|f| policy.path_reserved(&format!("{f}{any_ext}")))
}

/// Read what the done checks will compare against; a reason string means abandon before acting.
pub fn file_baseline(task: &mut Task, policy: &crate::policy::Policy) -> Result<Option<String>> {
    let files: Vec<FileCheck> = task.template.dones().filter_map(|d| d.file.clone()).collect();
    let owner = task.owner.clone();
    let mut sources = Vec::new();
    for c in &files {
        if let Some(s) = c.source.as_ref().or(c.export_of.as_ref()).or(c.extract_of.as_ref()) {
            sources.push(task.fmt(s)?);
        }
    }
    let attached: Vec<String> = task.template.attaches().iter().map(|a| task.fmt(a)).collect::<Result<_>>()?;
    let removes: Vec<String> = task.template.removes.iter().map(|r| task.fmt(r)).collect::<Result<_>>()?;
    for rel in write_paths(task)?.iter().chain(&removes).chain(&sources).chain(&attached) {
        if file_reserved(&owner, rel, policy) {
            return Ok(Some(format!("{rel}: scenario-reserved file")));
        }
    }
    let mut absent = BTreeSet::new();
    let mut present = BTreeSet::new();
    let recycled: BTreeSet<String> = files.iter().filter(|c| c.recycled).map(|c| task.fmt(&c.path)).collect::<Result<_>>()?;
    for rel in recycled {
        match guestfile::read(&owner, &rel) {
            None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
            Some(f) if !f.exists => return Ok(Some(format!("{rel}: not there, nothing to recycle"))),
            Some(_) => {
                present.insert(rel);
            }
        }
    }
    task.files_present = present;
    for c in files.iter().filter(|c| c.share.is_some()) {
        let (rel, dest) = (task.fmt(&c.path)?, task.fmt(c.share.as_deref().unwrap_or(""))?);
        let copy = share::unc(&dest, &leaf(&rel));
        if file_reserved(&owner, &rel, policy) || copy.is_empty() || policy.path_reserved(&copy) {
            let shown = if copy.is_empty() { "(LUDIUS_SHARE_HOST unset)".to_string() } else { copy };
            return Ok(Some(format!("{rel} or its copy {shown}: scenario-reserved or unknown")));
        }
        let (f, s) = (guestfile::read(&owner, &rel), share::read(&dest, &leaf(&rel)));
        let (Some(f), Some(s)) = (f.clone(), s) else {
            return Ok(Some(format!("{}: unreadable", if f.is_none() { rel } else { copy })));
        };
        if !f.exists {
            return Ok(Some(format!("{rel}: not there, nothing to copy")));
        }
        if s.exists {
            return Ok(Some(format!("{copy}: already exists, never overwritten")));
        }
        absent.insert(copy);
    }
    let moved: BTreeSet<String> = files.iter().filter_map(|c| c.moved_from.as_ref()).map(|m| task.fmt(m)).collect::<Result<_>>()?;
    for src in moved {
        match guestfile::read(&owner, &src) {
            None => return Ok(Some(format!("{src}: unreadable on the guest"))),
            Some(f) if !f.exists => return Ok(Some(format!("{src}: not there, nothing to move"))),
            Some(f) => {
                task.sources_before.insert(src, f.data);
            }
        }
    }
    for rel in attached.iter().collect::<BTreeSet<_>>() {
        match guestfile::read(&owner, rel) {
            None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
            Some(f) if !f.exists || f.data.len() > ATTACH_MAX_BYTES => {
                return Ok(Some(format!("{rel}: not there or over {ATTACH_MAX_BYTES} bytes, nothing to attach")));
            }
            Some(f) => {
                task.sources_before.insert(rel.clone(), f.data);
            }
        }
    }
    let exports: BTreeSet<String> = files.iter().filter_map(|c| c.export_of.as_ref()).map(|m| task.fmt(m)).collect::<Result<_>>()?;
    for src in exports {
        match guestfile::read(&owner, &src) {
            None => return Ok(Some(format!("{src}: unreadable on the guest"))),
            Some(f) if !f.exists => return Ok(Some(format!("{src}: not there, nothing to export"))),
            Some(f) => {
                task.sources_before.insert(src, f.data);
            }
        }
    }
    for c in files.iter().filter(|c| c.extract_of.is_some()) {
        let (rel, src) = (task.fmt(&c.path)?, task.fmt(c.extract_of.as_deref().unwrap_or(""))?);
        let Some(f) = guestfile::read(&owner, &src) else { return Ok(Some(format!("{src}: unreadable on the guest"))) };
        let member = if f.exists { zip_single(&f.data) } else { None };
        let Some((name, data)) = member.filter(|(n, _)| casefold(n) == casefold(&leaf(&rel))) else {
            return Ok(Some(format!("{src}: not an archive of exactly {}, nothing to extract", leaf(&rel))));
        };
        let _ = name;
        task.sources_before.insert(rel.clone(), data);
        let parent = rel.rsplit_once('\\').map(|(p, _)| p.to_string()).unwrap_or_default();
        let folder = guestfile::read(&owner, &parent);
        if folder.as_ref().is_some_and(|f| f.exists) {
            return Ok(Some(format!("{parent}: a file, not a folder to extract into")));
        }
        if folder.is_some() {
            absent.insert(rel);
        }
    }
    for c in files.iter().filter(|c| c.attachment_of.as_ref().is_some_and(|a| !a.is_empty())) {
        let a = c.attachment_of.as_ref().unwrap();
        let (rel, sender, subject) = (task.fmt(&c.path)?, task.fmt(&a[0])?, task.fmt(&a[1])?);
        let Some(got) = mail::matching(&owner, "inbox", &subject, true) else { return Ok(Some(format!("{subject:?}: inbox unreadable"))) };
        let address = format!("{}@{}", split_addr(sender.trim()).0, mail::domain());
        let hits: Vec<&mail::Message> = got.iter().filter(|m| m.sender.trim().to_lowercase() == address).collect();
        if hits.len() != 1 || hits[0].attachments.is_none() {
            return Ok(Some(format!("{subject:?} from {address}: not exactly one mail with readable attachments")));
        }
        if policy.mail_reserved(&hits[0].sender, &hits[0].subject, &hits[0].body) {
            return Ok(Some(format!("{subject:?} from {address}: scenario-reserved mail")));
        }
        let want = casefold(&leaf(&rel));
        let sha: Vec<&String> = hits[0].attachments.as_ref().unwrap().iter().filter(|(n, _)| *n == want).map(|(_, h)| h).collect();
        if sha.len() != 1 {
            return Ok(Some(format!("{subject:?}: no single attachment named {:?}", leaf(&rel))));
        }
        task.saved_sha.insert(rel, sha[0].clone());
    }
    let mut edits = BTreeMap::new();
    let mut appends = HashMap::new();
    for c in files.iter().filter(|c| c.append.is_some()) {
        let rel = task.fmt(&c.path)?;
        edits.insert(rel.clone(), task.fmt(c.source.as_deref().unwrap_or(&c.path))?);
        appends.insert(rel, c.append.clone().unwrap_or_default());
    }
    for (rel, src) in &edits {
        let f = guestfile::read(&owner, src);
        if f.as_ref().is_some_and(|f| !f.exists) {
            return Ok(Some(format!("{src}: not there, nothing to edit")));
        }
        let Some(f) = f else { return Ok(Some(format!("{src}: unreadable on the guest"))) };
        let Some(lines) = doc_lines(src, &f.data) else { return Ok(Some(format!("{src}: unreadable on the guest"))) };
        if rel.to_lowercase().ends_with(".xlsx")
            && let Some(why) = completes(&lines, &task.fmt(&appends[rel])?)
        {
            return Ok(Some(format!("{rel}: {why}")));
        }
        task.files_before.insert(rel.clone(), lines);
        if src != rel {
            task.sources_before.insert(src.clone(), f.data);
            match guestfile::read(&owner, rel) {
                None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
                Some(n) if n.exists => return Ok(Some(format!("{rel}: already exists, never overwritten"))),
                Some(_) => {
                    absent.insert(rel.clone());
                }
            }
        }
    }
    let mut commented: Vec<String> = Vec::new();
    for c in files.iter().filter(|c| c.comment.is_some()) {
        let rel = task.fmt(&c.path)?;
        if !commented.contains(&rel) {
            commented.push(rel);
        }
    }
    for rel in commented {
        let f = guestfile::read(&owner, &rel);
        if f.as_ref().is_some_and(|f| !f.exists) {
            return Ok(Some(format!("{rel}: not there, nothing to comment on")));
        }
        let (paras, notes) = match &f {
            Some(f) => (guestfile::docx_paragraphs(&f.data), guestfile::docx_comments(&f.data)),
            None => (None, None),
        };
        let (Some(paras), Some(notes)) = (paras, notes) else { return Ok(Some(format!("{rel}: unreadable on the guest"))) };
        task.files_before.insert(rel.clone(), paras.iter().map(|x| casefold(x)).collect());
        task.comments_before.insert(rel, notes);
    }
    let mut checked: Vec<String> = Vec::new();
    for c in files.iter().filter(|c| c.append.is_none() && c.comment.is_none() && !c.recycled && c.share.is_none()) {
        checked.push(task.fmt(&c.path)?);
    }
    if !task.inst.request.is_empty() {
        checked.extend(write_paths(task)?.into_iter().filter(|w| !w.ends_with(".*") && !edits.contains_key(w)));
    }
    let mut done_rel: Vec<String> = Vec::new();
    for rel in checked {
        if absent.contains(&rel) || done_rel.contains(&rel) {
            continue;
        }
        done_rel.push(rel.clone());
        match guestfile::read(&owner, &rel) {
            None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
            Some(f) if f.exists => return Ok(Some(format!("{rel}: already exists, never overwritten"))),
            Some(_) => {
                absent.insert(rel);
            }
        }
    }
    task.files_absent = Some(absent);
    Ok(None)
}

pub(crate) fn file_ok(c: &FileCheck, task: &mut Task) -> Result<bool> {
    let rel = task.fmt(&c.path)?;
    let owner = task.owner.clone();
    if let Some(dest) = &c.share {
        let (dest, name) = (task.fmt(dest)?, leaf(&rel));
        if !task.files_absent.as_ref().is_some_and(|a| a.contains(&share::unc(&dest, &name))) {
            return Ok(false);
        }
        let (s, f) = (share::read(&dest, &name), guestfile::read(&owner, &rel));
        return Ok(matches!((s, f), (Some(s), Some(f)) if s.exists && f.exists && s.data == f.data));
    }
    if c.recycled {
        let f = guestfile::read(&owner, &rel);
        return Ok(task.files_present.contains(&rel) && f.is_some_and(|f| !f.exists));
    }
    if let Some(comment) = &c.comment {
        let (f, before) = (guestfile::read(&owner, &rel), task.comments_before.get(&rel));
        let (Some(f), Some(before)) = (f, before) else { return Ok(false) };
        if !f.exists || f.written_ms < task.started_ms {
            return Ok(false);
        }
        let (Some(paras), Some(notes)) = (guestfile::docx_paragraphs(&f.data), guestfile::docx_comments(&f.data)) else { return Ok(false) };
        if Some(&paras.iter().map(|x| casefold(x)).collect::<Vec<_>>()) != task.files_before.get(&rel) {
            return Ok(false);
        }
        let mut count: HashMap<&String, i64> = HashMap::new();
        for n in &notes {
            *count.entry(n).or_default() += 1;
        }
        for b in before {
            *count.entry(b).or_default() -= 1;
        }
        if count.values().any(|n| *n < 0) {
            return Ok(false);
        }
        let new: Vec<String> = notes
            .iter()
            .filter(|n| {
                let c = count.get_mut(n).unwrap();
                if *c > 0 {
                    *c -= 1;
                    true
                } else {
                    false
                }
            })
            .map(|x| casefold(&x.split_whitespace().collect::<Vec<_>>().join(" ")))
            .collect();
        let want = casefold(&task.fmt(comment)?.split_whitespace().collect::<Vec<_>>().join(" "));
        return Ok(new == [want]);
    }
    if let Some(append) = &c.append {
        if let Some(src) = &c.source {
            let src = task.fmt(src)?;
            let s = guestfile::read(&owner, &src);
            let unchanged = s.is_some_and(|s| s.exists && Some(&s.data) == task.sources_before.get(&src));
            if !task.files_absent.as_ref().is_some_and(|a| a.contains(&rel)) || !unchanged {
                return Ok(false);
            }
        }
        let f = guestfile::read(&owner, &rel);
        let Some(before) = task.files_before.get(&rel).cloned() else { return Ok(false) };
        let Some(f) = f.filter(|f| f.exists && f.written_ms >= task.started_ms) else { return Ok(false) };
        if rel.to_lowercase().ends_with(".xlsx") {
            let Some(got) = doc_lines(&rel, &f.data) else { return Ok(false) };
            let mut want = below(&before, &task.fmt(append)?);
            let gotset: BTreeSet<&String> = got.iter().collect();
            let beforeset: BTreeSet<&String> = before.iter().collect();
            let mut new: Vec<String> = gotset.difference(&beforeset).map(|x| fold_value(x)).collect();
            new.sort();
            want.sort();
            return Ok(!want.is_empty() && beforeset.is_subset(&gotset) && new == want);
        }
        let add = lines(&task.fmt(append)?);
        let ok = !add.is_empty() && doc_lines(&rel, &f.data) == Some([before, add].concat());
        let attaches: Vec<String> = task.template.attaches().iter().map(|a| task.fmt(a)).collect::<Result<_>>()?;
        if ok && task.sources_before.contains_key(&rel) && attaches.contains(&rel) {
            task.sources_before.insert(rel, f.data);
        }
        return Ok(ok);
    }
    if !task.files_absent.as_ref().is_some_and(|a| a.contains(&rel)) {
        return Ok(false);
    }
    let f = guestfile::read(&owner, &rel);
    if let Some(from) = &c.moved_from {
        let mut gone_paths: Vec<String> = Vec::new();
        for r in std::iter::once(from).chain(&task.template.removes) {
            if !gone_paths.contains(r) {
                gone_paths.push(r.clone());
            }
        }
        let mut all_gone = true;
        for r in gone_paths {
            all_gone &= guestfile::read(&owner, &task.fmt(&r)?).is_some_and(|s| !s.exists);
        }
        let from = task.fmt(from)?;
        let moved = f.is_some_and(|f| f.exists && Some(&f.data) == task.sources_before.get(&from));
        return Ok(moved && all_gone);
    }
    if let Some(name) = &c.download {
        let want = download_bytes(&task.fmt(name)?);
        return Ok(f.is_some_and(|f| f.exists && Some(f.data) == want));
    }
    if c.extract_of.is_some() {
        return Ok(f.is_some_and(|f| f.exists && Some(&f.data) == task.sources_before.get(&rel)));
    }
    if c.attachment_of.is_some() {
        return Ok(f.is_some_and(|f| f.exists && Some(&sha256_hex(&f.data)) == task.saved_sha.get(&rel)));
    }
    let Some(f) = f.filter(|f| f.exists && f.written_ms >= task.started_ms) else { return Ok(false) };
    if let Some(src) = &c.export_of {
        let src = task.fmt(src)?;
        let s = guestfile::read(&owner, &src);
        let unchanged = s.as_ref().is_some_and(|s| s.exists && Some(&s.data) == task.sources_before.get(&src));
        let sdata = s.map(|s| s.data).unwrap_or_default();
        let l = rel.to_lowercase();
        if l.ends_with(".zip") {
            return Ok(unchanged && zip_holds(&f.data, &leaf(&src), &sdata));
        }
        if l.ends_with(".xlsx") {
            let (cells, want) = (guestfile::xlsx_cells(&f.data), csv_cells(&sdata));
            return Ok(unchanged && !want.is_empty() && cells.is_some_and(|c| fold(&c) == fold(&want)));
        }
        let tail = &f.data[f.data.len().saturating_sub(1024)..];
        return Ok(unchanged && f.data.starts_with(b"%PDF-") && tail.windows(5).any(|w| w == b"%%EOF"));
    }
    if let Some(cells) = &c.cells {
        let (got, want) = (guestfile::xlsx_cells(&f.data), guestfile::grid_cells(&task.fmt(cells)?));
        return Ok(!want.is_empty() && got.is_some_and(|g| fold(&g) == fold(&want)));
    }
    if let Some(p) = &c.paragraphs {
        let want = lines(&task.fmt(p)?);
        return Ok(!want.is_empty() && doc_lines(&rel, &f.data) == Some(want));
    }
    let slides = guestfile::pptx_slides(&f.data);
    let mut want = Vec::new();
    for s in c.slides.iter().flatten() {
        let w = lines(&task.fmt(s)?);
        if !w.is_empty() {
            want.push(w);
        }
    }
    Ok(!want.is_empty() && slides.is_some_and(|s| s.iter().map(|x| x.iter().map(|y| casefold(y)).collect::<Vec<_>>()).collect::<Vec<_>>() == want))
}

#[cfg(feature = "intranet")]
pub(crate) fn download_bytes(name: &str) -> Option<Vec<u8>> {
    crate::intranet::download_bytes(name)
}

/// Without the intranet add-on a download is unknown, so its check never passes.
#[cfg(not(feature = "intranet"))]
pub(crate) fn download_bytes(_name: &str) -> Option<Vec<u8>> {
    None
}
