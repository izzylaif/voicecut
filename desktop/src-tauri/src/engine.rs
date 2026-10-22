//! The engine: `voicecut-editor --desktop`, the VoiceCut editor's server, on
//! a free port of the loopback interface.
//!
//! It runs as the root of its own process tree with the app's pid as
//! `--parent-pid`, so it stops itself (and any Voice Cut run, and the Ollama
//! it may have started for local planners) if the app dies.
//! Stopping it asks it to cancel running jobs first, then ends the tree
//! (SIGTERM to the process group and, after a grace period, SIGKILL; on
//! Windows `taskkill /T`).

use std::fs::{self, OpenOptions};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::paths::Paths;
use crate::process;

/// Tried first so the editor keeps one origin (and its remembered playback
/// speed) from launch to launch.
const PREFERRED_PORT: u16 = 47615;
const START_TIMEOUT: Duration = Duration::from_secs(120);
const STOP_GRACE: Duration = Duration::from_secs(8);
const LOG_LIMIT_BYTES: u64 = 10 << 20;

pub struct Engine {
    pub port: u16,
    pid: u32,
    child: Arc<Mutex<Child>>,
    stopping: Arc<AtomicBool>,
}

fn free_port() -> u16 {
    TcpListener::bind(("127.0.0.1", PREFERRED_PORT))
        .or_else(|_| TcpListener::bind(("127.0.0.1", 0)))
        .and_then(|listener| listener.local_addr())
        .map(|address| address.port())
        .unwrap_or(PREFERRED_PORT)
}

fn agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .http_status_as_error(false)
        .build()
        .into()
}

impl Engine {
    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    /// Start the engine and wait until it answers.  `on_exit` runs if it
    /// stops without being asked to.
    pub fn start(
        paths: &Paths,
        on_exit: impl FnOnce(String) + Send + 'static,
    ) -> Result<Self, String> {
        let port = free_port();
        let logs = paths.logs();
        fs::create_dir_all(&logs).map_err(|error| error.to_string())?;
        fs::create_dir_all(paths.editor()).map_err(|error| error.to_string())?;
        let log_path = logs.join("engine.log");
        if fs::metadata(&log_path).is_ok_and(|meta| meta.len() > LOG_LIMIT_BYTES) {
            let _ = fs::rename(&log_path, logs.join("engine.previous.log"));
        }
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|error| error.to_string())?;
        let mut search: Vec<PathBuf> = vec![paths.tools()];
        search.extend(paths.mfa_tools());
        if cfg!(windows) {
            // The system's own folders (taskkill, explorer, ...).
            search.extend(std::env::split_paths(
                &std::env::var_os("PATH").unwrap_or_default(),
            ));
        } else {
            search.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"].map(PathBuf::from));
        }
        if paths.development && !cfg!(windows) {
            // A checkout finds micromamba where its developer put it.
            search.extend(std::env::split_paths(
                &std::env::var_os("PATH").unwrap_or_default(),
            ));
        }
        let mut command = Command::new(paths.editor_command());
        command
            .args([
                "--desktop",
                "--keys",
                "keychain",
                "--host",
                "127.0.0.1",
                "--port",
            ])
            .arg(port.to_string())
            .arg("--projects-dir")
            .arg(paths.editor())
            .arg("--env-file")
            .arg(paths.editor().join("voicecut.env"))
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .current_dir(paths.runtime())
            .env("PATH", std::env::join_paths(search).unwrap_or_default())
            .env("PYTHONNOUSERSITE", "1")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONUTF8", "1")
            .env("MAMBA_ROOT_PREFIX", paths.mamba_root())
            // Where the editor's Ollama logs, and VoiceCut's own Ollama
            // (the editor starts it when the system has none running).
            .env("VOICECUT_OLLAMA_LOG", logs.join("ollama.log"))
            .env("VOICECUT_OLLAMA_MODELS", paths.ollama_models())
            .env_remove("PYTHONPATH")
            .env_remove("PYTHONHOME")
            .env_remove("VIRTUAL_ENV")
            .env_remove("CONDA_PREFIX")
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|error| error.to_string())?)
            .stderr(log);
        if paths.ollama_executable().is_file() {
            command.env("VOICECUT_OLLAMA", paths.ollama_executable());
        }
        process::detach(&mut command);
        let child = command
            .spawn()
            .map_err(|error| format!("Cannot start the VoiceCut engine: {error}"))?;
        let pid = child.id();
        let engine = Engine {
            port,
            pid,
            child: Arc::new(Mutex::new(child)),
            stopping: Arc::new(AtomicBool::new(false)),
        };
        let health = format!("{}/api/health", engine.base_url());
        let started = Instant::now();
        loop {
            if let Ok(Some(status)) = engine.child.lock().unwrap().try_wait() {
                return Err(format!(
                    "The VoiceCut engine stopped while starting ({status}). See {}",
                    log_path.display()
                ));
            }
            if agent(Duration::from_secs(2))
                .get(&health)
                .call()
                .is_ok_and(|response| response.status() == 200)
            {
                break;
            }
            if started.elapsed() > START_TIMEOUT {
                engine.kill();
                return Err(format!(
                    "The VoiceCut engine did not start in time. See {}",
                    log_path.display()
                ));
            }
            std::thread::sleep(Duration::from_millis(150));
        }
        let child = engine.child.clone();
        let stopping = engine.stopping.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(500));
            let status = child.lock().unwrap().try_wait();
            match status {
                Ok(None) => continue,
                Ok(Some(status)) => {
                    if !stopping.load(Ordering::SeqCst) {
                        on_exit(format!(
                            "The VoiceCut engine stopped unexpectedly ({status})."
                        ));
                    }
                    return;
                }
                Err(_) => return,
            }
        });
        Ok(engine)
    }

    /// GET a JSON document from the engine.
    pub fn get(&self, path: &str) -> Result<serde_json::Value, String> {
        let mut response = agent(Duration::from_secs(30))
            .get(&format!("{}{path}", self.base_url()))
            .call()
            .map_err(|error| error.to_string())?;
        read_json(&mut response)
    }

    /// POST a JSON body to the engine and read its JSON answer.
    pub fn post(
        &self,
        path: &str,
        body: serde_json::Value,
        timeout: Duration,
    ) -> Result<serde_json::Value, String> {
        let mut response = agent(timeout)
            .post(&format!("{}{path}", self.base_url()))
            .send_json(body)
            .map_err(|error| error.to_string())?;
        read_json(&mut response)
    }

    /// Cancel running Voice Cut jobs, then stop the engine's process group.
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        let _ = self.post(
            "/api/desktop/stop-jobs",
            serde_json::json!({}),
            Duration::from_secs(15),
        );
        if cfg!(windows) {
            // Nothing to ask politely on Windows: the jobs are stopped already.
            self.kill();
            return;
        }
        process::stop_tree(self.pid, false);
        let deadline = Instant::now() + STOP_GRACE;
        while Instant::now() < deadline {
            if !matches!(self.child.lock().unwrap().try_wait(), Ok(None)) {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        self.kill();
    }

    fn kill(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        process::stop_tree(self.pid, true);
        let _ = self.child.lock().unwrap().wait();
    }
}

fn read_json(response: &mut ureq::http::Response<ureq::Body>) -> Result<serde_json::Value, String> {
    let status = response.status();
    let text = response
        .body_mut()
        .with_config()
        .limit(64 << 20)
        .read_to_string()
        .map_err(|error| error.to_string())?;
    let value: serde_json::Value = if text.trim().is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_str(&text).map_err(|error| error.to_string())?
    };
    if status.is_success() {
        Ok(value)
    } else {
        Err(value
            .get("detail")
            .and_then(|detail| detail.as_str())
            .map(str::to_string)
            .unwrap_or_else(|| format!("HTTP {status}")))
    }
}
