//! Linux: what an AppImage sets up for itself.
//!
//! The .deb installs VoiceCut's menu entry, the `.vcproj` file type and its
//! icon into /usr/share.  Nothing installs an AppImage, so each time it starts
//! it writes the same into the user's `$XDG_DATA_HOME` (~/.local/share),
//! pointing at wherever the AppImage file is now: VoiceCut shows up among the
//! applications and opens projects from the file manager.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const DESKTOP_FILE: &str = "voicecut.desktop";
const MIME_TYPE: &str = "application-x-voicecut-project";

/// Register the running AppImage, if VoiceCut runs from one.  `resources` is
/// the app's resource directory (its `linux/` holds the icons and file type).
pub fn integrate_appimage(resources: &Path) {
    let Some(appimage) = std::env::var_os("APPIMAGE").map(PathBuf::from) else {
        return;
    };
    let Some(data) = data_home() else {
        return;
    };
    let linux = resources.join("linux");
    let entry = format!(
        "[Desktop Entry]\n\
         Type=Application\n\
         Name=VoiceCut\n\
         Comment=Edits retake-heavy narration automatically\n\
         Exec={} %F\n\
         TryExec={}\n\
         Icon=voicecut\n\
         Categories=AudioVideo;Video;\n\
         MimeType=application/x-voicecut-project;\n\
         StartupWMClass=voicecut\n\
         Terminal=false\n",
        exec_argument(&appimage),
        appimage.display(),
    );
    let mime_changed = install(
        &data.join("mime/packages/voicecut.xml"),
        &fs::read(linux.join("voicecut-mime.xml")).unwrap_or_default(),
    );
    let icons = data.join("icons/hicolor/256x256");
    let icons_changed = install(
        &icons.join("apps/voicecut.png"),
        &fs::read(linux.join("voicecut.png")).unwrap_or_default(),
    ) | install(
        &icons.join(format!("mimetypes/{MIME_TYPE}.png")),
        &fs::read(linux.join(format!("{MIME_TYPE}.png"))).unwrap_or_default(),
    );
    let entry_changed = install(
        &data.join("applications").join(DESKTOP_FILE),
        entry.as_bytes(),
    );
    if mime_changed {
        refresh("update-mime-database", &data.join("mime"));
    }
    if icons_changed {
        refresh("gtk-update-icon-cache", &data.join("icons/hicolor"));
    }
    if entry_changed {
        refresh("update-desktop-database", &data.join("applications"));
    }
}

/// WebKitGTK's DMA-BUF renderer leaves windows blank with NVIDIA's driver;
/// use its older renderer there, unless the user decided otherwise.  Call it
/// before any window exists.
pub fn prepare_webkit() {
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none()
        && Path::new("/proc/driver/nvidia").exists()
    {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
}

fn data_home() -> Option<PathBuf> {
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|home| Path::new(&home).join(".local/share")))
}

/// Write `contents` to `path` unless it already holds them; whether it wrote.
fn install(path: &Path, contents: &[u8]) -> bool {
    if contents.is_empty() || fs::read(path).is_ok_and(|current| current == contents) {
        return false;
    }
    path.parent()
        .is_some_and(|parent| fs::create_dir_all(parent).is_ok())
        && fs::write(path, contents).is_ok()
}

/// Rebuild a cache the desktop reads; desktops without the tool do without.
fn refresh(tool: &str, directory: &Path) {
    let _ = crate::process::quiet(Command::new(tool).arg(directory)).output();
}

/// A path as one argument of a desktop entry's `Exec` key: quoted when it
/// needs to be, then escaped again for the key's string value.
fn exec_argument(path: &Path) -> String {
    let path = path.to_string_lossy();
    let reserved = |c: char| " \t\n\"'\\><~|&;$*?#()`".contains(c);
    let quoted = if path.contains(reserved) {
        let mut quoted = String::from("\"");
        for c in path.chars() {
            if matches!(c, '"' | '`' | '$' | '\\') {
                quoted.push('\\');
            }
            quoted.push(c);
        }
        quoted.push('"');
        quoted
    } else {
        path.into_owned()
    };
    quoted.replace('\\', "\\\\")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exec_arguments_are_quoted_when_needed() {
        assert_eq!(
            exec_argument(Path::new("/opt/VoiceCut.AppImage")),
            "/opt/VoiceCut.AppImage"
        );
        assert_eq!(
            exec_argument(Path::new("/home/a b/VoiceCut.AppImage")),
            "\"/home/a b/VoiceCut.AppImage\""
        );
        assert_eq!(exec_argument(Path::new("/x/$y")), "\"/x/\\\\$y\"");
    }
}
