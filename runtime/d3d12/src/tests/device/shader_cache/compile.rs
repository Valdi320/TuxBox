use super::*;

#[test]
fn a_directory_is_listed_once_and_extended() {
    let dir = std::env::temp_dir().join(format!("tuxbox-names-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("a.sha256"), b"").unwrap();
    let names = Names::new();
    assert!(names.of(&dir).contains("a.sha256"));
    fs::write(dir.join("b.sha256"), b"").unwrap();
    assert!(!names.of(&dir).contains("b.sha256"));
    names.of(&dir).insert("b.sha256".into());
    assert!(names.of(&dir).contains("b.sha256"));
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_pause_stays_a_fraction_of_the_time_already_waited() {
    let ms = Duration::from_millis;
    assert_eq!(pause_after(Duration::ZERO), ms(1));
    assert_eq!(pause_after(ms(16)), ms(2));
    assert_eq!(pause_after(ms(40)), ms(5));
    assert_eq!(pause_after(ms(200)), ms(25));
    assert_eq!(pause_after(Duration::from_secs(60)), ms(25));
}

#[test]
fn a_reply_is_looked_for_once_the_host_removes_the_request() {
    let dir = std::env::temp_dir().join(format!("tuxbox-reply-due-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let request = dir.join("request.dxbc");
    fs::write(&request, b"x").unwrap();
    assert!(!reply_is_due(&request, Duration::ZERO));
    assert!(reply_is_due(&request, UNPROMPTED_LOOK));
    fs::remove_file(&request).unwrap();
    assert!(reply_is_due(&request, Duration::ZERO));
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_reply_ends_the_wait() {
    let mut pauses = 0;
    let mut looks = 0;
    let reply = await_reply(Duration::from_secs(90), |_| pauses += 1, || {
        looks += 1;
        (looks == 3).then_some("reply")
    });
    assert_eq!((reply, looks, pauses), (Some("reply"), 3, 2));
}

#[test]
fn waiting_ends_when_the_limit_passes() {
    let mut looks = 0;
    let reply = await_reply(Duration::ZERO, |_| panic!("paused past the limit"), || {
        looks += 1;
        None::<()>
    });
    assert_eq!((reply, looks), (None, 1));
}
