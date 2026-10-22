//! Test automation (cargo feature `automation`, never in a release build).
//!
//! With `VOICECUT_AUTOMATION_PORT` set, a test build answers JSON requests on
//! that loopback port so an end-to-end test can drive the real app:
//!
//! * `POST /eval {"label"?, "script"}` runs `script` as the body of an async
//!   function in a window's page and returns its JSON result;
//! * `POST /windows` lists the windows (label, role, URL, focus, full screen);
//! * `POST /menu {"id"}` chooses a menu item;
//! * `POST /dialogs {"answers": [...]}` answers the next native dialogs
//!   (a path, `null` for Cancel, `true`/`false` for a confirmation);
//! * `POST /fullscreen {"label", "on"}`, `POST /open {"paths"}`,
//!   `POST /close {"label"}`, `POST /status`, `POST /update` (the app's
//!   own update, as the pages see it), `POST /quit`.

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager};

use crate::{AppState, Role};

/// Menu items the app handles itself rather than a page.
const APP_COMMANDS: &[&str] = &["quit", "new", "open", "help", "reveal", "check-updates"];

static ANSWERS: OnceLock<Mutex<VecDeque<Value>>> = OnceLock::new();
static WAITING: OnceLock<Mutex<HashMap<u64, Sender<Value>>>> = OnceLock::new();

fn answers() -> &'static Mutex<VecDeque<Value>> {
    ANSWERS.get_or_init(|| Mutex::new(VecDeque::new()))
}

fn waiting() -> &'static Mutex<HashMap<u64, Sender<Value>>> {
    WAITING.get_or_init(|| Mutex::new(HashMap::new()))
}

/// The answer a test queued for the next dialog, if any.
pub fn next_answer() -> Option<Value> {
    answers().lock().unwrap().pop_front()
}

pub fn start(app: &AppHandle) {
    let Some(port) = std::env::var("VOICECUT_AUTOMATION_PORT")
        .ok()
        .and_then(|port| port.parse::<u16>().ok())
    else {
        return;
    };
    let capability = tauri::ipc::CapabilityBuilder::new("automation")
        .window("*")
        .permission("allow-automation-result");
    let _ = app.add_capability(capability);
    let listener = match TcpListener::bind(("127.0.0.1", port)) {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!("automation: cannot listen on {port}: {error}");
            return;
        }
    };
    let app = app.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let app = app.clone();
            std::thread::spawn(move || handle(&app, stream));
        }
    });
}

/// The editor's pages may report results once their engine port is known.
pub fn allow_remote(app: &AppHandle, base: &str) {
    let capability = tauri::ipc::CapabilityBuilder::new("automation-editor")
        .remote(format!("{base}/*"))
        .window("*")
        .permission("allow-automation-result");
    let _ = app.add_capability(capability);
}

#[tauri::command]
pub fn automation_result(id: u64, ok: bool, value: Option<String>) {
    if let Some(sender) = waiting().lock().unwrap().remove(&id) {
        let value = value
            .map(|text| serde_json::from_str(&text).unwrap_or(Value::String(text)))
            .unwrap_or(Value::Null);
        let _ = sender.send(json!({ "ok": ok, "value": value }));
    }
}

fn handle(app: &AppHandle, mut stream: TcpStream) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut line = String::new();
    if reader.read_line(&mut line).is_err() {
        return;
    }
    let path = line.split_whitespace().nth(1).unwrap_or("/").to_string();
    let mut length = 0usize;
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header).is_err() || header.trim().is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0; length];
    let _ = reader.read_exact(&mut body);
    let request: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let response = route(app, &path, &request);
    let text = response.to_string();
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
        text.len()
    );
}

/// An integer property of the window's NSWindow, read on the main thread:
/// `collectionBehavior` (full screen capability), `windowNumber` (for
/// `screencapture -l`).
#[cfg(target_os = "macos")]
fn ns_window_integer(
    window: &tauri::WebviewWindow,
    property: &'static std::ffi::CStr,
) -> Option<u64> {
    use std::ffi::{c_char, c_void};
    #[link(name = "objc")]
    extern "C" {
        fn sel_registerName(name: *const c_char) -> *const c_void;
        fn objc_msgSend();
    }
    let address = window.ns_window().ok()? as usize;
    let (sender, receiver) = channel();
    window
        .run_on_main_thread(move || {
            let send: extern "C" fn(*mut c_void, *const c_void) -> u64 =
                unsafe { std::mem::transmute(objc_msgSend as *const ()) };
            let selector = unsafe { sel_registerName(property.as_ptr()) };
            let _ = sender.send(send(address as *mut c_void, selector));
        })
        .ok()?;
    receiver.recv_timeout(Duration::from_secs(5)).ok()
}

#[cfg(not(target_os = "macos"))]
fn ns_window_integer(_: &tauri::WebviewWindow, _: &'static std::ffi::CStr) -> Option<u64> {
    None
}

/// The menu bar as data: each item's text, id, shortcut and state.
fn describe_menu(items: Vec<tauri::menu::MenuItemKind<tauri::Wry>>) -> Vec<Value> {
    use tauri::menu::MenuItemKind;
    items
        .into_iter()
        .map(|item| match item {
            MenuItemKind::Submenu(submenu) => json!({
                "submenu": submenu.text().ok(),
                "items": describe_menu(submenu.items().unwrap_or_default()),
            }),
            MenuItemKind::MenuItem(item) => json!({
                "id": item.id().as_ref(),
                "text": item.text().ok(),
                "enabled": item.is_enabled().ok(),
            }),
            MenuItemKind::Predefined(item) => json!({ "predefined": item.text().ok() }),
            MenuItemKind::Check(item) => json!({ "check": item.text().ok() }),
            MenuItemKind::Icon(item) => json!({ "icon": item.text().ok() }),
        })
        .collect()
}

fn window_label(app: &AppHandle, request: &Value) -> Option<String> {
    request["label"]
        .as_str()
        .map(str::to_string)
        .or_else(|| crate::focused_window(app).map(|window| window.label().to_string()))
        .or_else(|| app.webview_windows().keys().next().cloned())
}

fn route(app: &AppHandle, path: &str, request: &Value) -> Value {
    let state = app.state::<AppState>();
    match path {
        "/eval" => {
            let Some(label) = window_label(app, request) else {
                return json!({ "ok": false, "value": "no window" });
            };
            let Some(window) = app.get_webview_window(&label) else {
                return json!({ "ok": false, "value": format!("no window {label}") });
            };
            let id = state
                .counter
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let (sender, receiver) = channel();
            waiting().lock().unwrap().insert(id, sender);
            let script = request["script"].as_str().unwrap_or("return null;");
            let wrapped = format!(
                "(async () => {{ const report = (ok, value) => window.__TAURI__.core.invoke('automation_result', \
                 {{ id: {id}, ok, value: value === undefined ? null : JSON.stringify(value) }}); \
                 try {{ report(true, await (async () => {{ {script} }})()); }} \
                 catch (error) {{ report(false, String((error && error.stack) || error)); }} }})();"
            );
            if let Err(error) = window.eval(&wrapped) {
                waiting().lock().unwrap().remove(&id);
                return json!({ "ok": false, "value": error.to_string() });
            }
            let timeout = request["timeout_ms"].as_u64().unwrap_or(30_000);
            match receiver.recv_timeout(Duration::from_millis(timeout)) {
                Ok(result) => result,
                Err(_) => {
                    waiting().lock().unwrap().remove(&id);
                    json!({ "ok": false, "value": "timed out" })
                }
            }
        }
        "/windows" => {
            let roles = state.roles.lock().unwrap().clone();
            let windows: Vec<Value> = app
                .webview_windows()
                .into_iter()
                .map(|(label, window)| {
                    let role = match roles.get(&label) {
                        Some(Role::App) => json!("app"),
                        Some(Role::Welcome) => json!("welcome"),
                        Some(Role::Document { project_id, path }) => {
                            json!({ "project_id": project_id, "path": path })
                        }
                        None => Value::Null,
                    };
                    let size = window
                        .inner_size()
                        .ok()
                        .map(|size| json!([size.width, size.height]));
                    json!({
                        "label": label,
                        "role": role,
                        "title": window.title().ok(),
                        "url": window.url().ok().map(|url| url.to_string()),
                        "focused": window.is_focused().ok(),
                        "visible": window.is_visible().ok(),
                        "fullscreen": window.is_fullscreen().ok(),
                        "resizable": window.is_resizable().ok(),
                        "collection_behavior": ns_window_integer(&window, c"collectionBehavior"),
                        "window_number": ns_window_integer(&window, c"windowNumber"),
                        "theme": window.theme().ok(),
                        "size": size,
                    })
                })
                .collect();
            json!(windows)
        }
        "/menu" => {
            let id = request["id"].as_str().unwrap_or_default();
            // The screen may be locked in a test, leaving no window focused:
            // a command for a page goes straight to the window named.
            match request["label"].as_str() {
                Some(label) if !APP_COMMANDS.contains(&id) && !id.starts_with("recent") => {
                    crate::send_command(app, label, json!({ "command": id }));
                }
                _ => crate::on_menu(app, id),
            }
            json!({ "ok": true })
        }
        "/dialogs" => {
            let mut queue = answers().lock().unwrap();
            queue.clear();
            queue.extend(request["answers"].as_array().cloned().unwrap_or_default());
            json!({ "ok": true })
        }
        "/fullscreen" => {
            match window_label(app, request).and_then(|label| app.get_webview_window(&label)) {
                Some(window) => {
                    json!({ "ok": window.set_fullscreen(request["on"].as_bool().unwrap_or(true)).is_ok() })
                }
                None => json!({ "ok": false }),
            }
        }
        "/open" => {
            let paths = request["paths"]
                .as_array()
                .map(|paths| {
                    paths
                        .iter()
                        .filter_map(|path| path.as_str().map(std::path::PathBuf::from))
                        .collect()
                })
                .unwrap_or_default();
            crate::open_paths(app, paths);
            json!({ "ok": true })
        }
        "/close" => {
            match window_label(app, request).and_then(|label| app.get_webview_window(&label)) {
                Some(window) => json!({ "ok": window.close().is_ok() }),
                None => json!({ "ok": false }),
            }
        }
        "/status" => serde_json::to_value(crate::status(app)).unwrap_or(Value::Null),
        "/update" => serde_json::to_value(crate::updates::view(app)).unwrap_or(Value::Null),
        "/menu-bar" => json!(app
            .menu()
            .map(|menu| describe_menu(menu.items().unwrap_or_default()))),
        "/quit" => {
            crate::request_quit(app);
            json!({ "ok": true })
        }
        _ => json!({ "ok": false, "value": format!("unknown request {path}") }),
    }
}
