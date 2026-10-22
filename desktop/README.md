# VoiceCut for macOS, Linux and Windows

The VoiceCut editor as a desktop app: a small download, a one-time setup of
VoiceCut's engine, then projects as `.vc` documents you open from the Finder,
the file manager or Explorer.

| | Installer | Engine and data | Whisper |
|---|---|---|---|
| macOS 14+, Apple Silicon | `VoiceCut-<v>-universal.dmg` | `~/Library/Application Support/VoiceCut` | MLX (GPU) |
| macOS 14+, Intel | the same | the same | faster-whisper on the CPU |
| Linux x86_64, glibc 2.35+ | `VoiceCut-<v>-x86_64.AppImage` or `-amd64.deb` | `$XDG_DATA_HOME/voicecut` (`~/.local/share/voicecut`) | faster-whisper: CUDA on NVIDIA, else CPU |
| Windows 10/11 x64 | `VoiceCut-<v>-x64-setup.exe` | `%LOCALAPPDATA%\com.vectozavr.voicecut` | faster-whisper: CUDA on NVIDIA, else CPU |

```
VoiceCut (Tauri, ~10 MB)                    data directory (see above)
┌──────────────────────────────┐            ┌─────────────────────────────────────────┐
│ menus, windows, dialogs,     │  spawns    │ runtime/  (a VoiceCut checkout's layout)│
│ documents, first-run setup   ├───────────►│   .venv/…/voicecut-editor --desktop     │
│                              │            │     └─ worker ─ pipeline ─ Whisper      │
│ system webview ── editor ────┼─ HTTP ────►│   127.0.0.1:<port>           └─ MFA     │
└──────────────────────────────┘            └─────────────────────────────────────────┘
```

* `src-tauri/` — the app (Rust): `setup.rs` installs the engine, `engine.rs`
  runs it, `lib.rs` has windows, menus, documents and quitting, `commands.rs`
  what the pages may call, `updates.rs` the app's own updates, `process.rs`
  process trees on each system, `linux.rs` what an AppImage registers for
  itself, `macos.rs` the Dock.
* `ui/` — the app's own pages: first-run setup, starting, errors.
* `runtime/` — what setup installs, pinned per system: tool versions and
  checksums (`runtime.json`), hashed Python locks, explicit conda locks of MFA
  (and of PyTorch for Intel Macs).
* `icons/` — the app icon, the document icon and the disk-image background,
  drawn by hand as SVG; `scripts/make-icons.sh` renders them (the rendered
  icons are committed, so only macOS builds need it).
* `linux/`, `windows/` — the menu entry, the MIME type and the installer hook
  that give `.vc` documents their icon.
* `scripts/` — `build-macos.sh` (the universal `.dmg`), `stage_runtime.py`
  (what every build bundles), `lock-runtime.sh` (the locks),
  `updater_manifest.py` (a release's `latest.json`).
* `tests/` — the end-to-end test of a built app (`e2e.py`) and the pipeline
  check it runs inside the installed engine (`pipeline_smoke.py`).

The editor itself is the same code as `voicecut-editor` in a browser
(`src/voicecut/editor`, `frontend/`); `frontend/src/desktop.ts` is the only
part that knows about the app.

## Why Tauri

| | Tauri (system webview) | Electron (Chromium) | pywebview |
|---|---|---|---|
| Download | ~4 MB `.dmg` | ~100 MB `.dmg` | ~30 MB (it must ship a Python to run setup) |
| Engine | WKWebView, WebKitGTK, WebView2 | its own Chromium | the same system webviews |
| Menus, documents, Dock | native | native | partial |

The engine (Python, PyTorch, Whisper, MFA) is several GB whatever the shell,
so the shell is chosen for the download, memory and native integration:
Tauri. Its price is three web engines. Every editor feature was checked in
WKWebView (audio at the recording's sample rate, the 0.5×–2.5×
pitch-preserving stretch, the video proxy and its sync, the canvas timeline
with trackpad pinch, captions); WebView2 is Chromium, which the editor was
written in. WebKitGTK plays audio and video through GStreamer, which the
AppImage carries along and the `.deb` depends on.

## What the app installs

On first launch the app shows what it will install and asks to go ahead.
Setup does what `scripts/install.sh` does in a checkout, pinned and verified:

1. uv and micromamba, checked against their SHA-256;
2. CPython through uv (3.12; on Apple Silicon also 3.11 for MLX; an Intel
   Mac's Python comes with its PyTorch, see "Intel Macs");
3. VoiceCut's source and built editor, from the app;
4. `.venv` (editor, pipeline, PyTorch, WhisperX, planners) from the
   system's hashed lock; on Intel Macs, Linux and Windows it also holds
   faster-whisper and PyTorch for the CPU;
5. Apple Silicon: `.venv-mlx` (MLX Whisper, local planners);
   Linux and Windows with an NVIDIA GPU (`nvidia-smi` finds one): the CUDA 12
   cuBLAS and cuDNN libraries faster-whisper needs, from
   `requirements-cuda-*.txt`;
6. `.mfa-env` (MFA 3.4.1 with Kaldi and FFmpeg) from the explicit conda lock,
   plus a one-line fix of MFA 3.4.1, which quotes tool paths containing a
   space and so cannot run from "Application Support" or `C:\Users\First Last`;
7. Respiro-en (breath detection), checked against its SHA-256;
8. a check that every part starts, and removal of the downloaded archives.

About 0.8–1.6 GB is downloaded and 3.4–4.3 GB stays on disk (the CUDA
libraries add about 1 GB). Each step records a fingerprint of its pins in
`setup.json`: a failed or cancelled setup resumes at the step that did not
finish ("Try Again"), and a new app version updates only what changed (a new
VoiceCut source takes seconds). Model weights download on first use into
`~/.cache/huggingface`, as with the command line. Logs are in the data
directory's `logs/`, and the appearance (System, Light, Dark) and playback
speed in its `preferences.json`: the editor's page cannot keep them, since its
address changes with the engine's port on every launch.

While it runs, setup keeps what it downloads in the data directory's `cache/`
— except the conda packages on Windows, which go to
`%USERPROFILE%\.voicecut-pkgs`: micromamba nests each package under its
channel's URL, which puts the MFA lock's longest file 201 characters below
the cache, and Windows refuses paths over 259 characters unless an
administrator has enabled long paths; under
`%LOCALAPPDATA%\com.vectozavr.voicecut\cache` that came to 263 characters
even for a four-letter user name. Both are removed when setup finishes.

To start over, quit VoiceCut and delete its data directory. To uninstall:
macOS — also delete `VoiceCut.app` and the "VoiceCut" items in Keychain
Access; Windows — Settings › Apps (tick "Delete the application data" to
remove the engine too); `.deb` — remove the package; AppImage — delete the
file, `~/.local/share/applications/voicecut-appimage.desktop` and
`~/.local/share/mime/packages/voicecut.xml`.

## Whisper on Intel Macs, Linux and Windows

MLX exists only on Apple Silicon. Elsewhere VoiceCut transcribes and recovers
untranscribed speech with faster-whisper (CTranslate2) and the same
large-v3-turbo model: in float16 on an NVIDIA GPU (checked with a one-second
decode, falling back to the CPU when the driver is too old), in float32 on the
CPU. int8 is faster on the CPU but invented a repeated phrase on a retake, so
it is not used. On the same recordings faster-whisper float32 produced the
same words and word timings as MLX, including the restarts the planner relies
on. On an M1 Max CPU it ran at about 0.9× real time (a 101 s take in 92 s,
against 27 s on MLX; 7.5 minutes in 6.4 minutes); expect similar on a recent
desktop CPU and many times faster on a GPU. The pipeline's `--whisper-engine`
chooses an engine by hand.

## Intel Macs

The disk image holds one universal app. On an Intel Mac it installs an engine
of its own (`macos-x86_64` in `runtime.json`) with Whisper on faster-whisper,
as on Linux. What differs is where PyTorch comes from: PyPI has no macOS
x86_64 wheels of PyTorch or torchaudio after 2.2.2, of numba after 0.62 or of
llvmlite after 0.45, and WhisperX 3.8.6 and pyannote.audio 4 need PyTorch
2.8. conda-forge still builds all of them, so setup creates `.venv` as a
conda environment from `runtime/main-osx-64.txt` — Python 3.12.15, PyTorch
2.8.0 (the OpenBLAS build), torchaudio 2.8.0, numba and llvmlite in the
versions the other locks pin — and installs `requirements-macos-x86_64.txt`,
everything else, hash-checked from PyPI, into it with `uv pip install
--no-deps`. Packages both bring are pinned to the same versions, and uv keeps
conda's copies. A new Python lock alone keeps the conda part. Otherwise:

* onnxruntime and cryptography are at 1.23.2 and 48.0.1, their last versions
  with Intel wheels;
* conda-forge's torchaudio brings torchcodec 0.8.1 (WhisperX asks for one
  below 0.8) and there is no torchvision; VoiceCut uses neither, and these
  are the two complaints of `uv pip check`;
* numpy comes from PyPI, as everywhere: conda's numpy calls MKL, which
  breaks once CTranslate2 is loaded;
* CTranslate2's wheel carries its own MKL and Intel OpenMP, so PyTorch cannot
  compute in a process that loaded CTranslate2 (MKL errors with conda's MKL
  build of PyTorch, a crash with the OpenBLAS one). The pipeline runs Whisper
  in processes of its own, where CTranslate2 imports PyTorch but never
  computes with it; `tests/test_transcribe_mlx.py` checks that the stages
  computing with PyTorch never load CTranslate2.

## Documents

A project is a `.vc` document holding the editor project — `project.json`,
the canonical WAV, peaks, video proxy, the timeline, exports — and the
pipeline's work directory with all its stages. The editor works on it in
place: every edit is saved as you make it, so File › Save only confirms it,
and reopening a document shows the finished Voice Cut edit without running
anything.

* On macOS the Finder shows a `.vc` folder as one file (a document package,
  declared in `src-tauri/Info.plist`) with its own icon.
* Linux and Windows have no packages: a `.vc` document is a folder, and
  inside it `<name>.vcproj`, a small launcher file. Double-clicking the
  launcher opens the project; it has the document icon and opens with
  VoiceCut (the `.deb` installs the MIME type `application/x-voicecut-project`,
  the AppImage registers it in `~/.local/share` when it starts, the Windows
  installer registers `.vcproj`). File › Open accepts the folder or the
  launcher, and so does dropping either on the window. A second VoiceCut
  started by the file manager hands its file to the first and quits.
* The source recording is referenced, not copied (a video can be many GB,
  and the document already holds the audio the editor needs). If it moves,
  VoiceCut looks for it beside the document and checks its SHA-256;
  otherwise the editor asks you to locate it. Editing and audio export work
  without it; a new Voice Cut run and video export need it.
* Moved or copied documents rebase their own paths when opened, also between
  systems; Save As copies a document and keeps editing the copy.
* A project of the browser editor (`.voicecut-cache/editor/<id>`) becomes a
  document by copying its folder to `<name>.vc`; opening it brings its Voice
  Cut run inside.

`src/voicecut/editor/documents.py` has the details.

## Privacy prompts (macOS)

The engine runs as VoiceCut, so macOS asks once, the first time VoiceCut
reads a recording or writes a project or export in the Desktop, Documents or
Downloads folder, on a removable or a network volume: "“VoiceCut” would like
to access files in your Desktop folder." Until the question is answered, the
file stays closed and the editor waits. The answer can be changed in System
Settings › Privacy & Security › Files and Folders. An ad-hoc signed build is a
new app to macOS after every rebuild and asks again — also after every update
VoiceCut installs itself (see "Updates"); a Developer ID build keeps the
answer across updates.

## Updates

VoiceCut updates itself from this repository's GitHub Releases
(`tauri-plugin-updater`, `src-tauri/src/updates.rs`):

* Ten seconds after launch and then every four hours, but never while Voice
  Cut or an export runs, the app reads the latest published release's
  manifest, `https://github.com/vectozavr/voicecut/releases/latest/download/latest.json`.
* When it lists a newer version, the editor's toolbar and the start page show
  a download-arrow button, "Update available: <version>". A click downloads
  the update, saves every open document as quitting does (asking first if
  Voice Cut or an export is running), installs it and restarts VoiceCut with
  the same documents open. VoiceCut › Check for Updates… (Help › Check for
  Updates… on Linux and Windows) looks right away.
* Settings › Updates › "Install updates automatically", on by default and
  kept in `preferences.json` in the data directory: the update downloads in
  the background and installs when VoiceCut quits — or, if VoiceCut was
  stopped without quitting, at the next launch before any window opens.
* Only the app is updated this way. The engine follows at the next launch,
  when the source inside the new app differs from what it runs (see "What
  the app installs").

What an install does:

* **macOS** swaps `VoiceCut.app` for the release's `.app.tar.gz`, asking for
  an administrator's password only if the user cannot write to the app. A
  VoiceCut opened from the disk image or straight from Downloads runs from a
  read-only copy and cannot update; it says to move it to Applications.
  An ad-hoc signed VoiceCut is a new app to macOS after each update, so the
  Files and Folders prompts (see "Privacy prompts") come back once.
* **Windows** runs the new setup `.exe` in passive mode (a progress bar, no
  questions); it starts VoiceCut again.
* **AppImage** replaces itself.
* **.deb** installs with `pkexec dpkg -i`, which asks for an administrator's
  password, so on Debian and Ubuntu updates install only from the button,
  never by themselves at quit. `apt` can install the release's `.deb` too.

### The signing key

Before installing anything the app checks the package's signature against
the public key in `src-tauri/tauri.conf.json` (`plugins.updater.pubkey`).
The matching private key signs the release builds; it is not in the
repository. It was made with
`npx tauri signer generate --ci -w ~/.tauri/voicecut-updater.key` (no
password), and the workflows read it from the repository secret
`TAURI_SIGNING_PRIVATE_KEY` (the key file's text; a key with a password also
needs `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`). Without the secret the
installers build as before, with no signatures, and the draft release gets
no `latest.json`.

Keep a copy of the private key in a safe place. Without it no later build can
be signed so that the installed apps accept it: every user would have to
download the next version by hand, with a new public key inside.

### Releasing an update

With the key, `tauri build --config src-tauri/tauri.updater.conf.json` (what
`build-macos.sh` and `desktop.yml` add) writes a signature next to every
installer and, on macOS, `VoiceCut.app.tar.gz`. `release.yml` attaches the
installers, the macOS archive (`-universal.app.tar.gz`, which `latest.json`
lists for both `darwin-aarch64` and `darwin-x86_64`) and `latest.json`
(`scripts/updater_manifest.py`, with the signatures inside) to the draft.
Once the draft is published, `releases/latest/download/latest.json` serves
its manifest and every installed VoiceCut offers the update. Each release
needs a higher version in `src-tauri/tauri.conf.json`: the app installs only
a later version (a pre-release only over a pre-release).

### Trying an update locally

A test build (cargo feature `automation`) looks for updates only at
`VOICECUT_UPDATE_ENDPOINT`, never on GitHub (a release would replace it with
the real app), and `tauri.test.conf.json` lets it use plain HTTP. On a Mac:

```bash
# The old version, somewhere writable.
VOICECUT_TEST_BUILD=1 desktop/scripts/build-macos.sh
ditto desktop/src-tauri/target/universal-apple-darwin/release/bundle/macos/VoiceCut.app \
  ~/vc-update/VoiceCut.app
# A newer one for this Mac (-x86_64 instead of -arm64 on an Intel Mac),
# signed, and its manifest.
(cd desktop && TAURI_SIGNING_PRIVATE_KEY=~/.tauri/voicecut-updater.key TAURI_SIGNING_PRIVATE_KEY_PASSWORD= \
  npx tauri build --bundles app --features automation --config src-tauri/tauri.test.conf.json \
  --config src-tauri/tauri.updater.conf.json --config '{"version": "0.2.1"}')
mkdir ~/vc-update/release
for file in VoiceCut.app.tar.gz{,.sig}; do
  cp "desktop/src-tauri/target/release/bundle/macos/${file}" \
    ~/vc-update/release/"${file/VoiceCut/VoiceCut-0.2.1-arm64}"
done
python3 desktop/scripts/updater_manifest.py ~/vc-update/release --version 0.2.1 \
  --base-url http://127.0.0.1:8000
python3 -m http.server 8000 --directory ~/vc-update/release &
# Apps opened by the Finder, and VoiceCut's restart, get launchd's environment.
launchctl setenv VOICECUT_UPDATE_ENDPOINT http://127.0.0.1:8000/latest.json
launchctl setenv VOICECUT_RUNTIME "$PWD"
launchctl setenv VOICECUT_DATA_DIR ~/vc-update/data
open ~/vc-update/VoiceCut.app
# …and afterwards:
launchctl unsetenv VOICECUT_UPDATE_ENDPOINT; launchctl unsetenv VOICECUT_RUNTIME
launchctl unsetenv VOICECUT_DATA_DIR
```

## API keys

Keys live in the system's credential store, never in documents or the
repository's `.env`: the login Keychain on macOS (service "VoiceCut", read
with `/usr/bin/security`), the Windows Credential Manager, and on Linux the
Secret Service (GNOME Keyring, KWallet, KeePassXC) through `keyring`. A Linux
session without one falls back to `editor/keys.env` in the data directory,
readable only by you, and Settings says so. The engine hands the chosen
planner's key to a Voice Cut run in its environment. A key in the app's own
environment (`GEMINI_API_KEY=… voicecut`) takes precedence, as it does for
the command line.

## Development

`VOICECUT_RUNTIME=/path/to/voicecut` runs the app against a checkout set up
with `scripts/install.sh` instead of the installed runtime (no setup;
`VOICECUT_DATA_DIR` moves the app's own data elsewhere too):

```bash
VOICECUT_RUNTIME="$PWD" VOICECUT_DATA_DIR=/tmp/voicecut-dev \
  desktop/src-tauri/target/universal-apple-darwin/release/bundle/macos/VoiceCut.app/Contents/MacOS/voicecut-desktop
```

Built with the cargo feature `automation` (`VOICECUT_TEST_BUILD=1
desktop/scripts/build-macos.sh`, or `npx tauri build --features automation`)
and started with `VOICECUT_AUTOMATION_PORT=<port>`, the app lets a test run
scripts in its pages, choose menu items and answer its dialogs over that
loopback port (`src-tauri/src/automation.rs`). Release builds do not contain
it. `tests/e2e.py` uses it to go from an empty data directory to a working
project:

```bash
python3 desktop/tests/e2e.py --app path/to/the/test/build   # --full runs Voice Cut too
```

## Building

Everything needs Rust (`rustup`), Node.js 20+ and Python 3.

* **macOS**: also uv, resvg (`cargo install resvg`) and the Xcode command
  line tools. `desktop/scripts/build-macos.sh` writes
  `desktop/build/VoiceCut-<version>-universal.dmg`, one app for Apple Silicon
  and Intel Macs (it adds the `aarch64-apple-darwin` and
  `x86_64-apple-darwin` Rust targets).
* **Linux** (build on Ubuntu 22.04 or Debian 12 to reach the most systems):
  the WebKitGTK, GStreamer and other packages listed in
  `.github/actions/prepare-desktop/action.yml`. **Windows**: nothing more
  (Tauri fetches NSIS). Then:

  ```bash
  npm ci --prefix frontend && npm run build --prefix frontend
  python3 desktop/scripts/stage_runtime.py
  npm ci --prefix desktop && (cd desktop && npx tauri build)
  ```

  which writes `src-tauri/target/release/bundle/{appimage,deb}` or
  `bundle/nsis` (`tauri.linux.conf.json` and `tauri.windows.conf.json` hold
  what differs from macOS).

The locks are refreshed with `desktop/scripts/lock-runtime.sh` (see its
header); it solves the Intel Mac, Linux and Windows locks from a Mac.

## Continuous integration and releases

* `.github/workflows/ci.yml` — Python tests on macOS (Apple Silicon and
  Intel), Linux and Windows, each installed from the locks the app installs;
  the editor frontend's checks.
* `.github/workflows/desktop.yml` — Clippy and the Rust tests on all three
  systems, then the installers as the run's artifacts (with the updater's
  signatures when the `TAURI_SIGNING_PRIVATE_KEY` secret is set); on Linux
  the `.deb` is installed and its menu entry and file type checked.
* `.github/workflows/e2e.yml` — a test build installed like a user would (the
  AppImage under Xvfb, the Windows installer silently, the app on an Intel
  Mac runner, since GitHub's Apple Silicon runners have no GPU for MLX), set
  up from nothing, Whisper and MFA run in the engine it installed, a project
  created, closed, reopened from its launcher and handed over by a second
  VoiceCut, then quit. Started by hand with "full" and a `GEMINI_API_KEY`
  secret, it runs the whole Voice Cut too.
* `.github/workflows/release.yml` — on a `v<version>` tag or by hand: builds
  all installers and attaches them to a **draft** GitHub Release with
  `RELEASE_NOTES.md`, plus `latest.json` and the macOS `.app.tar.gz` for the
  apps' own updates (see "Updates"). Nothing is published until someone
  presses Publish.

A failing step reports the end of its output as an annotation on the run.

## Signing and notarization

Without certificates the macOS app is signed ad hoc and the Windows
installer is unsigned. Both run, after a warning on the first launch of a
downloaded copy: macOS — "“VoiceCut” can’t be opened because Apple cannot
check it for malicious software", then System Settings › Privacy & Security ›
"Open Anyway" (or `xattr -dr com.apple.quarantine /Applications/VoiceCut.app`);
Windows — SmartScreen, "More info" › "Run anyway". Linux has no such step.

For macOS:

1. Join the Apple Developer Program ($99/year).
2. In Xcode › Settings › Accounts (or developer.apple.com › Certificates),
   create a **Developer ID Application** certificate; it lands in your login
   keychain. `security find-identity -v -p codesigning` shows its name.
3. Create an App Store Connect API key (Users and Access › Integrations ›
   Team Keys, role Developer) and download the `.p8` file — or use your Apple
   ID with an app-specific password from account.apple.com.
4. Build with:

   ```bash
   APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
   APPLE_API_ISSUER=<issuer id> APPLE_API_KEY=<key id> \
   APPLE_API_KEY_PATH=/path/to/AuthKey_<key id>.p8 \
   desktop/scripts/build-macos.sh
   ```

   (or `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` instead of the API key).
   `tauri build` signs the app with the hardened runtime and a secure
   timestamp and has Apple notarize and staple it; the script then signs,
   notarizes and staples the disk image and checks it with `spctl`.

   On GitHub the same comes from the repository secrets `APPLE_CERTIFICATE`
   (the certificate exported as `.p12`, base64), `APPLE_CERTIFICATE_PASSWORD`,
   `APPLE_SIGNING_IDENTITY`, `APPLE_API_ISSUER`, `APPLE_API_KEY` and
   `APPLE_API_KEY_CONTENT` (the `.p8` file's text).

A notarized VoiceCut opens with the usual one-time "downloaded from the
Internet" confirmation. What it installs later (Python, MFA, FFmpeg) is
downloaded by the app itself, not by a browser, so it carries no quarantine
flag and needs no notarization of its own. With Developer ID signing the
Keychain also recognizes every later version as the same app.

Windows code signing (an OV or EV certificate, or Azure Trusted Signing)
plugs into `bundle.windows.signCommand` in `tauri.windows.conf.json`; until
then SmartScreen warns until the installer has built up a reputation.
