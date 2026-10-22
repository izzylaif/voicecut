# Development, command line and building from source

The [VoiceCut app](../README.md#install) is the easiest way to use VoiceCut.
This guide is for everything else: running VoiceCut from a source checkout,
the `voicecut` command line, the editor in a browser, the tests, and building
the desktop app.

## Repository layout

| Path | What it is |
| --- | --- |
| `src/voicecut/` | The pipeline: transcription, gap recovery, take planner, MFA alignment, pauses, the renderer, video publication, and the `voicecut` command |
| `src/voicecut/editor/` | The editor's engine: projects and `.vc` documents, the Voice Cut worker, the preview and export renderer, the HTTP API (`voicecut-editor`) |
| `frontend/` | The editor's interface (React, TypeScript, a canvas timeline) |
| `desktop/` | The desktop app (Tauri 2): first-run setup, windows, menus, documents, updates, installers — see [desktop/README.md](../desktop/README.md) |
| `tests/` | Python tests with small fixtures and mocked model responses |
| `examples/` | The public example recordings and how they were made |
| `docs/` | These guides; `docs/demo/` is the website |

## Run from a source checkout

A checkout runs on Apple Silicon Macs with Python 3.11 or 3.12 and Homebrew
(the installer uses it for FFmpeg, micromamba and Node.js). On Linux and
Windows, use the app, which installs the same engine from pinned locks.

```bash
git clone https://github.com/vectozavr/voicecut.git
cd voicecut
./scripts/install.sh
source .venv/bin/activate
```

The installer sets up FFmpeg, MLX Whisper, Montreal Forced Aligner 3.4.1,
the editor, and the three isolated runtimes VoiceCut uses (`.venv`,
`.venv-mlx`, `.mfa-env`). It is safe to run again. The
[installation guide](installation.md) covers first-run model downloads,
manual setup and updating a checkout.

## The command line

The pipeline runs without the editor. Add the planner key to `.env` in the
checkout (or export it in your shell):

```dotenv
GEMINI_API_KEY=your_key_here
```

Then run VoiceCut with only the input file:

```bash
voicecut recording.wav
```

The result is written beside the input as `recording_edited.wav`. VoiceCut
keeps a supported input container by default and reads common formats through
FFmpeg, including WAV, MP3, M4A, FLAC, MOV and MP4. Uncommon audio containers
default to WAV; recognized video inputs default to MP4 when needed. An
existing output is never overwritten unless `--overwrite` is given.

```bash
# Choose the output path.
voicecut recording.mp3 -o final.mp3

# Edit Russian narration.
voicecut recording_ru.wav --language ru

# Edit video from its speech track.
voicecut presentation.mp4 -o presentation_cut.mp4

# Keep reusable diagnostics and stage metadata.
voicecut recording.wav --work-dir voicecut_work --debug-artifacts

# Keep breaths (on by default for English, off for Russian).
voicecut recording.wav --breath-cleanup off
```

Choose the planner and its model:

```bash
voicecut recording.wav --planner-backend openai
voicecut recording.wav --planner-backend gemini --planner-model gemini-3.8-flash
voicecut recording.wav --planner-backend ollama --planner-model gemma4:31b-it-qat

# Send Gemini the transcript only, without the audio of each window.
voicecut recording.wav --planner-audio off
```

| Backend | Default model | Credential |
| --- | --- | --- |
| `gemini` | `gemini-3.1-pro-preview` | `GEMINI_API_KEY` |
| `openai` | `gpt-5-mini` | `OPENAI_API_KEY` |
| `deepseek` | `deepseek-v4-flash` | `DEEPSEEK_API_KEY` |
| `ollama` | `gemma4:31b-it-qat` (`gemma4:31b-mlx` on Apple Silicon with more than 32 GB) | none: a model downloaded into [Ollama](https://ollama.com) |

`ollama` runs the planner on this computer through an installed or running
Ollama (`ollama pull <model>` first). [Configuration](configuration.md)
documents every backend, environment variable and advanced option;
`voicecut --help` is the authoritative list.

## The editor in a browser

The app's editor is the same code as `voicecut-editor`, which serves it on
your machine:

```bash
voicecut-editor                 # opens http://127.0.0.1:8765 in your browser
voicecut-editor recording.wav   # or open a recording right away
```

In a browser, API keys are checked with the provider and saved to the
checkout's `.env` (the file the `voicecut` command reads), and projects live
in `.voicecut-cache/editor/`. The browser editor is developed and tested in
Chromium-based browsers (Chrome, Edge, Arc, Brave). The
[editor guide](editor.md) describes every feature and shortcut.

## Tests and checks

```bash
.venv/bin/python -m pytest -q
ruff check src tests
ruff format --check src tests
```

The editor's frontend:

```bash
cd frontend
npm run typecheck
npm test
npm run build
```

The tests use small fixtures and mocked model responses; they do not download
MFA or Respiro-en. [CONTRIBUTING.md](../CONTRIBUTING.md) has focused test
commands and the contribution scope.

## The desktop app

[desktop/README.md](../desktop/README.md) explains how the app works and how
to build it:

- **Development** — run the app against your checkout
  (`VOICECUT_RUNTIME="$PWD"`), the test build with its automation port, and
  the end-to-end test `desktop/tests/e2e.py`;
- **Building** — `desktop/scripts/build-macos.sh` for the `.dmg`, and
  `npx tauri build` for the AppImage, `.deb` and Windows installer;
- **Updates**, **Continuous integration and releases**, and **Signing and
  notarization**.

## The website

`docs/demo/` is the website at <https://vectozavr.github.io/voicecut/>.
`.github/workflows/pages.yml` publishes it on every change to `main` and
once a day; to see it locally:

```bash
python3 scripts/build_pages_demo.py
python3 -m http.server 8000 --directory .pages-site
```

The download buttons find the latest release's installers in the visitor's
browser through the GitHub API, by the ends of their names (`-universal.dmg`,
`-x64-setup.exe`, `-x86_64.AppImage`, `-amd64.deb`). When the API cannot be
reached they use `release.json`, the copy the build writes, and else link
to the release page, so a new version needs no change to the site.
