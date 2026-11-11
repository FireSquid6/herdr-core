//! Scratch isolation prerequisite for real process/browser tests.
#![cfg(unix)] // Windows uses tests/windows.rs, not the Unix socket harness.

mod common;

#[test]
fn session_uses_the_explicit_scratch_root_and_private_temporary_directory() {
    let session = common::Session::new();
    let parent = std::env::var_os("HC_TEST_ROOT").map(std::path::PathBuf::from).unwrap_or("/var/tmp".into());
    assert_eq!(session.root.parent(), Some(parent.as_path()));
    assert_eq!(
        session.cmd().get_envs().find(|(key, _)| *key == "TMPDIR").and_then(|(_, value)| value),
        Some(session.root.join("tmp").as_os_str())
    );
    assert!(session.root.join("tmp").is_dir());
}
