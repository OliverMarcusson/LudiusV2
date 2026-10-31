use tonic::Status;

/// The gRPC status classes the C# agent mapped exceptions to.
#[derive(Debug)]
pub enum E {
    Arg(String),
    NotFound(String),
    Denied(String),
    Down(String),
    Failed(String),
    /// A PVE task ran and failed: its effect is settled, unlike a lost call.
    Task(String),
}

pub type R<T> = Result<T, E>;

impl std::fmt::Display for E {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let (E::Arg(m) | E::NotFound(m) | E::Denied(m) | E::Down(m) | E::Failed(m) | E::Task(m)) = self;
        f.write_str(m)
    }
}

impl From<E> for Status {
    fn from(e: E) -> Status {
        match e {
            E::Arg(m) => Status::invalid_argument(m),
            E::NotFound(m) => Status::not_found(m),
            E::Denied(m) => Status::permission_denied(m),
            E::Down(m) => Status::unavailable(m),
            E::Failed(m) | E::Task(m) => {
                eprintln!("agent call failed: {m}");
                Status::internal(m)
            }
        }
    }
}

pub fn down(e: impl std::fmt::Display) -> E {
    E::Down(e.to_string())
}
