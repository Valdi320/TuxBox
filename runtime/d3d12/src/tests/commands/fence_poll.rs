use super::poll;
use std::cell::Cell;

#[test]
fn stale_wakeup_does_not_release_unfinished_prefix() {
    let calls = Cell::new(0);
    let result = poll(7, || if calls.get() >= 3 { 7 } else { 6 },
        || Ok(()), || { calls.set(calls.get() + 1); Ok(()) }, || "removed");
    assert_eq!(result, Ok(()));
    assert_eq!(calls.get(), 3);
}

#[test]
fn missing_notification_still_observes_device_loss() {
    let calls = Cell::new(0);
    let result = poll(7, || 6,
        || if calls.get() >= 2 { Err("device lost") } else { Ok(()) },
        || { calls.set(calls.get() + 1); Ok(()) }, || "removed");
    assert_eq!(result, Err("device lost"));
    assert_eq!(calls.get(), 2);
}

#[test]
fn removed_sentinel_is_never_success() {
    assert_eq!(poll(7, || u64::MAX, || Ok(()), || panic!("waited"), || "removed"),
        Err("removed"));
    assert_eq!(poll(7, || u64::MAX, || Err("device lost"), || panic!("waited"), || "removed"),
        Err("device lost"));
}

#[test]
fn already_complete_and_wait_failure() {
    assert_eq!(poll(7, || 7, || Ok(()), || panic!("waited"), || "removed"), Ok(()));
    assert_eq!(poll(7, || 6, || Ok(()), || Err("wait failed"), || "removed"), Err("wait failed"));
}
