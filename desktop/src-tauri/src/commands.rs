//! Commands the app's pages and the editor call (`window.__TAURI__.core.invoke`).
//!
//! Dialogs are native sheets on the calling window.  A path chosen in a save
//! panel whose file exists was confirmed by the user ("Replace?"); the old
//! file goes to the Trash rather than being deleted.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Serialize;
use serde_json::Value;
use tauri::{AppHandle, Manager, State, WebviewWindow};
use tauri_plugin_dialog::DialogExt;

use crate::{
    display_name, document_window, remember_recent, AppState, Role, StatusView, MEDIA_EXTENSIONS,
};

#[tauri::command]
pub fn app_status(app: AppHandle) -> StatusView {
    crate::status(&app)
}

#[tauri::command]
pub fn start_setup(app: AppHandle) {
    crate::run_setup(app);
}

#[tauri::command]
pub fn cancel_setup(state: State<'_, AppState>) {
    if let Some(control) = state.control.lock().unwrap().clone() {
        control.cancel();
    }
}

/// Try again after a failure: finish the setup, or start the engine.
#[tauri::command]
pub fn restart_engine(app: AppHandle) {
    let state = app.state::<AppState>();
    let needs_setup = match &state.bundle {
        Ok(bundle) => !crate::runtime::pending_steps(&state.paths, bundle).is_empty(),
        Err(_) => false,
    };
    if needs_setup {
        crate::run_setup(app);
    } else {
        crate::restart_engine(app);
    }
}

#[tauri::command]
pub fn reveal_logs(state: State<'_, AppState>) {
    let logs = state.paths.logs();
    let _ = std::fs::create_dir_all(&logs);
    open_path(&logs);
}

#[tauri::command]
pub fn document_opened(
    app: AppHandle,
    window: WebviewWindow,
    project_id: String,
    path: String,
    name: String,
) {
    let path = PathBuf::from(&path);
    let path = crate::paths::canonical(&path).unwrap_or(path);
    app.state::<AppState>().roles.lock().unwrap().insert(
        window.label().to_string(),
        Role::Document {
            project_id,
            path: path.clone(),
        },
    );
    let _ = window.set_title(&name);
    remember_recent(&app, &path);
}

#[derive(Serialize)]
pub struct RecentDocument {
    path: String,
    name: String,
    exists: bool,
}

#[tauri::command]
pub fn recent_documents(state: State<'_, AppState>) -> Vec<RecentDocument> {
    state
        .recents
        .lock()
        .unwrap()
        .iter()
        .map(|path| RecentDocument {
            path: path.display().to_string(),
            name: path
                .file_stem()
                .map(|stem| stem.to_string_lossy().into_owned())
                .unwrap_or_default(),
            exists: path.exists(),
        })
        .collect()
}

#[tauri::command]
pub fn clear_recent_documents(app: AppHandle) {
    crate::clear_recents(&app);
}

/// Open a document (or, without `path`, one the user picks) in this window
/// if it is a welcome window, else in its own window.  False if cancelled.
#[tauri::command]
pub async fn open_document(
    app: AppHandle,
    window: WebviewWindow,
    path: Option<String>,
) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let path = match path {
            Some(path) => PathBuf::from(path),
            None => match pick_document(&app, Some(&window)) {
                Some(path) => path,
                None => return Ok(false),
            },
        };
        let state = app.state::<AppState>();
        let reuse =
            (state.role(window.label()) == Some(Role::Welcome)).then(|| window.label().to_string());
        crate::open_document(&app, &path, reuse)?;
        Ok(true)
    })
    .await
    .map_err(|error| error.to_string())?
}

#[derive(Serialize)]
pub struct NewProject {
    source: String,
    path: String,
}

/// Where a new project comes from and where its document goes.
#[tauri::command]
pub async fn choose_new_project(
    app: AppHandle,
    window: WebviewWindow,
    source: Option<String>,
) -> Result<Option<NewProject>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let source = match source {
            Some(source) => PathBuf::from(source),
            None => {
                let picked = match scripted_path() {
                    Some(answer) => answer,
                    None => app
                        .dialog()
                        .file()
                        .set_title("Choose a recording")
                        .add_filter("Audio or video recording", MEDIA_EXTENSIONS)
                        .set_parent(&window)
                        .blocking_pick_file()
                        .and_then(|file| file.into_path().ok()),
                };
                match picked {
                    Some(path) => path,
                    None => return Ok(None),
                }
            }
        };
        let stem = source
            .file_stem()
            .map(|stem| stem.to_string_lossy().into_owned())
            .unwrap_or_else(|| "Untitled".into());
        let folder = source.parent().map(Path::to_path_buf);
        let Some(path) = save_document_panel(
            &app,
            &window,
            "Save the new VoiceCut project",
            &stem,
            folder,
        )?
        else {
            return Ok(None);
        };
        Ok(Some(NewProject {
            source: source.display().to_string(),
            path: path.display().to_string(),
        }))
    })
    .await
    .map_err(|error| error.to_string())?
}

/// A place for a document (Save As).
#[tauri::command]
pub async fn choose_document_location(
    app: AppHandle,
    window: WebviewWindow,
    name: String,
    directory: Option<String>,
) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let folder = directory.map(PathBuf::from);
        Ok(
            save_document_panel(&app, &window, "Save As", &name, folder)?
                .map(|path| path.display().to_string()),
        )
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn choose_export_path(
    app: AppHandle,
    window: WebviewWindow,
    path: String,
    extensions: Vec<String>,
) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if let Some(answer) = scripted_path() {
            return Ok(answer.map(|path| path.display().to_string()));
        }
        let path = PathBuf::from(path);
        let extensions: Vec<&str> = extensions
            .iter()
            .map(|extension| extension.trim_start_matches('.'))
            .collect();
        let mut dialog = app
            .dialog()
            .file()
            .set_title("Export")
            .set_file_name(display_name(&path))
            .add_filter("Export", &extensions)
            .set_parent(&window);
        if let Some(folder) = path.parent().filter(|folder| folder.is_dir()) {
            dialog = dialog.set_directory(folder);
        }
        Ok(dialog
            .blocking_save_file()
            .and_then(|file| file.into_path().ok())
            .map(|path| path.display().to_string()))
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Locate a source recording that was moved.
#[tauri::command]
pub async fn choose_source(
    app: AppHandle,
    window: WebviewWindow,
    name: String,
) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if let Some(answer) = scripted_path() {
            return Ok(answer.map(|path| path.display().to_string()));
        }
        let extension = Path::new(&name)
            .extension()
            .map(|extension| extension.to_string_lossy().into_owned())
            .unwrap_or_default();
        let mut dialog = app
            .dialog()
            .file()
            .set_title(format!("Where is “{name}”?"))
            .set_parent(&window);
        if !extension.is_empty() {
            dialog = dialog.add_filter(&name, &[extension.as_str()]);
        }
        Ok(dialog
            .blocking_pick_file()
            .and_then(|file| file.into_path().ok())
            .map(|path| path.display().to_string()))
    })
    .await
    .map_err(|error| error.to_string())?
}

/// `window.confirm` for the editor: WKWebView has no dialogs of its own.
#[tauri::command]
pub async fn confirm_dialog(
    app: AppHandle,
    window: WebviewWindow,
    message: String,
    title: Option<String>,
    ok: Option<String>,
) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || {
        crate::ask(
            &app,
            Some(&window),
            title.as_deref().unwrap_or("VoiceCut"),
            &message,
            ok.as_deref().unwrap_or("OK"),
        )
    })
    .await
    .map_err(|error| error.to_string())
}

#[tauri::command]
pub fn open_external(url: String) -> Result<(), String> {
    open_in_browser(&url)
}

#[tauri::command]
pub fn reveal_path(path: String) {
    reveal(Path::new(&path));
}

#[tauri::command]
pub fn flush_done(state: State<'_, AppState>, token: u64) {
    let (pending, signal) = &state.flushes;
    pending.lock().unwrap().remove(&token);
    signal.notify_all();
}

#[tauri::command]
pub fn take_pending_action(state: State<'_, AppState>, window: WebviewWindow) -> Option<Value> {
    state.pending_actions.lock().unwrap().remove(window.label())
}

// -- helpers -------------------------------------------------------------------------

/// A test build's scripted answer to the next file panel: `Some(None)` is
/// Cancel.
#[cfg(feature = "automation")]
fn scripted_path() -> Option<Option<PathBuf>> {
    crate::automation::next_answer().map(|answer| answer.as_str().map(PathBuf::from))
}

#[cfg(not(feature = "automation"))]
fn scripted_path() -> Option<Option<PathBuf>> {
    None
}

pub fn pick_document(app: &AppHandle, parent: Option<&WebviewWindow>) -> Option<PathBuf> {
    if let Some(answer) = scripted_path() {
        return answer;
    }
    let mut dialog = app
        .dialog()
        .file()
        .set_title("Open a VoiceCut project")
        // A document is a folder; elsewhere than macOS one opens its launcher.
        .add_filter(
            "VoiceCut Project",
            &[if cfg!(target_os = "macos") {
                "vc"
            } else {
                "vcproj"
            }],
        );
    if let Some(parent) = parent {
        dialog = dialog.set_parent(parent);
    }
    dialog
        .blocking_pick_file()
        .and_then(|file| file.into_path().ok())
}

/// A save panel for a `.vc` document; an existing one the user agreed to
/// replace goes to the Trash.
fn save_document_panel(
    app: &AppHandle,
    window: &WebviewWindow,
    title: &str,
    name: &str,
    folder: Option<PathBuf>,
) -> Result<Option<PathBuf>, String> {
    let chosen = match scripted_path() {
        Some(answer) => answer,
        None => {
            let mut dialog = app
                .dialog()
                .file()
                .set_title(title)
                .set_file_name(format!("{name}.vc"))
                .add_filter("VoiceCut Project", &["vc"])
                .set_parent(window);
            if let Some(folder) = folder.filter(|folder| folder.is_dir()) {
                dialog = dialog.set_directory(folder);
            }
            dialog
                .blocking_save_file()
                .and_then(|file| file.into_path().ok())
        }
    };
    let Some(mut path) = chosen else {
        return Ok(None);
    };
    if !path
        .extension()
        .is_some_and(|extension| extension.eq_ignore_ascii_case("vc"))
    {
        path.set_extension("vc");
    }
    if path.exists() {
        let canonical = crate::paths::canonical(&path).unwrap_or_else(|_| path.clone());
        if document_window(app, &canonical).is_some() {
            return Err(format!(
                "“{}” is open in VoiceCut. Close it first, or choose another name.",
                display_name(&path)
            ));
        }
        move_to_trash(&path)?;
    }
    Ok(Some(path))
}

fn move_to_trash(path: &Path) -> Result<(), String> {
    #[allow(unused_mut)]
    let mut context = trash::TrashContext::default();
    #[cfg(target_os = "macos")]
    {
        use trash::macos::{DeleteMethod, TrashContextExtMacos};
        context.set_delete_method(DeleteMethod::NsFileManager);
    }
    context
        .delete(path)
        .map_err(|error| format!("Cannot move “{}” to the Trash: {error}", display_name(path)))
}

pub fn open_in_browser(url: &str) -> Result<(), String> {
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err("only web links open in the browser".into());
    }
    let result = if cfg!(target_os = "macos") {
        Command::new("open").arg(url).spawn()
    } else if cfg!(windows) {
        crate::process::quiet(Command::new("cmd").args(["/C", "start", "", url])).spawn()
    } else {
        crate::process::quiet(Command::new("xdg-open").arg(url)).spawn()
    };
    result.map(|_| ()).map_err(|error| error.to_string())
}

fn open_path(path: &Path) {
    let program = if cfg!(target_os = "macos") {
        "open"
    } else if cfg!(windows) {
        "explorer"
    } else {
        "xdg-open"
    };
    let _ = crate::process::quiet(Command::new(program).arg(path)).spawn();
}

pub fn reveal(path: &Path) {
    if cfg!(target_os = "macos") {
        let _ = Command::new("open").arg("-R").arg(path).spawn();
    } else if cfg!(windows) {
        let mut select = std::ffi::OsString::from("/select,");
        select.push(path);
        let _ = Command::new("explorer").arg(select).spawn();
    } else if let Some(parent) = path.parent() {
        open_path(parent);
    }
}

/// The user's appearance: "system", "light" or "dark".
#[tauri::command]
pub fn appearance(state: State<'_, AppState>) -> String {
    state.preferences.lock().unwrap().appearance.clone()
}

#[tauri::command]
pub fn set_appearance(app: AppHandle, appearance: String) -> Result<(), String> {
    crate::set_appearance(&app, &appearance)
}

/// An editor preference the app keeps across launches (playback speed).
#[tauri::command]
pub fn preference(state: State<'_, AppState>, key: String) -> Option<String> {
    state.preferences.lock().unwrap().editor.get(&key).cloned()
}

#[tauri::command]
pub fn set_preference(
    state: State<'_, AppState>,
    key: String,
    value: Option<String>,
) -> Result<(), String> {
    let mut preferences = state.preferences.lock().unwrap();
    if preferences.editor.get(&key) == value.as_ref() {
        return Ok(());
    }
    preferences.set_editor(&key, value)?;
    preferences.save(&state.paths.preferences())
}

/// The app's own update: the version on offer, if any, and how far it got.
#[tauri::command]
pub fn update_status(app: AppHandle) -> crate::updates::UpdateView {
    crate::updates::view(&app)
}

/// Download the update, save the documents, install it and restart.
#[tauri::command]
pub fn install_update(app: AppHandle) {
    crate::updates::install_now(app);
}

#[tauri::command]
pub fn set_automatic_updates(app: AppHandle, on: bool) -> Result<(), String> {
    crate::updates::set_automatic(&app, on)
}
