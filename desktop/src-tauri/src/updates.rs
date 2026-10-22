//! Updates of the app itself, from the project's GitHub Releases
//! (`tauri-plugin-updater`; `plugins.updater` in tauri.conf.json).
//!
//! The app looks for a newer release a little after launch and every few
//! hours, but not while Voice Cut or an export runs.  When there is one, the
//! pages show a download button: it downloads the update, saves the open
//! documents as quitting does, installs it and restarts VoiceCut with the same
//! documents.  With "Install updates automatically" (the default) the update
//! downloads in the background and installs when VoiceCut quits, or at the
//! next launch if VoiceCut did not get to quit.  The engine follows at that
//! launch, when the source bundled with the new app differs (`runtime`).
//!
//! Every package is checked against the public key in tauri.conf.json before
//! it is installed: only builds signed with the project's key install.

use std::cmp::Ordering;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use semver::Version;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::{Update, Updater, UpdaterExt};

use crate::paths::Paths;
use crate::{AppState, Leaving, Role};

const FIRST_CHECK: Duration = Duration::from_secs(10);
const CHECK_EVERY: Duration = Duration::from_secs(4 * 60 * 60);
/// How soon to look again when Voice Cut or an export was running.
const BUSY_RETRY: Duration = Duration::from_secs(5 * 60);
/// An update's restart reopens the documents if it comes within this time.
const REOPEN_WITHIN: Duration = Duration::from_secs(10 * 60);
const PACKAGE: &str = "package";
const RELEASE: &str = "release.json";

/// Whether `offered` is an update for `current`: a later version, ignoring
/// build metadata.  A pre-release is offered only to a pre-release.
pub fn newer(current: &Version, offered: &Version) -> bool {
    if !offered.pre.is_empty() && current.pre.is_empty() {
        return false;
    }
    offered.cmp_precedence(current) == Ordering::Greater
}

#[derive(Default, Clone, Copy, PartialEq, Debug)]
enum Phase {
    #[default]
    Idle,
    Downloading,
    /// Downloaded and verified, in the data directory.
    Ready,
    Installing,
}

#[derive(Default)]
pub struct Updates {
    inner: Mutex<Inner>,
    /// The download button's install is under way.
    installing: AtomicBool,
}

#[derive(Default)]
struct Inner {
    update: Option<Update>,
    phase: Phase,
    progress: Option<f64>,
    error: Option<String>,
}

/// What the pages show: the download button and the setting.
#[derive(Serialize, Clone, Debug)]
pub struct UpdateView {
    /// The running version.
    current: String,
    /// The newer version on offer, if any.
    version: Option<String>,
    /// "none", "available", "downloading", "ready" or "installing".
    state: &'static str,
    /// How much of the download arrived, 0 to 1, when its size is known.
    progress: Option<f64>,
    error: Option<String>,
    automatic: bool,
}

pub fn view(app: &AppHandle) -> UpdateView {
    let state = app.state::<AppState>();
    let automatic = state.preferences.lock().unwrap().automatic_updates;
    let inner = state.updates.inner.lock().unwrap();
    let version = inner.update.as_ref().map(|update| update.version.clone());
    UpdateView {
        current: app.package_info().version.to_string(),
        state: match (&version, inner.phase) {
            (None, _) => "none",
            (_, Phase::Idle) => "available",
            (_, Phase::Downloading) => "downloading",
            (_, Phase::Ready) => "ready",
            (_, Phase::Installing) => "installing",
        },
        version,
        progress: inner.progress,
        error: inner.error.clone(),
        automatic,
    }
}

fn changed(app: &AppHandle) {
    let _ = app.emit("update", view(app));
}

fn set_phase(app: &AppHandle, phase: Phase, error: Option<String>) {
    {
        let state = app.state::<AppState>();
        let mut inner = state.updates.inner.lock().unwrap();
        inner.phase = phase;
        inner.progress = None;
        inner.error = error;
    }
    changed(app);
}

fn text(error: impl ToString) -> String {
    error.to_string()
}

/// A .deb (or .rpm) update asks for the administrator's password, so it
/// installs only when the user asks for it.
fn installs_by_itself() -> bool {
    use tauri::utils::{config::BundleType, platform::bundle_type};
    !matches!(bundle_type(), Some(BundleType::Deb | BundleType::Rpm))
}

/// An installed app can update itself; a development binary cannot, and a
/// test build only from the release it is pointed at (a release's app is not
/// a test build).
fn updatable() -> bool {
    if cfg!(feature = "automation") && std::env::var_os("VOICECUT_UPDATE_ENDPOINT").is_none() {
        return false;
    }
    if cfg!(target_os = "macos") {
        // The updater replaces the app bundle around the binary.
        return std::env::current_exe().is_ok_and(|exe| {
            exe.parent()
                .is_some_and(|dir| dir.ends_with("Contents/MacOS"))
        });
    }
    tauri::utils::platform::bundle_type().is_some()
}

fn updater(app: &AppHandle) -> Result<Updater, String> {
    #[allow(unused_mut)]
    let mut builder = app.updater_builder();
    // Test builds can be pointed at a local release (desktop/README.md).
    #[cfg(feature = "automation")]
    if let Ok(endpoint) = std::env::var("VOICECUT_UPDATE_ENDPOINT") {
        let endpoint = endpoint.parse().map_err(text)?;
        builder = builder.endpoints(vec![endpoint]).map_err(text)?;
    }
    builder.build().map_err(text)
}

// -- the downloaded package ----------------------------------------------------------

#[derive(Serialize, Deserialize)]
struct Reopen {
    paths: Vec<PathBuf>,
    /// Seconds since 1970.
    written: u64,
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or_default()
}

/// The release a package in the data directory belongs to, and the package.
fn cached(paths: &Paths) -> Option<(Value, PathBuf)> {
    let directory = paths.updates();
    let release = std::fs::read_to_string(directory.join(RELEASE)).ok()?;
    let release = serde_json::from_str(&release).ok()?;
    let package = directory.join(PACKAGE);
    package.is_file().then_some((release, package))
}

fn cached_version(paths: &Paths) -> Option<String> {
    cached(paths).and_then(|(release, _)| release["version"].as_str().map(str::to_string))
}

fn keep(paths: &Paths, update: &Update, bytes: &[u8]) -> Result<(), String> {
    let directory = paths.updates();
    forget(paths);
    std::fs::create_dir_all(&directory).map_err(text)?;
    let partial = directory.join(format!("{PACKAGE}.partial"));
    std::fs::write(&partial, bytes).map_err(text)?;
    std::fs::rename(&partial, directory.join(PACKAGE)).map_err(text)?;
    let release = serde_json::to_vec_pretty(&update.raw_json).map_err(text)?;
    std::fs::write(directory.join(RELEASE), release).map_err(text)
}

fn forget(paths: &Paths) {
    let _ = std::fs::remove_dir_all(paths.updates());
}

/// The update on offer and its package, if it was downloaded.
fn downloaded(app: &AppHandle) -> Option<(Update, Vec<u8>)> {
    let state = app.state::<AppState>();
    let update = state.updates.inner.lock().unwrap().update.clone()?;
    if cached_version(&state.paths).as_deref() != Some(update.version.as_str()) {
        return None;
    }
    let (_, package) = cached(&state.paths)?;
    let bytes = std::fs::read(package).ok()?;
    Some((update, bytes))
}

// -- checking and downloading ----------------------------------------------------------

/// Ask the release server; remember what it offers.  The version on offer,
/// if any.
fn look(app: &AppHandle) -> Result<Option<String>, String> {
    let found = tauri::async_runtime::block_on(updater(app)?.check()).map_err(text)?;
    let state = app.state::<AppState>();
    let version = found.as_ref().map(|update| update.version.clone());
    if version.is_none() {
        // Whatever was downloaded is not newer any more.
        forget(&state.paths);
    }
    let ready = version.is_some() && cached_version(&state.paths) == version;
    {
        let mut inner = state.updates.inner.lock().unwrap();
        if matches!(inner.phase, Phase::Downloading | Phase::Installing) {
            return Ok(version);
        }
        inner.phase = if ready { Phase::Ready } else { Phase::Idle };
        inner.progress = None;
        inner.error = None;
        inner.update = found;
    }
    changed(app);
    Ok(version)
}

/// Download the update on offer, check its signature and keep it in the
/// data directory, reporting progress to the pages.
fn download(app: &AppHandle) -> Result<(Update, Vec<u8>), String> {
    if let Some(ready) = downloaded(app) {
        return Ok(ready);
    }
    let state = app.state::<AppState>();
    let update = {
        let mut inner = state.updates.inner.lock().unwrap();
        let update = inner
            .update
            .clone()
            .ok_or("there is no update to download")?;
        if inner.phase == Phase::Downloading {
            return Err("the update is already downloading".into());
        }
        inner.phase = Phase::Downloading;
        inner.progress = None;
        inner.error = None;
        update
    };
    changed(app);
    let mut received = 0u64;
    let mut reported = Instant::now();
    let result = tauri::async_runtime::block_on(update.download(
        |chunk, total| {
            received += chunk as u64;
            if reported.elapsed() < Duration::from_millis(200) {
                return;
            }
            reported = Instant::now();
            if let Some(total) = total.filter(|total| *total > 0) {
                let fraction = (received as f64 / total as f64).min(1.0);
                state.updates.inner.lock().unwrap().progress = Some(fraction);
                changed(app);
            }
        },
        || {},
    ))
    .map_err(text)
    .and_then(|bytes| keep(&state.paths, &update, &bytes).map(|()| bytes));
    match result {
        Ok(bytes) => {
            set_phase(app, Phase::Ready, None);
            Ok((update, bytes))
        }
        Err(message) => {
            set_phase(app, Phase::Idle, Some(message.clone()));
            Err(message)
        }
    }
}

/// Whether updates download in the background, to install at quit.
fn downloads_by_itself(app: &AppHandle) -> bool {
    let state = app.state::<AppState>();
    let automatic = state.preferences.lock().unwrap().automatic_updates;
    automatic && installs_by_itself() && !state.quitting.load(AtomicOrdering::SeqCst)
}

/// Look for updates now and every few hours; with automatic updates on,
/// download what is found so it installs at quit.
pub fn start(app: &AppHandle) {
    if !updatable() {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(FIRST_CHECK);
        loop {
            if crate::working(&app) {
                std::thread::sleep(BUSY_RETRY);
                continue;
            }
            match look(&app) {
                Ok(Some(_)) if downloads_by_itself(&app) && !crate::working(&app) => {
                    if let Err(message) = download(&app) {
                        eprintln!("cannot download the update: {message}");
                    }
                }
                Ok(_) => {}
                Err(message) => eprintln!("cannot check for updates: {message}"),
            }
            std::thread::sleep(CHECK_EVERY);
        }
    });
}

pub fn set_automatic(app: &AppHandle, on: bool) -> Result<(), String> {
    let state = app.state::<AppState>();
    {
        let mut preferences = state.preferences.lock().unwrap();
        if preferences.automatic_updates == on {
            return Ok(());
        }
        preferences.automatic_updates = on;
        preferences.save(&state.paths.preferences())?;
    }
    changed(app);
    let waiting = {
        let inner = state.updates.inner.lock().unwrap();
        inner.phase == Phase::Idle && inner.update.is_some()
    };
    if on && waiting && downloads_by_itself(app) && !crate::working(app) {
        let app = app.clone();
        std::thread::spawn(move || {
            let _ = download(&app);
        });
    }
    Ok(())
}

// -- installing ----------------------------------------------------------------------

/// Install a verified package.  On Windows the installer takes over once
/// VoiceCut exits, which this does, and starts VoiceCut again if `restart`.
fn install(update: &Update, bytes: &[u8], restart: bool) -> Result<(), String> {
    update
        .clone()
        .restart_after_install(restart)
        .install(bytes)
        .map_err(|error| {
            let message = error.to_string();
            match std::env::current_exe() {
                Ok(exe) if cfg!(target_os = "macos") && !in_place(&exe) => format!(
                    "{message}\n\nMove VoiceCut to the Applications folder, open it from there and try again."
                ),
                _ => message,
            }
        })
}

/// A Mac app run from a disk image or straight from Downloads runs from a
/// read-only copy, which cannot be replaced.
fn in_place(exe: &Path) -> bool {
    let exe = exe.to_string_lossy();
    !exe.contains("/AppTranslocation/") && !exe.starts_with("/Volumes/")
}

fn write_reopen(file: &Path, paths: Vec<PathBuf>) {
    let reopen = Reopen {
        paths,
        written: now(),
    };
    if let Ok(text) = serde_json::to_vec(&reopen) {
        let _ = std::fs::write(file, text);
    }
}

fn read_reopen(file: &Path) -> Vec<PathBuf> {
    let reopen: Option<Reopen> = std::fs::read_to_string(file)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok());
    let _ = std::fs::remove_file(file);
    match reopen {
        Some(reopen) if now().saturating_sub(reopen.written) <= REOPEN_WITHIN.as_secs() => {
            reopen.paths
        }
        _ => Vec::new(),
    }
}

fn remember_documents(app: &AppHandle) {
    let state = app.state::<AppState>();
    let documents = state
        .roles
        .lock()
        .unwrap()
        .values()
        .filter_map(|role| match role {
            Role::Document { path, .. } => Some(path.clone()),
            _ => None,
        })
        .collect();
    write_reopen(&state.paths.reopen(), documents);
}

/// The documents that were open when an update restarted VoiceCut.
pub fn documents_to_reopen(paths: &Paths) -> Vec<PathBuf> {
    read_reopen(&paths.reopen())
}

/// The download button: download, save every document, install, restart.
pub fn install_now(app: AppHandle) {
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        if state.updates.installing.swap(true, AtomicOrdering::SeqCst) {
            return;
        }
        if install_and_restart(&app).is_err() {
            state
                .updates
                .installing
                .store(false, AtomicOrdering::SeqCst);
        }
    });
}

/// Returns only if it did not restart.
fn install_and_restart(app: &AppHandle) -> Result<(), ()> {
    let state = app.state::<AppState>();
    // A download in the background finishes first.
    while state.updates.inner.lock().unwrap().phase == Phase::Downloading {
        std::thread::sleep(Duration::from_millis(200));
    }
    let (update, bytes) = download(app).map_err(|message| {
        crate::error_dialog(app, "VoiceCut can’t download the update", &message);
    })?;
    if !crate::ready_to_leave(app, Leaving::Update) {
        return Err(());
    }
    set_phase(app, Phase::Installing, None);
    remember_documents(app);
    if cfg!(windows) {
        // The installer replaces the app once it has exited.
        state.quitting.store(true, AtomicOrdering::SeqCst);
        crate::stop_engine(app);
    }
    match install(&update, &bytes, true) {
        Ok(()) => {
            forget(&state.paths);
            state.quitting.store(true, AtomicOrdering::SeqCst);
            crate::stop_engine(app);
            app.restart();
        }
        Err(message) => {
            forget(&state.paths);
            set_phase(app, Phase::Idle, Some(message.clone()));
            crate::error_dialog(app, "VoiceCut can’t install the update", &message);
            if state.quitting.load(AtomicOrdering::SeqCst) {
                // Windows: start again as it was, with the documents.
                app.restart();
            }
            let _ = std::fs::remove_file(state.paths.reopen());
            Err(())
        }
    }
}

/// Quitting: install the downloaded update, if updates install by themselves.
/// The engine has stopped.
pub fn install_on_quit(app: &AppHandle) {
    if !updatable() || !installs_by_itself() {
        return;
    }
    let state = app.state::<AppState>();
    if !state.preferences.lock().unwrap().automatic_updates {
        return;
    }
    if let Some((update, bytes)) = downloaded(app) {
        if let Err(message) = install(&update, &bytes, false) {
            eprintln!("cannot install the update: {message}");
        }
        forget(&state.paths);
    }
}

/// Launching with an update that was downloaded but not installed (VoiceCut
/// was stopped without quitting): install it before anything opens and
/// start the new version.  `documents` are what the launch was to open.
pub fn install_at_launch(app: &AppHandle, paths: &Paths, automatic: bool, documents: &[PathBuf]) {
    if !automatic || !updatable() || !installs_by_itself() {
        return;
    }
    let Some((release, package)) = cached(paths) else {
        return;
    };
    let restored = updater(app).and_then(|updater| updater.restore_update(release).map_err(text));
    let installed = match (restored, std::fs::read(package)) {
        (Ok(Some(update)), Ok(bytes)) => {
            write_reopen(&paths.reopen(), documents.to_vec());
            install(&update, &bytes, true)
        }
        // Not newer than this version any more: installed by hand.
        (Ok(None), _) => Err("already installed".into()),
        (Err(message), _) => Err(message),
        (_, Err(error)) => Err(error.to_string()),
    };
    forget(paths);
    match installed {
        Ok(()) => app.restart(),
        Err(message) => {
            let _ = std::fs::remove_file(paths.reopen());
            eprintln!("not installing the downloaded update: {message}");
        }
    }
}

// -- Check for Updates… ------------------------------------------------------------------

fn tell(app: &AppHandle, title: &str, message: &str) {
    #[cfg(feature = "automation")]
    if crate::automation::next_answer().is_some() {
        return;
    }
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Info)
        .blocking_show();
}

fn offer(app: &AppHandle, title: &str, message: &str) -> bool {
    #[cfg(feature = "automation")]
    if let Some(answer) = crate::automation::next_answer() {
        return answer.as_bool().unwrap_or(false);
    }
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Install and Restart".into(),
            "Later".into(),
        ))
        .blocking_show()
}

/// The menu's Check for Updates…: say what was found, offer to install it.
pub fn check_now(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let current = app.package_info().version.to_string();
        if !updatable() {
            tell(
                &app,
                "VoiceCut can’t update itself",
                "This copy of VoiceCut was not installed from a release.",
            );
            return;
        }
        match look(&app) {
            Err(message) => crate::error_dialog(&app, "VoiceCut can’t check for updates", &message),
            Ok(None) => tell(
                &app,
                "VoiceCut is up to date",
                &format!("VoiceCut {current} is the newest version."),
            ),
            Ok(Some(version)) => {
                let installing = app
                    .state::<AppState>()
                    .updates
                    .installing
                    .load(AtomicOrdering::SeqCst);
                if !installing
                    && offer(
                        &app,
                        &format!("VoiceCut {version} is available"),
                        &format!(
                            "You have VoiceCut {current}. VoiceCut saves your projects, installs the update and opens them again."
                        ),
                    )
                {
                    install_now(app.clone());
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn version(text: &str) -> Version {
        Version::parse(text).unwrap()
    }

    fn is_update(current: &str, offered: &str) -> bool {
        newer(&version(current), &version(offered))
    }

    #[test]
    fn later_versions_are_updates() {
        assert!(is_update("0.2.0", "0.2.1"));
        assert!(is_update("0.2.0", "0.3.0"));
        assert!(is_update("0.2.9", "0.10.0"));
        assert!(is_update("0.9.9", "1.0.0"));
    }

    #[test]
    fn the_same_or_an_earlier_version_is_not() {
        assert!(!is_update("0.2.0", "0.2.0"));
        assert!(!is_update("0.10.0", "0.9.0"));
        assert!(!is_update("1.0.0", "0.99.99"));
        // Build metadata does not make a version newer.
        assert!(!is_update("0.2.0", "0.2.0+build.7"));
        assert!(!is_update("0.2.0+build.7", "0.2.0"));
    }

    #[test]
    fn pre_releases_are_offered_only_to_pre_releases() {
        assert!(!is_update("0.2.0", "0.3.0-beta.1"));
        assert!(is_update("0.3.0-beta.1", "0.3.0-beta.2"));
        assert!(is_update("0.3.0-beta.2", "0.3.0"));
        assert!(!is_update("0.3.0", "0.3.0-rc.1"));
    }

    #[test]
    fn documents_are_reopened_only_right_after_an_update() {
        let directory =
            std::env::temp_dir().join(format!("voicecut-reopen-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let file = directory.join("reopen.json");
        let talk = PathBuf::from("/projects/Talk.vc");
        write_reopen(&file, vec![talk.clone()]);
        assert_eq!(read_reopen(&file), vec![talk.clone()]);
        // Read once.
        assert!(read_reopen(&file).is_empty());
        let stale = Reopen {
            paths: vec![talk],
            written: now() - REOPEN_WITHIN.as_secs() - 60,
        };
        std::fs::write(&file, serde_json::to_vec(&stale).unwrap()).unwrap();
        assert!(read_reopen(&file).is_empty());
        let _ = std::fs::remove_dir_all(directory);
    }
}
