VoiceCut listens to a narration recording, keeps the best take of every line,
fixes the pauses, and lets you fine-tune every cut on a timeline before
exporting.

## What's new in 0.3.0

- **The planner can run on your computer.** Local models run through Ollama
  on macOS, Windows and Linux, and VoiceCut installs Ollama unless you have
  it. New projects plan with Gemma 4 31B on your computer where it runs well
  (Apple Silicon with 32 GB of memory or more, or an NVIDIA GPU with 24 GB),
  and with the same model through OpenRouter elsewhere. The first Voice Cut
  offers to download the model (about 20 GB, once) and starts when it is
  there.
- **OpenRouter** joins Gemini, OpenAI and DeepSeek. Gemma 4 31B there edits
  nearly the way Gemini 3.1 Pro does, for about 5 cents of OpenRouter's
  charges per hour of recording. Gemini now defaults to Gemini 3.8 Flash.
- **Voice Cut edits only what is left on the timeline.** Trim a recording
  first and only the parts you kept are transcribed and planned; delete part
  of Voice Cut's result and the next run leaves it out too.
- **Intel Macs:** the macOS download is now universal.
- **Windows:** Voice Cut no longer hangs at "Checking runtimes", and setup
  works when Windows long paths are off.
- The run's log opens at a readable size and says whether Whisper runs on
  the GPU.

## Install

| System | File | |
| --- | --- | --- |
| macOS 14+, Apple Silicon or Intel | `VoiceCut-<version>-universal.dmg` | Open it and drag VoiceCut to Applications. |
| Linux x86_64 (glibc 2.35+: Ubuntu 22.04, Debian 12, Fedora 36 or newer) | `VoiceCut-<version>-x86_64.AppImage` | `chmod +x` it and run it. It adds itself to your applications. |
| Debian, Ubuntu | `VoiceCut-<version>-amd64.deb` | `sudo apt install ./VoiceCut-<version>-amd64.deb` |
| Windows 10/11 x64 | `VoiceCut-<version>-x64-setup.exe` | Run it; it installs for you alone, no administrator needed. |

The AppImage needs FUSE 2: if it does not start on Ubuntu, install it with
`sudo apt install libfuse2` (`libfuse2t64` on Ubuntu 24.04 and later).

On first launch VoiceCut downloads and sets up its engine (about 1.7 GB on a
Mac and 2.9 GB on Windows and Linux; keep up to 9 GB free while it installs).
The planner runs on your computer, or with your own API key for a cloud
provider (OpenRouter, Gemini, OpenAI, DeepSeek), which VoiceCut keeps in the
system's keychain or credential store.

Whisper runs on the GPU on Apple Silicon (MLX) and on NVIDIA GPUs with a
recent driver (CUDA 12); anywhere else it runs on the CPU, about as fast as
the recording plays.

### Updates

VoiceCut updates itself: when a new version is out, a download button appears
in its toolbar, and unless you turn it off in Settings the update installs
when you quit VoiceCut. `latest.json` and the `.app.tar.gz` are what it
downloads; you don't need them. On Debian and Ubuntu the update asks for
your password.

### Unsigned builds

Unless this release says it is signed, the installers are not signed with a
paid certificate yet:

- **macOS** says VoiceCut "cannot be opened because Apple cannot check it for
  malicious software". Open System Settings → Privacy & Security and choose
  **Open Anyway** (once).
- **Windows** SmartScreen says it "protected your PC". Choose **More info** →
  **Run anyway**.
