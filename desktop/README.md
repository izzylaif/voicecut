# VoiceCut for macOS

The VoiceCut editor as a Mac app: a small download, a one-time setup of
VoiceCut's engine, then projects as `.vc` documents you open from the Finder.
Apple Silicon (macOS 14 or later) for now; the design keeps Linux and Windows
in reach (see the end).

```
VoiceCut.app (Tauri, ~10 MB)                ~/Library/Application Support/VoiceCut
┌──────────────────────────────┐            ┌────────────────────────────────────────┐
│ menus, windows, dialogs,     │  spawns    │ runtime/  (a VoiceCut checkout's layout)│
│ Dock, Finder, first-run setup├───────────►│   .venv/bin/voicecut-editor --desktop  │
│                              │            │     └─ worker ─ pipeline ─ MLX Whisper │
│ WKWebView ── the editor ─────┼─ HTTP ────►│   127.0.0.1:<port>          └─ MFA     │
└──────────────────────────────┘            └────────────────────────────────────────┘
```

* `src-tauri/` — the app (Rust): `setup.rs` installs the engine, `engine.rs`
  runs it, `lib.rs` has windows, menus, documents and quitting, `commands.rs`
  what the pages may call.
* `ui/` — the app's own pages: first-run setup, starting, errors.
* `runtime/` — what setup installs, pinned: tool versions and checksums
  (`runtime.json`), hashed Python locks, the explicit conda lock of MFA.
* `icons/` — the app icon, the `.vc` document icon and the disk-image
  background, drawn by hand as SVG; `scripts/make-icons.sh` renders them.
* `scripts/build-macos.sh` — builds `VoiceCut.app` and the `.dmg`.

The editor itself is the same code as `voicecut-editor` in a browser
(`src/voicecut/editor`, `frontend/`); `frontend/src/desktop.ts` is the only
part that knows about the app.

## Why Tauri

| | Tauri (WKWebView) | Electron (Chromium) | pywebview |
|---|---|---|---|
| Download | ~4 MB `.dmg` | ~100 MB `.dmg` | ~30 MB (it must ship a Python to run setup) |
| Engine | the system WebKit | its own Chromium | the system WebKit |
| Menus, documents, Dock | native, `RunEvent::Opened` for Finder files | native | partial |
| Linux / Windows | WebKitGTK / WebView2 | same Chromium everywhere | GTK/Qt / WebView2 |

The engine (Python, PyTorch, MLX, MFA) is several GB whatever the shell, so
the shell is chosen for the download, memory and native integration: Tauri.
Its price is WebKit, while the editor had only been tested in Chrome; every
editor feature was checked in the app's WKWebView (audio playback at the
recording's sample rate, the 0.5×–2.5× pitch-preserving stretch, the video
proxy and its sync, the canvas timeline with trackpad pinch, the captions).
WebKit needed: native sheets instead of `window.confirm` (WKWebView has no
dialogs of its own), the system browser for external links, Safari's pinch
gesture events on the timeline, and native file panels for paths.

## What the app installs

On first launch the app shows what it will install and asks to go ahead.
Setup reuses `scripts/install.sh` with everything pinned and verified:

1. uv and micromamba, checked against their SHA-256;
2. CPython 3.12 and 3.11 through uv;
3. VoiceCut's source and built editor, from the app bundle;
4. `.venv` (editor, pipeline, PyTorch, WhisperX, planners) from
   `requirements-main.txt` with `--require-hashes`;
5. `.venv-mlx` (MLX Whisper, local planners) from `requirements-mlx.txt`;
6. `.mfa-env` (MFA 3.4.1, FFmpeg) from the explicit conda lock, plus a
   one-line fix of MFA 3.4.1, which quotes tool paths containing a space and
   so cannot run from "Application Support";
7. Respiro-en (breath detection), checked against its SHA-256;
8. a check that every part starts, and removal of the downloaded archives.

About 1.6 GB is downloaded and 4.3 GB stays on disk. Each step records a
fingerprint of its pins in `setup.json`: a failed or cancelled setup resumes
at the step that did not finish ("Try Again"), and a new app version updates
only what changed (a new VoiceCut source takes seconds). Model weights
download on first use into `~/.cache/huggingface`, as with the command line.
Logs are in `~/Library/Application Support/VoiceCut/logs`.

To start over, quit VoiceCut and delete `~/Library/Application Support/VoiceCut`.
To uninstall, also delete `VoiceCut.app` and the "VoiceCut" items in Keychain
Access.

## Documents

A project is a `.vc` document: a folder the Finder shows as one file (a
document package, declared in `src-tauri/Info.plist`), holding the editor
project — `project.json`, the canonical WAV, peaks, video proxy, the
timeline, exports — and the pipeline's work directory with all its stages.
The editor works on it in place: every edit is saved as you make it, so
File > Save only confirms it, and reopening a document shows the finished
Voice Cut edit without running anything.

* The source recording is referenced, not copied (a video can be many GB,
  and the document already holds the audio the editor needs). If it moves,
  VoiceCut looks for it beside the document and checks its SHA-256;
  otherwise the editor asks you to locate it. Editing and audio export work
  without it; a new Voice Cut run and video export need it.
* Moved or copied documents rebase their own paths when opened; Save As
  copies a document (instantly on APFS) and keeps editing the copy.
* A project of the browser editor (`.voicecut-cache/editor/<id>`) becomes a
  document by copying its folder to `<name>.vc`; opening it brings its Voice
  Cut run inside.

`src/voicecut/editor/documents.py` has the details. On Linux and Windows a
document is an ordinary folder.

## Privacy prompts

The engine runs as VoiceCut, so macOS asks once, the first time VoiceCut
reads a recording or writes a project or export in the Desktop, Documents or
Downloads folder, on a removable or a network volume: "“VoiceCut” would like
to access files in your Desktop folder." Until the question is answered, the
file stays closed and the editor waits. The answer can be changed in System
Settings › Privacy & Security › Files and Folders. An ad-hoc signed build is a
new app to macOS after every rebuild and asks again; a Developer ID build
keeps the answer across updates.

## API keys

Keys are generic passwords in the login Keychain (service "VoiceCut"), never
in documents or the repository's `.env`. The engine reads them with
`/usr/bin/security` and hands the chosen planner's key to a Voice Cut run in
its environment. A key in the app's own environment (for example
`GEMINI_API_KEY=… open VoiceCut.app` from a terminal) takes precedence, as it
does for the command line.

## Development

`VOICECUT_RUNTIME=/path/to/voicecut` runs the app against a checkout set up
with `scripts/install.sh` instead of the installed runtime (no setup;
`VOICECUT_DATA_DIR` moves the app's own data elsewhere too):

```bash
VOICECUT_RUNTIME="$PWD" VOICECUT_DATA_DIR=/tmp/voicecut-dev \
  desktop/src-tauri/target/release/bundle/macos/VoiceCut.app/Contents/MacOS/voicecut-desktop
```

`VOICECUT_TEST_BUILD=1 desktop/scripts/build-macos.sh` builds the end-to-end
test variant: started with `VOICECUT_AUTOMATION_PORT=<port>`, it lets a test
run scripts in its pages, choose menu items and answer its dialogs over that
loopback port (`src-tauri/src/automation.rs`). Release builds do not contain
it.

## Building

Needs Rust (`rustup`), Node.js 20+, uv, resvg (`cargo install resvg`) and the
Xcode command line tools.

```bash
desktop/scripts/build-macos.sh
```

writes `desktop/build/VoiceCut-<version>-arm64.dmg`. The locks are refreshed
with `desktop/scripts/lock-runtime.sh` (see its header).

## Signing and notarization

Without an Apple Developer account the app is signed ad hoc. It runs on the
Mac that built it. Downloaded on another Mac, macOS quarantines it and
Gatekeeper refuses to open it: "“VoiceCut” can’t be opened because Apple
cannot check it for malicious software" (or, on recent macOS, "…Not Opened").
The user can still open it with System Settings › Privacy & Security ›
"Open Anyway" after the first attempt, or with
`xattr -dr com.apple.quarantine /Applications/VoiceCut.app`, but a public
release should be notarized:

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

A notarized VoiceCut opens with the usual one-time "downloaded from the
Internet" confirmation. What it installs later (Python, MFA, FFmpeg) is
downloaded by the app itself, not by a browser, so it carries no quarantine
flag and needs no notarization of its own. With Developer ID signing the
Keychain also recognizes every later version as the same app.

## Linux and Windows

The shell, setup and documents are written to carry over: paths come from
`paths.rs` (XDG data dir, `%LOCALAPPDATA%`), venv layouts differ only in
`bin`/`Scripts`, documents are plain folders, uv and micromamba exist on all
three systems, and Tauri brings WebKitGTK and WebView2. What has to change is
VoiceCut itself — a Whisper that is not MLX (such as faster-whisper) and
platform locks — plus `KeychainKeys` (Secret Service, Windows Credential
Manager) and a file association for a folder format (for example a small
`.vc` launcher file inside the document).
