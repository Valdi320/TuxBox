//! A fence notification is a wakeup, not proof of completion.
/// Check device removal even if a driver never signals the registered event.
/// The caller provides a bounded wait; healthy slow work is not timed out.
pub(super) fn poll<E>(
    value: u64,
    mut completed: impl FnMut() -> u64,
    mut device_status: impl FnMut() -> Result<(), E>,
    mut wait: impl FnMut() -> Result<(), E>,
    removed_without_reason: impl Fn() -> E,
) -> Result<(), E> {
    loop {
        let observed = completed();
        device_status()?;
        if observed == u64::MAX {
            return Err(removed_without_reason());
        }
        if observed >= value {
            return Ok(());
        }
        wait()?;
    }
}

#[cfg(test)]
#[path = "../../tests/commands/fence_poll.rs"]
mod tests;
