//! Chat outcome checks (Mattermost). ponytail: not ported yet; every check reads as unknown, so chat tasks
//! never claim done. Port with react (step 4b) as an optional crate, like ludius-intranet.

/// sha256 of each file `user` shared with `other` named `name` since `since_ms`; None = unknown.
pub fn shared(_user: &str, _other: &str, _name: &str, _since_ms: i64, _posted: bool) -> Option<Vec<String>> {
    None
}

/// Posts by `user` in `where` (a user, or ~channel) with exactly `message` since `since_ms`; None = unknown.
pub fn posted(_user: &str, _where: &str, _message: &str, _since_ms: i64) -> Option<i64> {
    None
}
