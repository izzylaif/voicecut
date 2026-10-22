//! Where VoiceCut keeps its engine and its own state.
//!
//! ```text
//! ~/Library/Application Support/VoiceCut/
//!   runtime/          laid out like a VoiceCut checkout, so the Python code
//!     src/voicecut/   finds everything where scripts/install.sh puts it:
//!     .venv/          Python 3.12: editor, pipeline, WhisperX, planners
//!     .venv-mlx/      Python 3.11: MLX Whisper, local planners
//!     .mfa-env/       micromamba: Montreal Forced Aligner, FFmpeg
//!     .voicecut-cache/runtime/   MFA models, Respiro-en
//!   tools/            uv, micromamba
//!   python/           the CPython builds the environments use
//!   editor/           the engine's own directory (remembered settings)
//!   mamba/            micromamba's root prefix
//!   cache/            downloads during setup (removed afterwards)
//!   logs/             setup.log, engine.log
//!   setup.json        which parts of the runtime are installed
//!   recent.json       File > Open Recent
//!   preferences.json  appearance, playback speed
//! ```
//!
//! Model weights download on first use into the usual Hugging Face cache
//! (`~/.cache/huggingface`), shared with other tools.
//!
//! Linux uses `$XDG_DATA_HOME/voicecut` (`~/.local/share/voicecut`), Windows
//! `%LOCALAPPDATA%\com.vectozavr.voicecut`, with the same layout (`Scripts\`
//! instead of `bin/` inside the environments).

use std::path::{Path, PathBuf};

pub struct Paths {
    pub data: PathBuf,
    /// The runtime spec and VoiceCut's Python source inside the app bundle.
    pub bundle: PathBuf,
    /// `VOICECUT_RUNTIME`: a developer's checkout (with its `.venv`s and
    /// `.mfa-env` from scripts/install.sh) used as the runtime, without setup.
    runtime: Option<PathBuf>,
    pub development: bool,
}

impl Paths {
    pub fn new(bundle: PathBuf) -> Self {
        let data = std::env::var_os("VOICECUT_DATA_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(default_data_dir);
        let runtime = std::env::var_os("VOICECUT_RUNTIME").map(PathBuf::from);
        let development = runtime.is_some();
        Self {
            data,
            bundle,
            runtime,
            development,
        }
    }

    pub fn runtime(&self) -> PathBuf {
        self.runtime
            .clone()
            .unwrap_or_else(|| self.data.join("runtime"))
    }
    pub fn tools(&self) -> PathBuf {
        self.data.join("tools")
    }
    pub fn uv(&self) -> PathBuf {
        self.tools().join(executable("uv"))
    }
    pub fn micromamba(&self) -> PathBuf {
        self.tools().join(executable("micromamba"))
    }
    pub fn python_installs(&self) -> PathBuf {
        self.data.join("python")
    }
    pub fn cache(&self) -> PathBuf {
        self.data.join("cache")
    }
    pub fn mamba_root(&self) -> PathBuf {
        self.data.join("mamba")
    }
    pub fn editor(&self) -> PathBuf {
        self.data.join("editor")
    }
    pub fn logs(&self) -> PathBuf {
        self.data.join("logs")
    }
    pub fn setup_state(&self) -> PathBuf {
        self.data.join("setup.json")
    }
    pub fn recent(&self) -> PathBuf {
        self.data.join("recent.json")
    }
    pub fn preferences(&self) -> PathBuf {
        self.data.join("preferences.json")
    }

    pub fn venv(&self) -> PathBuf {
        self.runtime().join(".venv")
    }
    pub fn mlx_venv(&self) -> PathBuf {
        self.runtime().join(".venv-mlx")
    }
    pub fn mfa_env(&self) -> PathBuf {
        self.runtime().join(".mfa-env")
    }
    /// The MFA command conda installs.
    pub fn mfa_executable(&self) -> PathBuf {
        if cfg!(windows) {
            self.mfa_env().join("Scripts").join("mfa.exe")
        } else {
            self.mfa_env().join("bin").join("mfa")
        }
    }
    /// Where FFmpeg and the other command-line tools of the MFA environment are.
    pub fn mfa_tools(&self) -> Vec<PathBuf> {
        if cfg!(windows) {
            let env = self.mfa_env();
            vec![env.join("Library").join("bin"), env.join("Scripts"), env]
        } else {
            vec![self.mfa_env().join("bin")]
        }
    }
    pub fn runtime_cache(&self) -> PathBuf {
        self.runtime().join(".voicecut-cache").join("runtime")
    }
    pub fn mfa_cache(&self) -> PathBuf {
        self.runtime_cache().join("mfa")
    }
    /// `voicecut-editor`, the engine the app shows in its windows.
    pub fn editor_command(&self) -> PathBuf {
        bin(&self.venv(), "voicecut-editor")
    }
    pub fn venv_python(&self, venv: &Path) -> PathBuf {
        bin(venv, "python")
    }
}

fn executable(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    }
}

fn bin(venv: &Path, name: &str) -> PathBuf {
    if cfg!(windows) {
        venv.join("Scripts").join(format!("{name}.exe"))
    } else {
        venv.join("bin").join(name)
    }
}

/// `path` absolute with its links resolved, written the way programs expect
/// (`std::fs::canonicalize` gives `\\?\C:\...` on Windows).
pub fn canonical(path: &Path) -> std::io::Result<PathBuf> {
    let path = path.canonicalize()?;
    #[cfg(windows)]
    {
        let text = path.to_string_lossy();
        if let Some(share) = text.strip_prefix(r"\\?\UNC\") {
            return Ok(PathBuf::from(format!(r"\\{share}")));
        }
        if let Some(rest) = text.strip_prefix(r"\\?\") {
            return Ok(PathBuf::from(rest));
        }
    }
    Ok(path)
}

fn default_data_dir() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    if cfg!(target_os = "macos") {
        home.join("Library/Application Support/VoiceCut")
    } else if cfg!(windows) {
        // Not %LOCALAPPDATA%\VoiceCut: the installer puts the app there.
        // The uninstaller's "Delete the application data" removes this one.
        std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or(home)
            .join("com.vectozavr.voicecut")
    } else {
        std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".local/share"))
            .join("voicecut")
    }
}
