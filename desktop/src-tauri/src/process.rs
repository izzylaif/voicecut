//! Child processes that stop together, on macOS, Linux and Windows.
//!
//! POSIX: each tool and the engine lead their own process group, stopped
//! with a signal to the group.  Windows: they start without a console window
//! (their own children share that hidden console instead of opening windows)
//! and a whole tree is ended with `taskkill /T`.

use std::process::Command;

/// `CREATE_NO_WINDOW`.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Start `command` as the root of its own tree, with no console window.
pub fn detach(command: &mut Command) -> &mut Command {
    host_env(command);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

/// No console window for a short command (Windows).
pub fn quiet(command: &mut Command) -> &mut Command {
    host_env(command);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

/// Leave out what an AppImage's launcher pointed into the image (its
/// libraries, GTK and GStreamer): VoiceCut's tools and the programs it opens
/// bring their own.  Variables set on `command` itself stay.
fn host_env(command: &mut Command) {
    #[cfg(target_os = "linux")]
    if let Some(appdir) = std::env::var_os("APPDIR") {
        let appdir = appdir.to_string_lossy().into_owned();
        let set: Vec<_> = command
            .get_envs()
            .map(|(name, _)| name.to_owned())
            .collect();
        for (name, value) in std::env::vars_os() {
            let value = value.to_string_lossy();
            if name == "APPDIR" || set.contains(&name) || !value.contains(appdir.as_str()) {
                continue;
            }
            let kept: Vec<&str> = value
                .split(':')
                .filter(|entry| !entry.is_empty() && !entry.starts_with(appdir.as_str()))
                .collect();
            if kept.is_empty() {
                command.env_remove(&name);
            } else {
                command.env(&name, kept.join(":"));
            }
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = command;
}

/// End the tree started by `pid`: SIGTERM (SIGKILL with `force`) to its
/// process group on POSIX; `taskkill /T /F` on Windows, which cannot ask
/// politely.
pub fn stop_tree(pid: u32, force: bool) {
    #[cfg(unix)]
    unsafe {
        libc::killpg(
            pid as libc::pid_t,
            if force { libc::SIGKILL } else { libc::SIGTERM },
        );
    }
    #[cfg(windows)]
    {
        let _ = force;
        let _ =
            quiet(Command::new("taskkill").args(["/PID", &pid.to_string(), "/T", "/F"])).output();
    }
}
