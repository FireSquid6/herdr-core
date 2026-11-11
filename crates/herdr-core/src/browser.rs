//! Browser ownership decisions, separate from process/network IO.
//!
//! Not exposed by the API: the real native rendering/DevTools gate must pass
//! before a browser capability is advertised. A future worker actor must apply
//! revocation (including queued frames and DevTools) before publishing a grant,
//! and check authorization again immediately before executing queued work.

pub const LEASE_MS: u64 = 30_000;

pub fn valid_workspace(id: &str) -> bool {
    (1..=128).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}

/// Worker incarnation is generated afresh by the runtime, never supplied by a viewer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Generation {
    incarnation: u128,
    sequence: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Denied {
    Owned,
    ConfirmationRequired,
    NotOwner,
    StaleGeneration,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Grant {
    pub generation: Generation,
    pub revoked: Option<u64>,
}

struct Owner {
    connection: u64,
    deadline: u64,
}

/// Times are monotonic milliseconds since worker start; connection IDs are
/// assigned by the server. This state owns no process, so revocation cannot
/// stop a browser or change its viewport.
pub struct Lease {
    generation: Generation,
    owner: Option<Owner>,
}

impl Lease {
    pub fn new(incarnation: u128) -> Self {
        Self { generation: Generation { incarnation, sequence: 0 }, owner: None }
    }

    pub fn acquire(&mut self, connection: u64, now: u64, force: bool, confirmed: bool) -> Result<Grant, Denied> {
        if force && !confirmed {
            return Err(Denied::ConfirmationRequired);
        }
        let current = self.owner.as_ref().filter(|owner| now < owner.deadline);
        if current.is_some_and(|owner| owner.connection != connection) && !force {
            return Err(Denied::Owned);
        }
        let same = current.is_some_and(|owner| owner.connection == connection);
        let revoked = self.owner.as_ref().filter(|_| !same).map(|owner| owner.connection);
        if !same {
            self.generation.sequence = self.generation.sequence.checked_add(1).expect("lease generation exhausted");
        }
        self.owner = Some(Owner { connection, deadline: now.saturating_add(LEASE_MS) });
        Ok(Grant { generation: self.generation, revoked })
    }

    pub fn authorize(&self, connection: u64, generation: Generation, now: u64) -> Result<(), Denied> {
        if !self.owner.as_ref().is_some_and(|owner| owner.connection == connection && now < owner.deadline) {
            return Err(Denied::NotOwner);
        }
        if generation != self.generation {
            return Err(Denied::StaleGeneration);
        }
        Ok(())
    }

    pub fn heartbeat(&mut self, connection: u64, generation: Generation, now: u64) -> Result<(), Denied> {
        self.authorize(connection, generation, now)?;
        self.owner.as_mut().unwrap().deadline = now.saturating_add(LEASE_MS);
        Ok(())
    }

    pub fn release(&mut self, connection: u64, generation: Generation, now: u64) -> Result<(), Denied> {
        self.authorize(connection, generation, now)?;
        self.owner = None;
        Ok(())
    }

    pub fn disconnect(&mut self, connection: u64) -> Option<u64> {
        if self.owner.as_ref().is_some_and(|owner| owner.connection == connection) {
            self.owner.take().map(|owner| owner.connection)
        } else {
            None
        }
    }

    /// Returns the connection whose channels must be revoked, once only.
    pub fn expire(&mut self, now: u64) -> Option<u64> {
        if self.owner.as_ref().is_some_and(|owner| now >= owner.deadline) {
            self.owner.take().map(|owner| owner.connection)
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workspace_identity_is_opaque_and_cannot_escape_its_storage_directory() {
        for id in ["ws_A-1", "a", &"a".repeat(128)] {
            assert!(valid_workspace(id), "{id}");
        }
        for id in ["", ".", "..", "../other", "a/b", "a\\b", "/absolute", "a\0b", "a b", "é", &"a".repeat(129)] {
            assert!(!valid_workspace(id), "{id:?}");
        }
    }

    #[test]
    fn release_and_disconnect_cannot_revoke_a_replacement_controller() {
        let mut lease = Lease::new(123);
        let first = lease.acquire(1, 0, false, false).unwrap();
        let second = lease.acquire(2, 1, true, true).unwrap();
        assert_eq!(lease.release(1, first.generation, 2), Err(Denied::NotOwner));
        assert_eq!(lease.disconnect(1), None);
        assert!(lease.authorize(2, second.generation, 2).is_ok());
        lease.release(2, second.generation, 2).unwrap();
        let third = lease.acquire(2, 3, false, false).unwrap();
        assert_ne!(third.generation, second.generation);
        assert_eq!(lease.release(2, second.generation, 3), Err(Denied::StaleGeneration));
        assert_eq!(lease.disconnect(2), Some(2));
        assert_eq!(lease.authorize(2, third.generation, 3), Err(Denied::NotOwner));
        let mut replacement_worker = Lease::new(456);
        let fresh = replacement_worker.acquire(1, 0, false, false).unwrap();
        assert_ne!(fresh.generation, first.generation, "counter reset must not replay old requests");
    }

    #[test]
    fn heartbeats_extend_only_a_current_lease_and_expiry_requires_reacquisition() {
        let mut lease = Lease::new(123);
        let first = lease.acquire(1, 0, false, false).unwrap();
        assert_eq!(lease.acquire(1, 1, false, false).unwrap().generation, first.generation);
        assert_eq!(lease.heartbeat(2, first.generation, 2), Err(Denied::NotOwner));
        lease.heartbeat(1, first.generation, 29_000).unwrap();
        assert!(lease.authorize(1, first.generation, 58_999).is_ok());
        assert_eq!(lease.heartbeat(1, first.generation, 59_000), Err(Denied::NotOwner));
        assert_eq!(lease.expire(59_000), Some(1));
        assert_eq!(lease.expire(59_000), None);
        let second = lease.acquire(1, 59_001, false, false).unwrap();
        assert_ne!(second.generation, first.generation);
        assert_eq!(lease.authorize(1, first.generation, 59_001), Err(Denied::StaleGeneration));
    }

    #[test]
    fn ownership_is_exclusive_and_confirmed_takeover_invalidates_queued_work() {
        let mut lease = Lease::new(123);
        let first = lease.acquire(1, 0, false, false).unwrap();
        assert_eq!(first.revoked, None);
        assert_eq!(lease.acquire(2, 1, false, false), Err(Denied::Owned));
        assert_eq!(lease.acquire(2, 1, true, false), Err(Denied::ConfirmationRequired));
        assert!(lease.authorize(1, first.generation, 1).is_ok());
        let second = lease.acquire(2, 2, true, true).unwrap();
        assert_eq!(second.revoked, Some(1));
        assert_ne!(first.generation, second.generation);
        assert_eq!(lease.authorize(1, first.generation, 2), Err(Denied::NotOwner));
        assert_eq!(lease.authorize(2, first.generation, 2), Err(Denied::StaleGeneration));
        assert!(lease.authorize(2, second.generation, 2).is_ok());
    }
}
