VoiceCut listens to a narration recording, keeps the best take of every line,
fixes the pauses, and lets you fine-tune every cut on a timeline before
exporting.

## Install

| System | File | |
| --- | --- | --- |
| macOS 14+ on Apple Silicon | `VoiceCut-<version>-arm64.dmg` | Open it and drag VoiceCut to Applications. |
| Linux x86_64 (glibc 2.35+: Ubuntu 22.04, Debian 12, Fedora 36 or newer) | `VoiceCut-<version>-x86_64.AppImage` | `chmod +x` it and run it. It adds itself to your applications. |
| Debian, Ubuntu | `VoiceCut-<version>-amd64.deb` | `sudo apt install ./VoiceCut-<version>-amd64.deb` |
| Windows 10/11 x64 | `VoiceCut-<version>-x64-setup.exe` | Run it; it installs for you alone, no administrator needed. |

The AppImage needs FUSE 2: if it does not start on Ubuntu, install it with
`sudo apt install libfuse2` (`libfuse2t64` on Ubuntu 24.04 and later).

On first launch VoiceCut downloads and sets up its engine (about 1.5 GB, up
to 7 GB of disk while installing). Voice Cut itself needs a Gemini API key,
which VoiceCut keeps in the system's keychain or credential store.

Whisper runs on the GPU on Apple Silicon (MLX) and on NVIDIA GPUs with a
recent driver (CUDA 12); anywhere else it runs on the CPU, about as fast as
the recording plays.

### Unsigned builds

Unless this release says it is signed, the installers are not signed with a
paid certificate yet:

- **macOS** says VoiceCut "cannot be opened because Apple cannot check it for
  malicious software". Open System Settings → Privacy & Security and choose
  **Open Anyway** (once).
- **Windows** SmartScreen says it "protected your PC". Choose **More info** →
  **Run anyway**.
