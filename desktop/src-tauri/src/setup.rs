//! First-run setup: install VoiceCut's engine into the data directory.
//!
//! The steps mirror `scripts/install.sh` with every version pinned: uv and
//! micromamba (checked against their SHA-256), CPython builds through uv, the
//! Python environments from hashed lock files (on Intel Macs the main one over
//! an explicit conda lock with PyTorch), the MFA environment from an explicit
//! conda lock, Respiro-en, then a check that every part starts.
//! A step that finishes is recorded in `setup.json`; a failed or cancelled
//! setup resumes at the step that did not finish.

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::paths::Paths;
use crate::process;
use crate::runtime::{Bundle, Installed, Step};

const LOG_TAIL: usize = 14;
const DOWNLOAD_ATTEMPTS: u32 = 3;
/// The share of the main environment's step that creating its conda base
/// takes, where it has one (Intel Macs); uv installs the rest.
const BASE_SHARE: f64 = 0.45;
/// In a main environment created from a conda lock: that lock's SHA-256.
const BASE_MARKER: &str = "voicecut-base.sha256";
/// Environment variables setup tools keep: identity, locale and proxies.
const KEPT_VARIABLES: &[&str] = &[
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    // Windows cannot start programs without these.
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "CommonProgramFiles",
    "HOMEDRIVE",
    "HOMEPATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
];

#[derive(Serialize, Clone)]
pub struct StepView {
    pub id: &'static str,
    pub label: &'static str,
    pub detail: &'static str,
    /// pending, running, done, failed
    pub state: &'static str,
    pub fraction: Option<f64>,
}

#[derive(Serialize, Clone, Default)]
pub struct SetupView {
    pub steps: Vec<StepView>,
    pub running: bool,
    pub finished: bool,
    pub progress: f64,
    pub error: Option<String>,
    pub log: Vec<String>,
    /// Only VoiceCut itself changed: a quick update, not a download.
    pub light: bool,
    pub download_gb: f64,
    pub installed_gb: f64,
    pub needed_gb: f64,
    pub free_gb: Option<f64>,
}

impl SetupView {
    pub fn new(paths: &Paths, bundle: &Bundle, steps: &[Step]) -> Self {
        Self {
            steps: steps
                .iter()
                .map(|step| StepView {
                    id: step.id(),
                    label: step.label(),
                    detail: step.detail(),
                    state: "pending",
                    fraction: None,
                })
                .collect(),
            light: steps.iter().all(|step| step.is_light()),
            download_gb: bundle.spec.space.download_gb,
            installed_gb: bundle.spec.space.installed_gb,
            needed_gb: bundle.spec.space.needed_gb,
            free_gb: free_gigabytes(&paths.data),
            ..Self::default()
        }
    }

    fn recompute(&mut self, weights: &[f64]) {
        let total: f64 = weights.iter().sum();
        let done: f64 = self
            .steps
            .iter()
            .zip(weights)
            .map(|(step, weight)| match step.state {
                "done" => *weight,
                "running" => weight * step.fraction.unwrap_or(0.0),
                _ => 0.0,
            })
            .sum();
        self.progress = if total > 0.0 { done / total } else { 1.0 };
    }
}

/// Lets the window cancel a running setup.
#[derive(Default)]
pub struct Control {
    cancelled: AtomicBool,
    child: Mutex<Option<u32>>,
}

impl Control {
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
        if let Some(pid) = *self.child.lock().unwrap() {
            process::stop_tree(pid, false);
        }
    }

    fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }
}

pub fn free_gigabytes(path: &Path) -> Option<f64> {
    let mut probe = path.to_path_buf();
    while !probe.exists() {
        probe = probe.parent()?.to_path_buf();
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let c_path = std::ffi::CString::new(probe.as_os_str().as_bytes()).ok()?;
        let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c_path.as_ptr(), &mut stat) } != 0 {
            return None;
        }
        Some(stat.f_bavail as f64 * stat.f_frsize as f64 / 1e9)
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let wide: Vec<u16> = probe.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut available: u64 = 0;
        let ok = unsafe {
            windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(
                wide.as_ptr(),
                &mut available,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        };
        (ok != 0).then(|| available as f64 / 1e9)
    }
}

/// What the tools of the running step have reported, to estimate its share
/// done: uv lists what it downloads and installs, micromamba what it links.
#[derive(Default)]
struct Tally {
    packages: usize,
    announced: usize,
    downloaded: usize,
    installed: usize,
    linked: usize,
    /// Where uv's share of the step starts (after a conda base).
    start: f64,
    /// Never move the bar back when a tool announces more work.
    shown: f64,
}

struct Run<'a> {
    paths: &'a Paths,
    bundle: &'a Bundle,
    control: &'a Control,
    steps: &'a [Step],
    view: Mutex<SetupView>,
    weights: Vec<f64>,
    current: Mutex<usize>,
    tally: Mutex<Tally>,
    publish: &'a (dyn Fn(&SetupView) + Sync),
    log: Mutex<Option<File>>,
    last_publish: Mutex<Instant>,
}

impl Run<'_> {
    fn update(&self, change: impl FnOnce(&mut SetupView), force: bool) {
        let mut view = self.view.lock().unwrap();
        change(&mut view);
        view.recompute(&self.weights);
        let mut last = self.last_publish.lock().unwrap();
        if force || last.elapsed() >= Duration::from_millis(150) {
            *last = Instant::now();
            (self.publish)(&view);
        }
    }

    fn fraction(&self, fraction: f64) {
        let index = *self.current.lock().unwrap();
        self.update(
            |view| view.steps[index].fraction = Some(fraction.clamp(0.0, 1.0)),
            false,
        );
    }

    fn line(&self, line: &str) {
        let line = line.trim_end();
        if line.is_empty() {
            return;
        }
        if let Some(file) = self.log.lock().unwrap().as_mut() {
            let _ = writeln!(file, "{line}");
        }
        self.estimate(line.trim_start());
        let line = line.to_string();
        self.update(
            |view| {
                view.log.push(line);
                let excess = view.log.len().saturating_sub(LOG_TAIL);
                view.log.drain(..excess);
            },
            false,
        );
    }

    /// Advance the running step's progress from its tools' output.
    fn estimate(&self, text: &str) {
        let step = self.steps[*self.current.lock().unwrap()];
        let mut tally = self.tally.lock().unwrap();
        let fraction = match step {
            Step::MainEnv | Step::MlxEnv => {
                let start = tally.start;
                let share = |fraction: f64| start + (1.0 - start) * fraction;
                if text.starts_with("Linking ") {
                    // micromamba creating the environment's conda base.
                    tally.linked += 1;
                    Some(
                        0.05 + (BASE_SHARE - 0.05) * tally.linked as f64
                            / tally.packages.max(1) as f64,
                    )
                } else if let Some(rest) = text.strip_prefix("Resolved ") {
                    tally.packages = rest
                        .split_whitespace()
                        .next()
                        .and_then(|n| n.parse().ok())
                        .unwrap_or(0);
                    Some(share(0.08))
                } else if text.starts_with("Downloading ") {
                    tally.announced += 1;
                    None
                } else if text.starts_with("Downloaded ") {
                    tally.downloaded += 1;
                    Some(share(
                        0.08 + 0.72 * tally.downloaded as f64 / tally.announced.max(1) as f64,
                    ))
                } else if text.starts_with("Prepared ") {
                    Some(share(0.82))
                } else if text.starts_with("+ ") {
                    tally.installed += 1;
                    Some(share(
                        0.84 + 0.1 * tally.installed as f64 / tally.packages.max(1) as f64,
                    ))
                } else {
                    None
                }
            }
            Step::Aligner => text.starts_with("Linking ").then(|| {
                tally.linked += 1;
                0.35 + 0.55 * tally.linked as f64 / tally.packages.max(1) as f64
            }),
            _ => None,
        };
        let fraction = fraction
            .map(|fraction| fraction.min(0.95))
            .filter(|fraction| *fraction > tally.shown);
        if let Some(fraction) = fraction {
            tally.shown = fraction;
        }
        drop(tally);
        if let Some(fraction) = fraction {
            self.fraction(fraction);
        }
    }

    fn check(&self) -> Result<(), String> {
        if self.control.cancelled() {
            Err("Setup was cancelled.".into())
        } else {
            Ok(())
        }
    }

    /// The environment of every setup tool: nothing from the user's shell
    /// that could point it at another Python or conda installation.
    fn environment(&self) -> Vec<(OsString, OsString)> {
        let mut environment: Vec<(OsString, OsString)> = KEPT_VARIABLES
            .iter()
            .filter_map(|name| std::env::var_os(name).map(|value| ((*name).into(), value)))
            .collect();
        let paths = self.paths;
        let mut search = vec![paths.tools()];
        if cfg!(windows) {
            // The system's own folders (cmd, taskkill, ...).
            search.extend(std::env::split_paths(
                &std::env::var_os("PATH").unwrap_or_default(),
            ));
        } else {
            search.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"].map(PathBuf::from));
        }
        let entries: Vec<(&str, OsString)> = vec![
            ("PATH", std::env::join_paths(search).unwrap_or_default()),
            ("NO_COLOR", "1".into()),
            ("PYTHONNOUSERSITE", "1".into()),
            ("UV_NO_CONFIG", "1".into()),
            ("UV_NO_PROGRESS", "1".into()),
            ("UV_CACHE_DIR", paths.cache().join("uv").into()),
            ("UV_PYTHON_INSTALL_DIR", paths.python_installs().into()),
            ("UV_PYTHON_PREFERENCE", "only-managed".into()),
            ("MAMBA_ROOT_PREFIX", paths.mamba_root().into()),
            ("CONDA_PKGS_DIRS", paths.cache().join("pkgs").into()),
        ];
        environment.extend(
            entries
                .into_iter()
                .map(|(name, value)| (name.into(), value)),
        );
        environment
    }

    /// Run a tool, streaming its output into the log; fail on a non-zero exit.
    fn run(
        &self,
        program: &Path,
        args: &[&std::ffi::OsStr],
        extra: &[(&str, OsString)],
    ) -> Result<(), String> {
        self.check()?;
        let name = program
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default();
        self.line(&format!(
            "$ {name} {}",
            args.iter()
                .map(|arg| arg.to_string_lossy())
                .collect::<Vec<_>>()
                .join(" ")
        ));
        let mut command = Command::new(program);
        command
            .args(args)
            .env_clear()
            .envs(self.environment())
            .envs(extra.iter().map(|(name, value)| (name, value)))
            .current_dir(&self.paths.data)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        process::detach(&mut command);
        let mut child = command
            .spawn()
            .map_err(|error| format!("Cannot start {name}: {error}"))?;
        *self.control.child.lock().unwrap() = Some(child.id());
        let (sender, receiver) = mpsc::channel::<String>();
        let mut readers = Vec::new();
        for stream in [
            child
                .stdout
                .take()
                .map(|s| Box::new(s) as Box<dyn Read + Send>),
            child
                .stderr
                .take()
                .map(|s| Box::new(s) as Box<dyn Read + Send>),
        ]
        .into_iter()
        .flatten()
        {
            let sender = sender.clone();
            readers.push(std::thread::spawn(move || {
                let mut reader = BufReader::new(stream);
                let mut buffer = Vec::new();
                while reader.read_until(b'\n', &mut buffer).unwrap_or(0) > 0 {
                    for part in String::from_utf8_lossy(&buffer).split('\r') {
                        let _ = sender.send(part.to_string());
                    }
                    buffer.clear();
                }
            }));
        }
        drop(sender);
        for line in receiver {
            self.line(&line);
        }
        let status = child.wait().map_err(|error| error.to_string());
        *self.control.child.lock().unwrap() = None;
        for reader in readers {
            let _ = reader.join();
        }
        self.check()?;
        let status = status?;
        if status.success() {
            Ok(())
        } else {
            Err(format!(
                "{name} failed (exit status {}).",
                status
                    .code()
                    .map_or("signal".into(), |code| code.to_string())
            ))
        }
    }

    /// Download `url` to `destination`, checking its SHA-256.
    fn download(
        &self,
        url: &str,
        sha256: &str,
        destination: &Path,
        share: (f64, f64),
    ) -> Result<(), String> {
        let mut last_error = String::new();
        for attempt in 1..=DOWNLOAD_ATTEMPTS {
            self.check()?;
            match self.download_once(url, sha256, destination, share) {
                Ok(()) => return Ok(()),
                Err(error) => {
                    self.line(&format!("download attempt {attempt} failed: {error}"));
                    last_error = error;
                    std::thread::sleep(Duration::from_secs(2 * attempt as u64));
                }
            }
        }
        self.check()?;
        Err(format!(
            "Cannot download {}: {last_error}. Check your internet connection and try again.",
            url.rsplit('/').next().unwrap_or(url)
        ))
    }

    fn download_once(
        &self,
        url: &str,
        sha256: &str,
        destination: &Path,
        share: (f64, f64),
    ) -> Result<(), String> {
        self.line(&format!("GET {url}"));
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_connect(Some(Duration::from_secs(30)))
            .timeout_recv_response(Some(Duration::from_secs(60)))
            .proxy(ureq::Proxy::try_from_env())
            .build()
            .into();
        let mut response = agent.get(url).call().map_err(|error| error.to_string())?;
        let total = response.body().content_length();
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let partial = destination.with_extension("part");
        let mut file = File::create(&partial).map_err(|error| error.to_string())?;
        let mut reader = response.body_mut().as_reader();
        let mut digest = Sha256::new();
        let mut buffer = vec![0u8; 1 << 16];
        let mut received: u64 = 0;
        loop {
            self.check()?;
            let count = reader
                .read(&mut buffer)
                .map_err(|error| error.to_string())?;
            if count == 0 {
                break;
            }
            digest.update(&buffer[..count]);
            file.write_all(&buffer[..count])
                .map_err(|error| error.to_string())?;
            received += count as u64;
            if let Some(total) = total.filter(|total| *total > 0) {
                self.fraction(share.0 + share.1 * received as f64 / total as f64);
            }
        }
        file.flush().map_err(|error| error.to_string())?;
        drop(file);
        let actual = format!("{:x}", digest.finalize());
        if actual != sha256 {
            let _ = fs::remove_file(&partial);
            return Err(format!(
                "checksum mismatch (expected {sha256}, got {actual})"
            ));
        }
        fs::rename(&partial, destination).map_err(|error| error.to_string())
    }

    fn uv(&self, args: &[&std::ffi::OsStr]) -> Result<(), String> {
        self.run(&self.paths.uv(), args, &[])
    }

    // -- the steps -------------------------------------------------------------
    fn tools(&self) -> Result<(), String> {
        let spec = &self.bundle.spec;
        let paths = self.paths;
        fs::create_dir_all(paths.tools()).map_err(|error| error.to_string())?;
        let archive = paths
            .cache()
            .join(if cfg!(windows) { "uv.zip" } else { "uv.tar.gz" });
        self.download(&spec.uv.url, &spec.uv.sha256, &archive, (0.0, 0.5))?;
        let partial = paths.uv().with_extension("part");
        extract_uv(&archive, &partial)?;
        make_executable(&partial)?;
        fs::rename(&partial, paths.uv()).map_err(|error| error.to_string())?;
        let micromamba = &spec.micromamba;
        self.download(
            &micromamba.url,
            &micromamba.sha256,
            &paths.micromamba(),
            (0.5, 0.5),
        )?;
        make_executable(&paths.micromamba())
    }

    fn python(&self) -> Result<(), String> {
        let python = &self.bundle.spec.python;
        let install_dir = self.paths.python_installs();
        let mut args: Vec<&std::ffi::OsStr> = vec![
            "python".as_ref(),
            "install".as_ref(),
            "--no-bin".as_ref(),
            "--install-dir".as_ref(),
            install_dir.as_os_str(),
        ];
        if self.bundle.spec.main_base.is_none() {
            args.push(python.main.as_ref());
        }
        if self.bundle.applies(Step::MlxEnv) {
            args.push(python.mlx.as_ref());
        }
        self.uv(&args)
    }

    fn source(&self) -> Result<(), String> {
        let runtime = self.paths.runtime();
        let source = self.bundle.source();
        fs::create_dir_all(&runtime).map_err(|error| error.to_string())?;
        let target = runtime.join("src");
        if target.exists() {
            fs::remove_dir_all(&target).map_err(|error| error.to_string())?;
        }
        copy_dir(&source.join("src"), &target).map_err(|error| error.to_string())?;
        for name in ["pyproject.toml", "README.md", "LICENSE"] {
            fs::copy(source.join(name), runtime.join(name)).map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    fn environment_step(
        &self,
        venv: &Path,
        version: &str,
        requirements: &str,
        base: Option<&str>,
    ) -> Result<(), String> {
        let python = self.paths.venv_python(venv);
        if let Some(base) = base {
            self.conda_base(venv, base)?;
        } else {
            if !venv_has_version(venv, version) {
                if venv.exists() {
                    fs::remove_dir_all(venv).map_err(|error| error.to_string())?;
                }
                self.uv(&[
                    "venv".as_ref(),
                    "--python".as_ref(),
                    version.as_ref(),
                    venv.as_os_str(),
                ])?;
            }
            self.fraction(0.05);
        }
        let lock = self.bundle.file(requirements);
        // The Linux lock takes PyTorch from its CPU index and everything else
        // from PyPI; every package is checked against its hash, so take each
        // from whichever index has the pinned version.  Over a conda base,
        // `install` adds the lock to what conda installed (`sync` would remove
        // that), leaving the packages conda installed in the version the lock
        // pins; without dependencies, since what conda brings instead of PyPI
        // (PyTorch, numba) is not in the lock.
        let mut args: Vec<&std::ffi::OsStr> = vec![
            "pip".as_ref(),
            if base.is_some() { "install" } else { "sync" }.as_ref(),
            "--python".as_ref(),
            python.as_os_str(),
            "--index-strategy".as_ref(),
            "unsafe-best-match".as_ref(),
            "--require-hashes".as_ref(),
            "--compile-bytecode".as_ref(),
        ];
        if base.is_some() {
            args.extend(["--no-deps", "--requirement"].map(std::ffi::OsStr::new));
        }
        args.push(lock.as_os_str());
        self.uv(&args)?;
        self.fraction(0.9);
        // VoiceCut itself, editable like scripts/install.sh, so `voicecut` and
        // `voicecut-editor` exist and run the source in runtime/src.
        let runtime = self.paths.runtime();
        self.uv(&[
            "pip".as_ref(),
            "install".as_ref(),
            "--python".as_ref(),
            python.as_os_str(),
            "--no-deps".as_ref(),
            "--editable".as_ref(),
            runtime.as_os_str(),
        ])
    }

    /// Create `venv` as a conda environment from the explicit lock `base`,
    /// unless it already is one from the same lock (when only the Python
    /// lock changed, or setup failed after this part).
    fn conda_base(&self, venv: &Path, base: &str) -> Result<(), String> {
        let lock = self.bundle.file(base);
        let wanted = file_sha256(&lock).ok_or_else(|| format!("Cannot read {base}"))?;
        let marker = venv.join(BASE_MARKER);
        let python = self.paths.venv_python(venv);
        if python.exists() && fs::read_to_string(&marker).is_ok_and(|text| text.trim() == wanted) {
            return Ok(());
        }
        if venv.exists() {
            fs::remove_dir_all(venv).map_err(|error| error.to_string())?;
        }
        fs::create_dir_all(self.paths.mamba_root()).map_err(|error| error.to_string())?;
        self.tally.lock().unwrap().packages = explicit_packages(&lock);
        self.run(
            &self.paths.micromamba(),
            &[
                "create".as_ref(),
                "--no-rc".as_ref(),
                "--yes".as_ref(),
                "--prefix".as_ref(),
                venv.as_os_str(),
                "--file".as_ref(),
                lock.as_os_str(),
            ],
            &[],
        )?;
        fs::write(&marker, format!("{wanted}\n")).map_err(|error| error.to_string())?;
        // uv's share of the step comes after.
        let mut tally = self.tally.lock().unwrap();
        tally.start = BASE_SHARE;
        tally.shown = tally.shown.max(BASE_SHARE);
        drop(tally);
        self.fraction(BASE_SHARE);
        Ok(())
    }

    fn aligner(&self) -> Result<(), String> {
        let paths = self.paths;
        let mfa = &self.bundle.spec.mfa;
        let prefix = paths.mfa_env();
        if prefix.exists() {
            fs::remove_dir_all(&prefix).map_err(|error| error.to_string())?;
        }
        fs::create_dir_all(paths.mamba_root()).map_err(|error| error.to_string())?;
        let lock = self.bundle.file(&mfa.lock);
        self.tally.lock().unwrap().packages = explicit_packages(&lock);
        self.fraction(0.05);
        self.run(
            &paths.micromamba(),
            &[
                "create".as_ref(),
                "--no-rc".as_ref(),
                "--yes".as_ref(),
                "--prefix".as_ref(),
                prefix.as_os_str(),
                "--file".as_ref(),
                lock.as_os_str(),
            ],
            &[],
        )?;
        self.fraction(0.9);
        for patch in &mfa.patches {
            apply_patch(&prefix.join(&patch.file), &patch.sha256, &patch.remove)?;
            self.line(&format!("patched {}", patch.file));
        }
        let cache = paths.mfa_cache();
        fs::create_dir_all(cache.join("huggingface")).map_err(|error| error.to_string())?;
        let output = process::quiet(
            // Not the `mfa` command: on Windows it is a launcher that cannot
            // start Python from a path with a space (see voicecut/mfa_cli.py).
            Command::new(paths.micromamba())
                .args(["run", "--prefix"])
                .arg(&prefix)
                .arg("python")
                .arg(
                    paths
                        .runtime()
                        .join("src")
                        .join("voicecut")
                        .join("mfa_cli.py"),
                )
                .arg("version")
                .env_clear()
                .envs(self.environment())
                .env("MFA_ROOT_DIR", &cache)
                .env("HF_HOME", cache.join("huggingface")),
        )
        .output()
        .map_err(|error| format!("Cannot run MFA: {error}"))?;
        let text = String::from_utf8_lossy(&output.stdout);
        self.line(&format!("mfa version: {}", text.trim()));
        if !text.contains(&mfa.version) {
            return Err(format!(
                "The aligner did not start (expected MFA {}): {}",
                mfa.version,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(())
    }

    fn respiro(&self) -> Result<(), String> {
        let respiro = &self.bundle.spec.respiro;
        let folder = self
            .paths
            .runtime_cache()
            .join("respiro-en")
            .join(&respiro.commit);
        let count = respiro.files.len() as f64;
        for (index, (name, sha256)) in respiro.files.iter().enumerate() {
            let destination = folder.join(name);
            if file_sha256(&destination).as_deref() == Some(sha256.as_str()) {
                continue;
            }
            let url = format!("{}/{name}", respiro.url);
            self.download(
                &url,
                sha256,
                &destination,
                (index as f64 / count, 1.0 / count),
            )?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&destination, fs::Permissions::from_mode(0o600));
            }
        }
        Ok(())
    }

    /// CUDA and cuDNN for faster-whisper on an NVIDIA GPU (Linux, Windows).
    fn gpu(&self) -> Result<(), String> {
        let Some(lock) = self.bundle.spec.requirements.cuda.as_deref() else {
            return Ok(());
        };
        let python = self.paths.venv_python(&self.paths.venv());
        let lock = self.bundle.file(lock);
        self.uv(&[
            "pip".as_ref(),
            "install".as_ref(),
            "--python".as_ref(),
            python.as_os_str(),
            "--require-hashes".as_ref(),
            "--requirement".as_ref(),
            lock.as_os_str(),
        ])
    }

    fn verify(&self) -> Result<(), String> {
        let paths = self.paths;
        let main = paths.venv_python(&paths.venv());
        self.run(
            &main,
            &[
                "-c".as_ref(),
                "import voicecut.editor.server, voicecut.full_pipeline, soundfile".as_ref(),
            ],
            &[],
        )?;
        self.fraction(0.4);
        self.run(&paths.editor_command(), &["--help".as_ref()], &[])?;
        self.fraction(0.6);
        if self.bundle.applies(Step::MlxEnv) {
            let mlx = paths.venv_python(&paths.mlx_venv());
            self.run(
                &mlx,
                &[
                    "-c".as_ref(),
                    "import mlx.core, mlx_lm, mlx_whisper, voicecut.transcribe_mlx".as_ref(),
                ],
                &[],
            )?;
        } else {
            self.run(
                &main,
                &[
                    "-c".as_ref(),
                    "import faster_whisper, voicecut.transcribe_mlx".as_ref(),
                ],
                &[],
            )?;
        }
        let ffmpeg = paths.mfa_tools()[0].join(if cfg!(windows) {
            "ffmpeg.exe"
        } else {
            "ffmpeg"
        });
        self.run(
            &ffmpeg,
            &["-hide_banner".as_ref(), "-version".as_ref()],
            &[],
        )
    }

    fn cleanup(&self) -> Result<(), String> {
        let cache = self.paths.cache();
        if cache.exists() {
            fs::remove_dir_all(&cache).map_err(|error| error.to_string())?;
        }
        let packages = self.paths.mamba_root().join("pkgs");
        if packages.exists() {
            let _ = fs::remove_dir_all(packages);
        }
        Ok(())
    }

    fn step(&self, step: Step) -> Result<(), String> {
        let spec = &self.bundle.spec;
        let paths = self.paths;
        match step {
            Step::Tools => self.tools(),
            Step::Python => self.python(),
            Step::Source => self.source(),
            Step::MainEnv => self.environment_step(
                &paths.venv(),
                &spec.python.main,
                &spec.requirements.main,
                spec.main_base.as_deref(),
            ),
            Step::MlxEnv => self.environment_step(
                &paths.mlx_venv(),
                &spec.python.mlx,
                spec.requirements.mlx.as_deref().unwrap_or_default(),
                None,
            ),
            Step::Gpu => self.gpu(),
            Step::Aligner => self.aligner(),
            Step::Respiro => self.respiro(),
            Step::Verify => self.verify(),
            Step::Cleanup => self.cleanup(),
        }
    }
}

/// Run `steps`; `publish` receives every change of the setup's view.
pub fn run(
    paths: &Paths,
    bundle: &Bundle,
    steps: &[Step],
    control: &Control,
    publish: &(dyn Fn(&SetupView) + Sync),
) -> Result<(), String> {
    let _ = fs::create_dir_all(paths.logs());
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(paths.logs().join("setup.log"))
        .ok();
    let mut view = SetupView::new(paths, bundle, steps);
    view.running = true;
    let run = Run {
        paths,
        bundle,
        control,
        steps,
        view: Mutex::new(view),
        weights: steps.iter().map(|step| step.weight()).collect(),
        current: Mutex::new(0),
        tally: Mutex::new(Tally::default()),
        publish,
        log: Mutex::new(log),
        last_publish: Mutex::new(Instant::now()),
    };
    run.line(&format!(
        "VoiceCut {} setup: {}",
        env!("CARGO_PKG_VERSION"),
        steps
            .iter()
            .map(|step| step.id())
            .collect::<Vec<_>>()
            .join(", ")
    ));
    let heavy = steps.iter().any(|step| !step.is_light());
    let free = free_gigabytes(&paths.data);
    if heavy && free.is_some_and(|free| free < bundle.spec.space.needed_gb) {
        let message = format!(
            "VoiceCut needs about {:.0} GB of free disk space to install; {:.1} GB is free.",
            bundle.spec.space.needed_gb,
            free.unwrap_or(0.0)
        );
        run.update(
            |view| {
                view.running = false;
                view.error = Some(message.clone());
            },
            true,
        );
        return Err(message);
    }
    let mut installed = Installed::load(&paths.setup_state());
    for (index, step) in steps.iter().enumerate() {
        *run.current.lock().unwrap() = index;
        *run.tally.lock().unwrap() = Tally::default();
        run.update(
            |view| {
                view.steps[index].state = "running";
                view.steps[index].fraction = None;
            },
            true,
        );
        run.line(&format!("== {}", step.label()));
        let result = run.step(*step);
        if let Err(error) = result {
            run.line(&format!("error: {error}"));
            run.update(
                |view| {
                    view.steps[index].state = "failed";
                    view.running = false;
                    view.error = Some(error.clone());
                },
                true,
            );
            return Err(error);
        }
        installed
            .steps
            .insert(step.id().to_string(), bundle.fingerprint(*step));
        installed
            .save(&paths.setup_state())
            .map_err(|error| format!("Cannot save {}: {error}", paths.setup_state().display()))?;
        run.update(
            |view| {
                view.steps[index].state = "done";
                view.steps[index].fraction = Some(1.0);
            },
            true,
        );
    }
    run.update(
        |view| {
            view.running = false;
            view.finished = true;
            view.free_gb = free_gigabytes(&paths.data);
        },
        true,
    );
    Ok(())
}

fn make_executable(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o755))
            .map_err(|error| error.to_string())?;
    }
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

/// Take the `uv` executable out of its release archive (zip on Windows).
fn extract_uv(archive: &Path, destination: &Path) -> Result<(), String> {
    let file = File::open(archive).map_err(|error| error.to_string())?;
    #[cfg(windows)]
    {
        let mut zip = zip::ZipArchive::new(file).map_err(|error| error.to_string())?;
        for index in 0..zip.len() {
            let mut entry = zip.by_index(index).map_err(|error| error.to_string())?;
            if entry.name().rsplit('/').next() == Some("uv.exe") {
                let mut output = File::create(destination).map_err(|error| error.to_string())?;
                std::io::copy(&mut entry, &mut output).map_err(|error| error.to_string())?;
                return Ok(());
            }
        }
    }
    #[cfg(not(windows))]
    {
        let mut entries = tar::Archive::new(flate2::read::GzDecoder::new(file));
        for entry in entries.entries().map_err(|error| error.to_string())? {
            let mut entry = entry.map_err(|error| error.to_string())?;
            let path = entry
                .path()
                .map_err(|error| error.to_string())?
                .into_owned();
            if path.file_name().is_some_and(|name| name == "uv") {
                entry
                    .unpack(destination)
                    .map_err(|error| error.to_string())?;
                return Ok(());
            }
        }
    }
    Err("the uv download does not contain uv".into())
}

fn copy_dir(source: &Path, destination: &Path) -> std::io::Result<()> {
    fs::create_dir_all(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let target = destination.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

/// How many packages an explicit conda lock lists (one URL per line).
fn explicit_packages(lock: &Path) -> usize {
    fs::read_to_string(lock)
        .map(|text| {
            text.lines()
                .filter(|line| line.starts_with("https://"))
                .count()
        })
        .unwrap_or(0)
}

fn file_sha256(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let mut digest = Sha256::new();
    std::io::copy(&mut file, &mut digest).ok()?;
    Some(format!("{:x}", digest.finalize()))
}

fn venv_has_version(venv: &Path, version: &str) -> bool {
    let python = if cfg!(windows) {
        venv.join("Scripts/python.exe")
    } else {
        venv.join("bin/python")
    };
    python.exists()
        && fs::read_to_string(venv.join("pyvenv.cfg"))
            .map(|config| {
                config.lines().any(|line| {
                    let mut parts = line.splitn(2, '=');
                    parts.next().map(str::trim) == Some("version")
                        && parts.next().map(str::trim) == Some(version)
                })
            })
            .unwrap_or(false)
}

/// Remove `remove` from a pinned file, writing a new file: conda hard-links
/// installed files to its package cache, which must stay untouched.
fn apply_patch(path: &Path, sha256: &str, remove: &str) -> Result<(), String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("Cannot patch {}: {error}", path.display()))?;
    if file_sha256(path).as_deref() != Some(sha256) {
        return Err(format!("{} is not the expected version", path.display()));
    }
    if text.matches(remove).count() != 1 {
        return Err(format!(
            "{} does not contain the code to patch",
            path.display()
        ));
    }
    let partial = path.with_extension("py.patched");
    fs::write(&partial, text.replace(remove, "")).map_err(|error| error.to_string())?;
    fs::rename(&partial, path).map_err(|error| error.to_string())
}
