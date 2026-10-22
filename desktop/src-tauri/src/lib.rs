//! VoiceCut for the desktop: the VoiceCut editor in native windows.
//!
//! The app installs VoiceCut's engine on first launch (`setup`), starts it
//! (`engine`) and shows the editor it serves in its windows: one window per
//! `.vc` document, plus a welcome window when nothing is open.  Menus,
//! dialogs, the Dock, the Finder and the window lifecycle are native; the
//! editor's pages call the app through Tauri commands (`commands`).

#[cfg(feature = "automation")]
mod automation;
mod commands;
mod engine;
#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
mod paths;
mod process;
mod runtime;
mod setup;

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};
use tauri::menu::{
    AboutMetadata, Menu, MenuItemBuilder, PredefinedMenuItem, Submenu, SubmenuBuilder,
};
use tauri::{
    AppHandle, DragDropEvent, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent, Wry,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use engine::Engine;
use paths::Paths;
use runtime::Bundle;

const RECENT_LIMIT: usize = 12;
/// Files macOS asks the app to open while it is still launching, before its
/// state exists (a double-clicked document starts the app this way).
static EARLY_PATHS: Mutex<Vec<PathBuf>> = Mutex::new(Vec::new());
const FLUSH_TIMEOUT: Duration = Duration::from_secs(3);
const REVEAL_LABEL: &str = if cfg!(target_os = "macos") {
    "Show in Finder"
} else if cfg!(windows) {
    "Show in File Explorer"
} else {
    "Show in Folder"
};
const HELP_URL: &str = "https://github.com/vectozavr/voicecut/blob/main/docs/editor.md";
pub const MEDIA_EXTENSIONS: &[&str] = &[
    "aac", "aif", "aiff", "flac", "m4a", "mp3", "oga", "ogg", "opus", "wav", "mkv", "mov", "mp4",
    "webm",
];
/// Commands the editor's pages may call (see `capabilities/app.json` for the
/// app's own pages).
const EDITOR_PERMISSIONS: &[&str] = &[
    "core:default",
    "allow-document-opened",
    "allow-recent-documents",
    "allow-clear-recent-documents",
    "allow-open-document",
    "allow-choose-new-project",
    "allow-choose-document-location",
    "allow-choose-export-path",
    "allow-choose-source",
    "allow-confirm-dialog",
    "allow-open-external",
    "allow-reveal-path",
    "allow-flush-done",
    "allow-take-pending-action",
];

#[derive(Clone, Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
pub enum Phase {
    Checking,
    NeedsSetup,
    Installing,
    Starting,
    Ready { url: String },
    Failed { message: String, during: String },
}

/// What a window shows.
#[derive(Clone, PartialEq)]
pub enum Role {
    /// The app's own page: setup, starting, errors.
    App,
    Welcome,
    Document {
        project_id: String,
        path: PathBuf,
    },
}

pub struct AppState {
    pub paths: Paths,
    pub bundle: Result<Bundle, String>,
    pub phase: Mutex<Phase>,
    pub setup: Mutex<Option<setup::SetupView>>,
    pub control: Mutex<Option<Arc<setup::Control>>>,
    pub engine: Mutex<Option<Arc<Engine>>>,
    pub roles: Mutex<HashMap<String, Role>>,
    pub pending_paths: Mutex<Vec<PathBuf>>,
    pub pending_actions: Mutex<HashMap<String, Value>>,
    pub recents: Mutex<Vec<PathBuf>>,
    pub flushes: (Mutex<HashSet<u64>>, Condvar),
    pub counter: AtomicU64,
    pub quitting: AtomicBool,
    pub recent_menu: Mutex<Option<Submenu<Wry>>>,
}

impl AppState {
    pub fn engine(&self) -> Option<Arc<Engine>> {
        self.engine.lock().unwrap().clone()
    }

    pub fn role(&self, label: &str) -> Option<Role> {
        self.roles.lock().unwrap().get(label).cloned()
    }

    fn next(&self) -> u64 {
        self.counter.fetch_add(1, Ordering::SeqCst)
    }
}

#[derive(Serialize, Clone)]
pub struct StatusView {
    #[serde(flatten)]
    phase: Phase,
    setup: Option<setup::SetupView>,
    version: &'static str,
    data_dir: String,
}

pub fn status(app: &AppHandle) -> StatusView {
    let state = app.state::<AppState>();
    let phase = state.phase.lock().unwrap().clone();
    let setup = state.setup.lock().unwrap().clone();
    StatusView {
        phase,
        setup,
        version: env!("CARGO_PKG_VERSION"),
        data_dir: state.paths.data.display().to_string(),
    }
}

fn set_phase(app: &AppHandle, phase: Phase) {
    *app.state::<AppState>().phase.lock().unwrap() = phase;
    let _ = app.emit("status", status(app));
}

// -- setup and the engine ----------------------------------------------------------

/// Decide what the first window shows: setup, an update, or the editor.
fn boot(app: AppHandle) {
    let state = app.state::<AppState>();
    let bundle = match &state.bundle {
        Ok(bundle) => bundle.clone(),
        Err(message) => {
            set_phase(
                &app,
                Phase::Failed {
                    message: message.clone(),
                    during: "setup".into(),
                },
            );
            return;
        }
    };
    let pending = runtime::pending_steps(&state.paths, &bundle);
    if pending.is_empty() || state.paths.development {
        start_engine(app);
    } else if state.paths.setup_state().is_file() {
        // An update, or a setup that stopped half-way: carry on by itself.
        run_setup(app);
    } else {
        *state.setup.lock().unwrap() = Some(setup::SetupView::new(&state.paths, &bundle, &pending));
        set_phase(&app, Phase::NeedsSetup);
    }
}

pub fn run_setup(app: AppHandle) {
    let state = app.state::<AppState>();
    let Ok(bundle) = state.bundle.clone() else {
        return;
    };
    {
        let mut control = state.control.lock().unwrap();
        if control.is_some() {
            return; // already running
        }
        *control = Some(Arc::new(setup::Control::default()));
    }
    let steps = runtime::pending_steps(&state.paths, &bundle);
    *state.setup.lock().unwrap() = Some(setup::SetupView::new(&state.paths, &bundle, &steps));
    set_phase(&app, Phase::Installing);
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let control = state.control.lock().unwrap().clone().unwrap();
        let publisher = app.clone();
        let publish = move |view: &setup::SetupView| {
            *publisher.state::<AppState>().setup.lock().unwrap() = Some(view.clone());
            let _ = publisher.emit("status", status(&publisher));
        };
        let result = setup::run(&state.paths, &bundle, &steps, &control, &publish);
        *state.control.lock().unwrap() = None;
        match result {
            Ok(()) => start_engine(app.clone()),
            Err(message) => set_phase(
                &app,
                Phase::Failed {
                    message,
                    during: "setup".into(),
                },
            ),
        }
    });
}

pub fn start_engine(app: AppHandle) {
    set_phase(&app, Phase::Starting);
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let watcher = app.clone();
        let result = Engine::start(&state.paths, move |message| {
            engine_stopped(watcher, message)
        });
        let engine = match result {
            Ok(engine) => Arc::new(engine),
            Err(message) => {
                set_phase(
                    &app,
                    Phase::Failed {
                        message,
                        during: "engine".into(),
                    },
                );
                return;
            }
        };
        let base = engine.base_url();
        let mut capability = tauri::ipc::CapabilityBuilder::new("editor")
            .remote(format!("{base}/*"))
            .window("*");
        for permission in EDITOR_PERMISSIONS {
            capability = capability.permission(*permission);
        }
        if let Err(error) = app.add_capability(capability) {
            eprintln!("cannot allow the editor to call the app: {error}");
        }
        #[cfg(feature = "automation")]
        automation::allow_remote(&app, &base);
        *state.engine.lock().unwrap() = Some(engine);
        set_phase(&app, Phase::Ready { url: base.clone() });
        let pending: Vec<PathBuf> = std::mem::take(&mut *state.pending_paths.lock().unwrap());
        let app_windows: Vec<String> = state
            .roles
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, role)| **role == Role::App)
            .map(|(label, _)| label.clone())
            .collect();
        let documents: Vec<PathBuf> = pending
            .iter()
            .filter(|path| is_document(path))
            .cloned()
            .collect();
        // The first document takes over the app's own window; otherwise it
        // becomes the welcome window.
        let mut reuse = app_windows.first().cloned();
        for path in documents {
            let label = reuse.take();
            let app = app.clone();
            if let Err(message) = open_document(&app, &path, label.clone()) {
                error_dialog(
                    &app,
                    &format!("VoiceCut can’t open “{}”", display_name(&path)),
                    &message,
                );
                if label.is_some() {
                    reuse = label;
                }
            }
        }
        for label in app_windows
            .into_iter()
            .filter(|label| Some(label) != reuse.as_ref())
        {
            if let Some(window) = app.get_webview_window(&label) {
                if state.role(&label) == Some(Role::App) {
                    let _ = window.destroy();
                }
            }
        }
        if let Some(label) = reuse {
            show_welcome_in(&app, &label);
        }
        for path in pending.iter().filter(|path| !is_document(path)) {
            new_project(&app, Some(path.clone()));
        }
    });
}

fn engine_stopped(app: AppHandle, message: String) {
    let state = app.state::<AppState>();
    if state.quitting.load(Ordering::SeqCst) {
        return;
    }
    *state.engine.lock().unwrap() = None;
    // Documents reopen once the engine runs again.
    let open: Vec<PathBuf> = state
        .roles
        .lock()
        .unwrap()
        .values()
        .filter_map(|role| match role {
            Role::Document { path, .. } => Some(path.clone()),
            _ => None,
        })
        .collect();
    state.pending_paths.lock().unwrap().extend(open);
    let labels: Vec<String> = state.roles.lock().unwrap().keys().cloned().collect();
    for label in labels.iter().skip(1) {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.destroy();
        }
        state.roles.lock().unwrap().remove(label);
    }
    set_phase(
        &app,
        Phase::Failed {
            message,
            during: "engine".into(),
        },
    );
    match labels.first() {
        Some(label) => {
            state.roles.lock().unwrap().insert(label.clone(), Role::App);
            if let Some(window) = app.get_webview_window(label) {
                let _ = window.set_title("VoiceCut");
                if let Ok(url) = app_page_url() {
                    let _ = window.navigate(url);
                }
            }
        }
        None => {
            let _ = create_window(
                &app,
                WebviewUrl::App("index.html".into()),
                "VoiceCut",
                Role::App,
            );
        }
    }
}

pub fn restart_engine(app: AppHandle) {
    if app.state::<AppState>().engine().is_some() {
        return;
    }
    start_engine(app);
}

// -- windows ---------------------------------------------------------------------------

fn app_page_url() -> Result<tauri::Url, String> {
    let url = if cfg!(windows) {
        "http://tauri.localhost/index.html"
    } else {
        "tauri://localhost/index.html"
    };
    tauri::Url::parse(url).map_err(|error| error.to_string())
}

/// Whether a window may navigate to `url`; anything else opens in the
/// browser.
fn navigation_allowed(app: &AppHandle, url: &tauri::Url) -> bool {
    match url.scheme() {
        "tauri" | "about" | "data" | "blob" => return true,
        _ => {}
    }
    if url.host_str() == Some("tauri.localhost") {
        return true;
    }
    let engine_port = app.state::<AppState>().engine().map(|engine| engine.port);
    if url.host_str() == Some("127.0.0.1") && url.port().is_some() && url.port() == engine_port {
        return true;
    }
    if matches!(url.scheme(), "http" | "https") {
        let _ = commands::open_in_browser(url.as_str());
    }
    false
}

pub fn create_window(
    app: &AppHandle,
    url: WebviewUrl,
    title: &str,
    role: Role,
) -> Result<WebviewWindow, String> {
    let state = app.state::<AppState>();
    let label = format!("window-{}", state.next());
    let navigator = app.clone();
    let window = WebviewWindowBuilder::new(app, &label, url)
        .title(title)
        .inner_size(1440.0, 900.0)
        .min_inner_size(980.0, 640.0)
        .resizable(true)
        .focused(true)
        .on_navigation(move |url| navigation_allowed(&navigator, url))
        .build()
        .map_err(|error| error.to_string())?;
    #[cfg(target_os = "macos")]
    macos::allow_full_screen(&window);
    state.roles.lock().unwrap().insert(label, role);
    Ok(window)
}

fn engine_url(app: &AppHandle, query: &str) -> Result<tauri::Url, String> {
    let engine = app
        .state::<AppState>()
        .engine()
        .ok_or("VoiceCut is still starting")?;
    tauri::Url::parse(&format!("{}/{query}", engine.base_url())).map_err(|error| error.to_string())
}

/// Turn the window `label` into a welcome window.
fn show_welcome_in(app: &AppHandle, label: &str) {
    let Some(window) = app.get_webview_window(label) else {
        return;
    };
    if let Ok(url) = engine_url(app, "") {
        app.state::<AppState>()
            .roles
            .lock()
            .unwrap()
            .insert(label.to_string(), Role::Welcome);
        let _ = window.set_title("VoiceCut");
        let _ = window.navigate(url);
        let _ = window.set_focus();
    }
}

/// Bring up a welcome window (or, before the engine runs, the app's page).
pub fn show_welcome(app: &AppHandle) -> Option<WebviewWindow> {
    let state = app.state::<AppState>();
    let existing = state
        .roles
        .lock()
        .unwrap()
        .iter()
        .find(|(_, role)| matches!(role, Role::Welcome | Role::App))
        .map(|(label, _)| label.clone());
    if let Some(window) = existing.and_then(|label| app.get_webview_window(&label)) {
        let _ = window.unminimize();
        let _ = window.set_focus();
        return Some(window);
    }
    match engine_url(app, "") {
        Ok(url) => create_window(app, WebviewUrl::External(url), "VoiceCut", Role::Welcome).ok(),
        Err(_) => create_window(
            app,
            WebviewUrl::App("index.html".into()),
            "VoiceCut",
            Role::App,
        )
        .ok(),
    }
}

pub fn focused_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.webview_windows()
        .into_values()
        .find(|window| window.is_focused().unwrap_or(false))
}

pub fn send_command(app: &AppHandle, label: &str, command: Value) {
    let _ = app.emit_to(label, "menu", command);
}

// -- documents -------------------------------------------------------------------------

/// The document a launcher file (`<name>.vcproj`, what Linux and Windows
/// users double-click) stands for; any other path unchanged.
pub fn document_path(path: PathBuf) -> PathBuf {
    if path.is_file()
        && path
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("vcproj"))
    {
        if let Some(parent) = path.parent() {
            return parent.to_path_buf();
        }
    }
    path
}

pub fn is_document(path: &Path) -> bool {
    path.is_dir()
        && (path
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("vc"))
            || path.join("project.json").is_file())
}

pub fn is_media(path: &Path) -> bool {
    path.is_file()
        && path.extension().is_some_and(|extension| {
            let extension = extension.to_string_lossy().to_lowercase();
            MEDIA_EXTENSIONS.contains(&extension.as_str())
        })
}

pub fn display_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.display().to_string())
}

fn document_window(app: &AppHandle, path: &Path) -> Option<String> {
    app.state::<AppState>()
        .roles
        .lock()
        .unwrap()
        .iter()
        .find(|(_, role)| matches!(role, Role::Document { path: open, .. } if open == path))
        .map(|(label, _)| label.clone())
}

/// Open a `.vc` document: focus its window, or show it in `reuse` (a welcome
/// window) or a new window.  Blocks on the engine; call it off the main thread.
pub fn open_document(app: &AppHandle, path: &Path, reuse: Option<String>) -> Result<(), String> {
    let state = app.state::<AppState>();
    let path = paths::canonical(path).map_err(|error| error.to_string())?;
    if let Some(window) =
        document_window(app, &path).and_then(|label| app.get_webview_window(&label))
    {
        let _ = window.unminimize();
        let _ = window.set_focus();
        return Ok(());
    }
    let Some(engine) = state.engine() else {
        state.pending_paths.lock().unwrap().push(path);
        return Ok(());
    };
    let project = engine.post(
        "/api/documents/open",
        json!({ "path": path }),
        Duration::from_secs(600),
    )?;
    let project_id = project["id"]
        .as_str()
        .ok_or("the engine sent no project id")?
        .to_string();
    let name = project["document"]["name"]
        .as_str()
        .map(str::to_string)
        .unwrap_or_else(|| display_name(&path));
    let url = engine_url(app, &format!("?project={}", urlencode(&project_id)))?;
    let role = Role::Document {
        project_id,
        path: path.clone(),
    };
    let target = reuse.or_else(|| {
        // A welcome window, preferably the focused one, shows the document.
        let focused = focused_window(app).map(|window| window.label().to_string());
        let roles = state.roles.lock().unwrap();
        focused
            .filter(|label| roles.get(label) == Some(&Role::Welcome))
            .or_else(|| {
                roles
                    .iter()
                    .find(|(_, role)| **role == Role::Welcome)
                    .map(|(label, _)| label.clone())
            })
    });
    match target.and_then(|label| app.get_webview_window(&label)) {
        Some(window) => {
            state
                .roles
                .lock()
                .unwrap()
                .insert(window.label().to_string(), role);
            let _ = window.set_title(&name);
            window.navigate(url).map_err(|error| error.to_string())?;
            let _ = window.set_focus();
        }
        None => {
            create_window(app, WebviewUrl::External(url), &name, role)?;
        }
    }
    remember_recent(app, &path);
    Ok(())
}

/// Start a new project, from `source` or a recording the user picks, in a
/// welcome window (it shows the progress of reading the recording).
pub fn new_project(app: &AppHandle, source: Option<PathBuf>) {
    let state = app.state::<AppState>();
    let action = json!({ "command": "new-project", "source": source });
    if state.engine().is_none() {
        if let Some(source) = source {
            state.pending_paths.lock().unwrap().push(source);
        }
        show_welcome(app);
        return;
    }
    let welcome = {
        let focused = focused_window(app).map(|window| window.label().to_string());
        let roles = state.roles.lock().unwrap();
        focused
            .filter(|label| roles.get(label) == Some(&Role::Welcome))
            .or_else(|| {
                roles
                    .iter()
                    .find(|(_, role)| **role == Role::Welcome)
                    .map(|(label, _)| label.clone())
            })
    };
    match welcome.and_then(|label| app.get_webview_window(&label)) {
        Some(window) => {
            let _ = window.set_focus();
            send_command(app, window.label(), action);
        }
        None => {
            if let Ok(url) = engine_url(app, "") {
                if let Ok(window) =
                    create_window(app, WebviewUrl::External(url), "VoiceCut", Role::Welcome)
                {
                    state
                        .pending_actions
                        .lock()
                        .unwrap()
                        .insert(window.label().to_string(), action);
                }
            }
        }
    }
}

/// Files from the Finder, the Dock or a drop: documents open, recordings
/// start new projects.
pub fn open_paths(app: &AppHandle, paths: Vec<PathBuf>) {
    let paths: Vec<PathBuf> = paths.into_iter().map(document_path).collect();
    let app = app.clone();
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        if state.engine().is_none() {
            state.pending_paths.lock().unwrap().extend(paths);
            if app.webview_windows().is_empty() {
                show_welcome(&app);
            }
            return;
        }
        for path in paths {
            if is_document(&path) {
                if let Err(message) = open_document(&app, &path, None) {
                    error_dialog(
                        &app,
                        &format!("VoiceCut can’t open “{}”", display_name(&path)),
                        &message,
                    );
                }
            } else if is_media(&path) {
                new_project(&app, Some(path));
            } else {
                error_dialog(
                    &app,
                    &format!("VoiceCut can’t open “{}”", display_name(&path)),
                    "Open a VoiceCut project (.vc), or an audio or video recording to start a new one.",
                );
            }
        }
    });
}

fn load_recents(paths: &Paths) -> Vec<PathBuf> {
    std::fs::read_to_string(paths.recent())
        .ok()
        .and_then(|text| serde_json::from_str::<Vec<PathBuf>>(&text).ok())
        .unwrap_or_default()
}

pub fn remember_recent(app: &AppHandle, path: &Path) {
    let state = app.state::<AppState>();
    {
        let mut recents = state.recents.lock().unwrap();
        recents.retain(|item| item != path);
        recents.insert(0, path.to_path_buf());
        recents.truncate(RECENT_LIMIT);
        let _ = std::fs::create_dir_all(&state.paths.data);
        let _ = std::fs::write(
            state.paths.recent(),
            serde_json::to_vec_pretty(&*recents).unwrap_or_default(),
        );
    }
    refresh_recent_menu(app);
}

pub fn clear_recents(app: &AppHandle) {
    let state = app.state::<AppState>();
    state.recents.lock().unwrap().clear();
    let _ = std::fs::remove_file(state.paths.recent());
    refresh_recent_menu(app);
}

fn refresh_recent_menu(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let state = handle.state::<AppState>();
        let Some(menu) = state.recent_menu.lock().unwrap().clone() else {
            return;
        };
        while let Ok(Some(item)) = menu.remove_at(0) {
            drop(item);
        }
        let recents = state.recents.lock().unwrap().clone();
        for (index, path) in recents.iter().enumerate() {
            let name = path
                .file_stem()
                .map(|stem| stem.to_string_lossy().into_owned())
                .unwrap_or_default();
            if let Ok(item) = MenuItemBuilder::with_id(format!("recent:{index}"), name)
                .enabled(path.exists())
                .build(&handle)
            {
                let _ = menu.append(&item);
            }
        }
        if !recents.is_empty() {
            if let Ok(separator) = PredefinedMenuItem::separator(&handle) {
                let _ = menu.append(&separator);
            }
        }
        if let Ok(item) = MenuItemBuilder::with_id("recent-clear", "Clear Menu")
            .enabled(!recents.is_empty())
            .build(&handle)
        {
            let _ = menu.append(&item);
        }
    });
}

// -- dialogs ----------------------------------------------------------------------------

pub fn error_dialog(app: &AppHandle, title: &str, message: &str) {
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Error)
        .blocking_show();
}

pub fn ask(
    app: &AppHandle,
    window: Option<&WebviewWindow>,
    title: &str,
    message: &str,
    ok: &str,
) -> bool {
    #[cfg(feature = "automation")]
    if let Some(answer) = automation::next_answer() {
        return answer.as_bool().unwrap_or(false);
    }
    let mut dialog = app
        .dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            ok.to_string(),
            "Cancel".to_string(),
        ));
    if let Some(window) = window {
        dialog = dialog.parent(window);
    }
    dialog.blocking_show()
}

// -- closing and quitting ----------------------------------------------------------------

/// Ask the page in `label` to save its edit now; wait until it has.
pub fn flush(app: &AppHandle, label: &str) {
    let state = app.state::<AppState>();
    let token = state.next();
    let (pending, signal) = &state.flushes;
    pending.lock().unwrap().insert(token);
    send_command(app, label, json!({ "command": "flush", "token": token }));
    let guard = pending.lock().unwrap();
    let _ = signal.wait_timeout_while(guard, FLUSH_TIMEOUT, |pending| pending.contains(&token));
    pending.lock().unwrap().remove(&token);
}

/// Voice Cut or an export running in a project.
fn busy(engine: &Engine, project_id: &str) -> bool {
    engine
        .get(&format!("/api/projects/{}", urlencode(project_id)))
        .map(|project| {
            let jobs = &project["jobs"];
            jobs["voicecut"].is_string() || jobs["export"].is_string()
        })
        .unwrap_or(false)
}

fn close_document_window(app: AppHandle, label: String, project_id: String, path: PathBuf) {
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let window = app.get_webview_window(&label);
        if let Some(engine) = state.engine() {
            flush(&app, &label);
            let running = busy(&engine, &project_id);
            if running
                && !ask(
                    &app,
                    window.as_ref(),
                    &format!("Voice Cut is still working on “{}”", display_name(&path)),
                    "Closing the window stops it. The project keeps everything finished so far.",
                    "Stop and Close",
                )
            {
                return;
            }
            let _ = engine.post(
                &format!("/api/projects/{}/close?cancel=true", urlencode(&project_id)),
                json!({}),
                Duration::from_secs(30),
            );
        }
        state.roles.lock().unwrap().remove(&label);
        if let Some(window) = window {
            let _ = window.destroy();
        }
    });
}

pub fn request_quit(app: &AppHandle) {
    let state = app.state::<AppState>();
    if state.quitting.load(Ordering::SeqCst) {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let documents: Vec<(String, String, PathBuf)> = state
            .roles
            .lock()
            .unwrap()
            .iter()
            .filter_map(|(label, role)| match role {
                Role::Document { project_id, path } => {
                    Some((label.clone(), project_id.clone(), path.clone()))
                }
                _ => None,
            })
            .collect();
        if let Some(engine) = state.engine() {
            for (label, _, _) in &documents {
                flush(&app, label);
            }
            let working: Vec<String> = documents
                .iter()
                .filter(|(_, project_id, _)| busy(&engine, project_id))
                .map(|(_, _, path)| display_name(path))
                .collect();
            if !working.is_empty()
                && !ask(
                    &app,
                    None,
                    "Quit VoiceCut?",
                    &format!(
                        "Voice Cut is still working on {}. Quitting stops it; each project keeps everything finished so far.",
                        working.iter().map(|name| format!("“{name}”")).collect::<Vec<_>>().join(", ")
                    ),
                    "Quit",
                )
            {
                return;
            }
        }
        if let Some(control) = state.control.lock().unwrap().clone() {
            if !ask(
                &app,
                None,
                "Quit VoiceCut?",
                "VoiceCut is still being installed. The setup continues where it stopped the next time you open VoiceCut.",
                "Quit",
            ) {
                return;
            }
            control.cancel();
        }
        state.quitting.store(true, Ordering::SeqCst);
        stop_engine(&app);
        app.exit(0);
    });
}

fn stop_engine(app: &AppHandle) {
    let engine = app.state::<AppState>().engine.lock().unwrap().take();
    if let Some(engine) = engine {
        engine.stop();
    }
}

// -- menus -----------------------------------------------------------------------------

#[cfg(target_os = "macos")]
fn build_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let item = |id: &str, text: &str, accelerator: Option<&str>| {
        let builder = MenuItemBuilder::with_id(id, text);
        match accelerator {
            Some(accelerator) => builder.accelerator(accelerator),
            None => builder,
        }
        .build(app)
    };
    let about = AboutMetadata {
        name: Some("VoiceCut".into()),
        version: Some(env!("CARGO_PKG_VERSION").into()),
        copyright: Some("© 2026 VoiceCut contributors · MIT License".into()),
        website: Some("https://github.com/vectozavr/voicecut".into()),
        website_label: Some("github.com/vectozavr/voicecut".into()),
        ..Default::default()
    };
    let app_menu = SubmenuBuilder::new(app, "VoiceCut")
        .about(Some(about))
        .separator()
        .item(&item("settings", "Settings…", Some("CmdOrCtrl+,"))?)
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .item(&item("quit", "Quit VoiceCut", Some("CmdOrCtrl+Q"))?)
        .build()?;
    let recent = SubmenuBuilder::with_id(app, "recent", "Open Recent").build()?;
    let file = SubmenuBuilder::new(app, "File")
        .item(&item("new", "New Project…", Some("CmdOrCtrl+N"))?)
        .item(&item("open", "Open…", Some("CmdOrCtrl+O"))?)
        .item(&recent)
        .separator()
        .close_window()
        .item(&item("save", "Save", Some("CmdOrCtrl+S"))?)
        .item(&item("save-as", "Save As…", Some("CmdOrCtrl+Shift+S"))?)
        .separator()
        .item(&item("export", "Export…", Some("CmdOrCtrl+E"))?)
        .item(&item("reveal", REVEAL_LABEL, None)?)
        .build()?;
    // Undo and Select All act on the timeline, or on the text field in focus.
    let edit = SubmenuBuilder::new(app, "Edit")
        .item(&item("undo", "Undo", Some("CmdOrCtrl+Z"))?)
        .item(&item("redo", "Redo", Some("CmdOrCtrl+Shift+Z"))?)
        .separator()
        .cut()
        .copy()
        .paste()
        .item(&item("select-all", "Select All", Some("CmdOrCtrl+A"))?)
        .build()?;
    let view = SubmenuBuilder::new(app, "View")
        .item(&item("zoom-in", "Zoom In", Some("CmdOrCtrl+="))?)
        .item(&item("zoom-out", "Zoom Out", Some("CmdOrCtrl+-"))?)
        .item(&item("zoom-fit", "Show the Whole Edit", None)?)
        .separator()
        .fullscreen()
        .build()?;
    let window = SubmenuBuilder::new(app, "Window")
        .minimize()
        .maximize()
        .separator()
        .item(&PredefinedMenuItem::bring_all_to_front(app, None)?)
        .build()?;
    let help = SubmenuBuilder::new(app, "Help")
        .item(&item("shortcuts", "Keyboard Shortcuts", None)?)
        .item(&item("help", "VoiceCut Help", None)?)
        .build()?;
    let menu = Menu::with_items(app, &[&app_menu, &file, &edit, &view, &window, &help])?;
    #[cfg(target_os = "macos")]
    {
        window.set_as_windows_menu_for_nsapp()?;
        help.set_as_help_menu_for_nsapp()?;
    }
    *app.state::<AppState>().recent_menu.lock().unwrap() = Some(recent);
    Ok(menu)
}

/// Windows and Linux: a menu bar in each window, laid out as there.  The
/// editor handles Ctrl+Z, Ctrl+A and the clipboard itself, so the Edit menu
/// shows those commands without taking their keys.
#[cfg(not(target_os = "macos"))]
fn build_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let item = |id: &str, text: &str, accelerator: Option<&str>| {
        let builder = MenuItemBuilder::with_id(id, text);
        match accelerator {
            Some(accelerator) => builder.accelerator(accelerator),
            None => builder,
        }
        .build(app)
    };
    let recent = SubmenuBuilder::with_id(app, "recent", "Open &Recent").build()?;
    let file = SubmenuBuilder::new(app, "&File")
        .item(&item("new", "&New Project…", Some("Ctrl+N"))?)
        .item(&item("open", "&Open…", Some("Ctrl+O"))?)
        .item(&recent)
        .separator()
        .item(&item("save", "&Save", Some("Ctrl+S"))?)
        .item(&item("save-as", "Save &As…", Some("Ctrl+Shift+S"))?)
        .separator()
        .item(&item("export", "&Export…", Some("Ctrl+E"))?)
        .item(&item("reveal", REVEAL_LABEL, None)?)
        .separator()
        .item(&item("settings", "Se&ttings…", Some("Ctrl+,"))?)
        .separator()
        .item(&item("close-window", "&Close Window", Some("Ctrl+W"))?)
        .item(&item("quit", "&Quit VoiceCut", Some("Ctrl+Q"))?)
        .build()?;
    let edit = SubmenuBuilder::new(app, "&Edit")
        .item(&item("undo", "&Undo", None)?)
        .item(&item("redo", "&Redo", None)?)
        .separator()
        .item(&item("select-all", "Select &All", None)?)
        .build()?;
    let view = SubmenuBuilder::new(app, "&View")
        .item(&item("zoom-in", "Zoom &In", None)?)
        .item(&item("zoom-out", "Zoom &Out", None)?)
        .item(&item("zoom-fit", "Show the &Whole Edit", None)?)
        .separator()
        .item(&item("fullscreen", "&Full Screen", Some("F11"))?)
        .build()?;
    let about = AboutMetadata {
        name: Some("VoiceCut".into()),
        version: Some(env!("CARGO_PKG_VERSION").into()),
        copyright: Some("© 2026 VoiceCut contributors · MIT License".into()),
        website: Some("https://github.com/vectozavr/voicecut".into()),
        website_label: Some("github.com/vectozavr/voicecut".into()),
        ..Default::default()
    };
    let help = SubmenuBuilder::new(app, "&Help")
        .item(&item("shortcuts", "&Keyboard Shortcuts", None)?)
        .item(&item("help", "VoiceCut &Help", None)?)
        .separator()
        .about(Some(about))
        .build()?;
    let menu = Menu::with_items(app, &[&file, &edit, &view, &help])?;
    *app.state::<AppState>().recent_menu.lock().unwrap() = Some(recent);
    Ok(menu)
}

fn on_menu(app: &AppHandle, id: &str) {
    let focused = focused_window(app);
    match id {
        "quit" => request_quit(app),
        "new" => new_project(app, None),
        "open" => {
            let app = app.clone();
            std::thread::spawn(move || {
                let parent = focused_window(&app);
                if let Some(path) = commands::pick_document(&app, parent.as_ref()) {
                    open_paths(&app, vec![path]);
                }
            });
        }
        "recent-clear" => clear_recents(app),
        "close-window" => {
            if let Some(window) = focused {
                let _ = window.close();
            }
        }
        "fullscreen" => {
            if let Some(window) = focused {
                let on = window.is_fullscreen().unwrap_or(false);
                let _ = window.set_fullscreen(!on);
            }
        }
        "help" => {
            let _ = commands::open_in_browser(HELP_URL);
        }
        "reveal" => {
            let state = app.state::<AppState>();
            if let Some(Role::Document { path, .. }) = focused
                .as_ref()
                .and_then(|window| state.role(window.label()))
            {
                commands::reveal(&path);
            }
        }
        _ if id.starts_with("recent:") => {
            let index: usize = id["recent:".len()..].parse().unwrap_or(usize::MAX);
            let path = app
                .state::<AppState>()
                .recents
                .lock()
                .unwrap()
                .get(index)
                .cloned();
            if let Some(path) = path {
                open_paths(app, vec![path]);
            }
        }
        command => {
            if let Some(window) = focused {
                send_command(app, window.label(), json!({ "command": command }));
            } else if command == "settings" {
                if let Some(window) = show_welcome(app) {
                    send_command(app, window.label(), json!({ "command": command }));
                }
            }
        }
    }
}

// -- the application --------------------------------------------------------------------

pub fn urlencode(value: &str) -> String {
    value
        .bytes()
        .map(|byte| match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (byte as char).to_string()
            }
            _ => format!("%{byte:02X}"),
        })
        .collect()
}

/// The commands of `commands`, plus any given (the test build's).
macro_rules! handlers {
    ($($extra:path),*) => {
        tauri::generate_handler![
            commands::app_status,
            commands::start_setup,
            commands::cancel_setup,
            commands::restart_engine,
            commands::reveal_logs,
            commands::document_opened,
            commands::recent_documents,
            commands::clear_recent_documents,
            commands::open_document,
            commands::choose_new_project,
            commands::choose_document_location,
            commands::choose_export_path,
            commands::choose_source,
            commands::confirm_dialog,
            commands::open_external,
            commands::reveal_path,
            commands::flush_done,
            commands::take_pending_action,
            $($extra),*
        ]
    };
}

pub fn run() {
    #[cfg(target_os = "linux")]
    linux::prepare_webkit();
    let builder = tauri::Builder::default();
    // Linux and Windows start a new process for every file opened from the
    // file manager; the first one opens it and the new one quits.
    #[cfg(not(target_os = "macos"))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, argv, cwd| {
        let paths: Vec<PathBuf> = argv
            .iter()
            .skip(1)
            .map(|argument| Path::new(&cwd).join(argument))
            .filter(|path| path.exists())
            .collect();
        if paths.is_empty() {
            show_welcome(app);
        } else {
            open_paths(app, paths);
        }
    }));
    let builder = builder.plugin(tauri_plugin_dialog::init());
    #[cfg(feature = "automation")]
    let builder = builder.invoke_handler(handlers!(automation::automation_result));
    #[cfg(not(feature = "automation"))]
    let builder = builder.invoke_handler(handlers!());
    let app = builder
        .setup(|app| {
            let resources = app.path().resource_dir()?;
            #[cfg(target_os = "linux")]
            {
                let resources = resources.clone();
                std::thread::spawn(move || linux::integrate_appimage(&resources));
            }
            let paths = Paths::new(resources.join("runtime"));
            let bundle = Bundle::load(&paths);
            let recents = load_recents(&paths);
            app.manage(AppState {
                paths,
                bundle,
                phase: Mutex::new(Phase::Checking),
                setup: Mutex::new(None),
                control: Mutex::new(None),
                engine: Mutex::new(None),
                roles: Mutex::new(HashMap::new()),
                pending_paths: Mutex::new(Vec::new()),
                pending_actions: Mutex::new(HashMap::new()),
                recents: Mutex::new(recents),
                flushes: (Mutex::new(HashSet::new()), Condvar::new()),
                counter: AtomicU64::new(1),
                quitting: AtomicBool::new(false),
                recent_menu: Mutex::new(None),
            });
            let handle = app.handle().clone();
            #[cfg(feature = "automation")]
            automation::start(&handle);
            let menu = build_menu(&handle)?;
            app.set_menu(menu)?;
            refresh_recent_menu(&handle);
            app.on_menu_event(|app, event| on_menu(app, event.id().as_ref()));
            create_window(
                &handle,
                WebviewUrl::App("index.html".into()),
                "VoiceCut",
                Role::App,
            )
            .map_err(|error| -> Box<dyn std::error::Error> { error.into() })?;
            // Files the app was launched with: from the Finder before setup,
            // or as arguments (Linux and Windows).
            let mut arguments: Vec<PathBuf> = std::mem::take(&mut *EARLY_PATHS.lock().unwrap());
            arguments.extend(
                std::env::args_os()
                    .skip(1)
                    .map(PathBuf::from)
                    .filter(|path| path.exists()),
            );
            if !arguments.is_empty() {
                open_paths(&handle, arguments);
            }
            std::thread::spawn(move || boot(handle));
            Ok(())
        })
        .on_window_event(|window, event| {
            let app = window.app_handle();
            let state = app.state::<AppState>();
            let label = window.label().to_string();
            match event {
                WindowEvent::CloseRequested { api, .. } => {
                    if state.quitting.load(Ordering::SeqCst) {
                        return;
                    }
                    if let Some(Role::Document { project_id, path }) = state.role(&label) {
                        api.prevent_close();
                        close_document_window(app.clone(), label, project_id, path);
                    }
                }
                WindowEvent::Destroyed => {
                    state.roles.lock().unwrap().remove(&label);
                    state.pending_actions.lock().unwrap().remove(&label);
                }
                WindowEvent::DragDrop(DragDropEvent::Enter { .. }) => {
                    let _ = app.emit_to(&label, "drag", json!({ "active": true }));
                }
                WindowEvent::DragDrop(DragDropEvent::Leave) => {
                    let _ = app.emit_to(&label, "drag", json!({ "active": false }));
                }
                WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                    let _ = app.emit_to(&label, "drag", json!({ "active": false }));
                    open_paths(app, paths.clone());
                }
                _ => {}
            }
        })
        .build(tauri::generate_context!())
        .expect("VoiceCut could not start");

    app.run(|app, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Opened { urls } => {
            let paths: Vec<PathBuf> = urls
                .iter()
                .filter_map(|url| url.to_file_path().ok())
                .collect();
            if app.try_state::<AppState>().is_none() {
                EARLY_PATHS.lock().unwrap().extend(paths);
                return;
            }
            open_paths(app, paths);
        }
        #[cfg(target_os = "macos")]
        RunEvent::Reopen {
            has_visible_windows,
            ..
        } => {
            if !has_visible_windows {
                show_welcome(app);
            }
        }
        RunEvent::ExitRequested { api, code, .. } => {
            let state = app.state::<AppState>();
            if state.quitting.load(Ordering::SeqCst) {
                return;
            }
            api.prevent_exit();
            // The last window closing keeps a Mac app running; anything else
            // asking to quit goes through the same checks as Quit VoiceCut.
            if code.is_some() || !cfg!(target_os = "macos") {
                request_quit(app);
            }
        }
        RunEvent::Exit => stop_engine(app),
        _ => {}
    });
}
