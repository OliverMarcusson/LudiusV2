//! Chat add-on: what the chat server holds (unread posts, a conversation, shared files, sent posts) and the
//! pair reset. Backends are features: `mattermost`. Without one, every read is unknown (None), never empty.

#[cfg(feature = "mattermost")]
mod mattermost;
#[cfg(feature = "mattermost")]
pub use mattermost::*;

/// No backend compiled in: unknown, never empty.
#[cfg(not(feature = "mattermost"))]
mod none {
    use super::Post;
    pub fn unread(_: &str) -> Option<Vec<Post>> {
        None
    }
    pub fn conversation(_: &str, _: &str) -> Option<(Vec<Post>, i64)> {
        None
    }
    pub fn shared(_: &str, _: &str, _: &str, _: i64, _: bool) -> Option<Vec<String>> {
        None
    }
    pub fn posted(_: &str, _: &str, _: &str, _: i64) -> Option<i64> {
        None
    }
    pub fn reset(_: &str, _: &str) -> Option<usize> {
        None
    }
}
#[cfg(not(feature = "mattermost"))]
pub use none::*;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Post {
    pub id: String,
    pub sender: String,
    pub message: String,
    pub at: i64,
    /// Team channel display name; empty for a direct message.
    pub channel: String,
}

impl Post {
    /// The conversation a post belongs to: `~channel`, or the sender of a direct message.
    pub fn key(&self) -> String {
        if self.channel.is_empty() { self.sender.clone() } else { format!("~{}", self.channel) }
    }
}
