//! The runtime the app installs, as pinned in the bundle, and what is
//! installed already.
//!
//! The bundle carries `runtime/runtime.json` (tool versions, URLs and
//! SHA-256s), the hashed lock files and VoiceCut's Python source with the
//! built editor (`runtime/source`, hashed into `runtime/source.sha256` at
//! build time).  Each setup step has a fingerprint of exactly the pins it
//! installs; `setup.json` records the fingerprint of every finished step, so a
//! new app version updates only what changed and a failed setup resumes at the
//! step that failed.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::paths::Paths;

#[derive(Deserialize, Clone)]
pub struct Download {
    pub version: String,
    pub url: String,
    pub sha256: String,
}

#[derive(Deserialize, Clone)]
pub struct Pythons {
    pub main: String,
    pub mlx: String,
}

#[derive(Deserialize, Clone)]
pub struct Requirements {
    pub main: String,
    pub mlx: String,
}

#[derive(Deserialize, Serialize, Clone)]
pub struct Patch {
    pub file: String,
    pub sha256: String,
    pub remove: String,
}

#[derive(Deserialize, Clone)]
pub struct Mfa {
    pub version: String,
    pub lock: String,
    pub patches: Vec<Patch>,
}

#[derive(Deserialize, Clone)]
pub struct Respiro {
    pub commit: String,
    pub url: String,
    pub files: BTreeMap<String, String>,
}

#[derive(Deserialize, Clone)]
pub struct Space {
    pub download_gb: f64,
    pub installed_gb: f64,
    pub needed_gb: f64,
}

#[derive(Deserialize, Clone)]
pub struct Spec {
    pub uv: Download,
    pub micromamba: Download,
    pub python: Pythons,
    pub requirements: Requirements,
    pub mfa: Mfa,
    pub respiro: Respiro,
    pub space: Space,
}

/// The pinned runtime of this app version.
#[derive(Clone)]
pub struct Bundle {
    pub dir: PathBuf,
    pub spec: Spec,
    source_sha256: String,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Step {
    Tools,
    Python,
    Source,
    MainEnv,
    MlxEnv,
    Aligner,
    Respiro,
    Verify,
    Cleanup,
}

impl Step {
    pub const ALL: [Step; 9] = [
        Step::Tools,
        Step::Python,
        Step::Source,
        Step::MainEnv,
        Step::MlxEnv,
        Step::Aligner,
        Step::Respiro,
        Step::Verify,
        Step::Cleanup,
    ];

    pub fn id(self) -> &'static str {
        match self {
            Step::Tools => "tools",
            Step::Python => "python",
            Step::Source => "source",
            Step::MainEnv => "main-env",
            Step::MlxEnv => "mlx-env",
            Step::Aligner => "aligner",
            Step::Respiro => "respiro",
            Step::Verify => "verify",
            Step::Cleanup => "cleanup",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Step::Tools => "Downloading the installers",
            Step::Python => "Installing Python",
            Step::Source => "Installing VoiceCut",
            Step::MainEnv => "Installing the audio engine",
            Step::MlxEnv => "Installing speech recognition",
            Step::Aligner => "Installing the aligner and FFmpeg",
            Step::Respiro => "Downloading the breath detector",
            Step::Verify => "Checking the installation",
            Step::Cleanup => "Cleaning up",
        }
    }

    pub fn detail(self) -> &'static str {
        match self {
            Step::Tools => "uv and micromamba",
            Step::Python => "Python 3.12 and 3.11",
            Step::Source => "the editor and the Voice Cut pipeline",
            Step::MainEnv => "PyTorch, WhisperX, Silero VAD, Gemini and OpenAI clients",
            Step::MlxEnv => "MLX Whisper for Apple Silicon",
            Step::Aligner => "Montreal Forced Aligner 3.4.1, FFmpeg",
            Step::Respiro => "Respiro-en",
            Step::Verify => "starting every part once",
            Step::Cleanup => "removing downloaded archives",
        }
    }

    /// Relative share of the whole setup's time, for the progress bar.
    pub fn weight(self) -> f64 {
        match self {
            Step::Tools => 3.0,
            Step::Python => 4.0,
            Step::Source => 1.0,
            Step::MainEnv => 40.0,
            Step::MlxEnv => 15.0,
            Step::Aligner => 25.0,
            Step::Respiro => 3.0,
            Step::Verify => 7.0,
            Step::Cleanup => 2.0,
        }
    }

    /// The step is a quick refresh of VoiceCut itself, not a download.
    pub fn is_light(self) -> bool {
        matches!(self, Step::Source | Step::Verify | Step::Cleanup)
    }
}

fn sha256_hex(data: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(data);
    format!("{:x}", digest.finalize())
}

impl Bundle {
    pub fn load(paths: &Paths) -> Result<Self, String> {
        let dir = paths.bundle.clone();
        let read = |name: &str| {
            fs::read_to_string(dir.join(name))
                .map_err(|error| format!("the app is damaged: cannot read {name}: {error}"))
        };
        let spec: Spec = serde_json::from_str(&read("runtime.json")?)
            .map_err(|error| format!("the app is damaged: runtime.json: {error}"))?;
        let source_sha256 = read("source.sha256")?.trim().to_string();
        Ok(Self {
            dir,
            spec,
            source_sha256,
        })
    }

    pub fn file(&self, name: &str) -> PathBuf {
        self.dir.join(name)
    }

    pub fn source(&self) -> PathBuf {
        self.dir.join("source")
    }

    fn file_hash(&self, name: &str) -> String {
        fs::read(self.file(name))
            .map(|data| sha256_hex(&data))
            .unwrap_or_default()
    }

    /// What the step installs, as one hash: the step reruns when it changes.
    pub fn fingerprint(&self, step: Step) -> String {
        let spec = &self.spec;
        let input = match step {
            Step::Tools => format!("{} {}", spec.uv.sha256, spec.micromamba.sha256),
            Step::Python => format!(
                "{} {} {}",
                spec.uv.version, spec.python.main, spec.python.mlx
            ),
            Step::Source => self.source_sha256.clone(),
            Step::MainEnv => format!(
                "{} {}",
                spec.python.main,
                self.file_hash(&spec.requirements.main)
            ),
            Step::MlxEnv => format!(
                "{} {}",
                spec.python.mlx,
                self.file_hash(&spec.requirements.mlx)
            ),
            Step::Aligner => format!(
                "{} {}",
                self.file_hash(&spec.mfa.lock),
                serde_json::to_string(&spec.mfa.patches).unwrap_or_default()
            ),
            Step::Respiro => format!("{} {:?}", spec.respiro.commit, spec.respiro.files),
            // Checking and cleaning up follow any change.
            Step::Verify | Step::Cleanup => Step::ALL[..7]
                .iter()
                .map(|step| self.fingerprint(*step))
                .collect::<Vec<_>>()
                .join(" "),
        };
        sha256_hex(input.as_bytes())
    }
}

/// `setup.json`: the fingerprint of every finished step.
#[derive(Serialize, Deserialize, Default)]
pub struct Installed {
    pub steps: BTreeMap<String, String>,
}

impl Installed {
    pub fn load(path: &Path) -> Self {
        fs::read_to_string(path)
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let temporary = path.with_extension("json.tmp");
        fs::write(
            &temporary,
            serde_json::to_vec_pretty(self).unwrap_or_default(),
        )?;
        fs::rename(temporary, path)
    }

    pub fn is_current(&self, bundle: &Bundle, step: Step) -> bool {
        self.steps.get(step.id()) == Some(&bundle.fingerprint(step))
    }
}

/// The steps still to run, given what is installed and on disk.
pub fn pending_steps(paths: &Paths, bundle: &Bundle) -> Vec<Step> {
    let installed = Installed::load(&paths.setup_state());
    let present = |step: Step| match step {
        Step::Tools => paths.uv().is_file() && paths.micromamba().is_file(),
        Step::Source => paths
            .runtime()
            .join("src/voicecut/editor/static/index.html")
            .is_file(),
        // `exists` follows the venv's link to its Python build.
        Step::MainEnv => {
            paths.editor_command().is_file() && paths.venv_python(&paths.venv()).exists()
        }
        Step::MlxEnv => paths.venv_python(&paths.mlx_venv()).exists(),
        Step::Aligner => paths.mfa_env().join("bin/mfa").is_file(),
        _ => true,
    };
    Step::ALL
        .into_iter()
        .filter(|step| !installed.is_current(bundle, *step) || !present(*step))
        .collect()
}
