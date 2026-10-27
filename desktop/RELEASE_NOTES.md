VoiceCut listens to a narration recording, keeps the best take of every line,
fixes the pauses, and lets you fine-tune every cut on a timeline before
exporting.

## What's new in 0.4.3

- **Pauses are back when breaths are removed.** In a tightly spoken
  recording, with a breath in every pause, removing the breaths could leave
  almost no pauses at all (0.4.2 did this to Russian recordings). Every pause
  now keeps its length: what the recording's clean quiet cannot fill gets
  room tone from short quiet stretches of the same recording, or silence when
  it has none. In a video, such a pause keeps its recorded sound instead.

## New in 0.4.2

- **Breaths removed in Russian too.** Voice Cut now replaces breaths, clicks
  and mouth noise in pauses with room tone in every language; Russian
  recordings used to keep them. Settings › Voice Cut has a *Remove breaths*
  checkbox, ticked by default: untick it to keep every pause as recorded. A
  change applies the next time you run Voice Cut.

## New in 0.4.1

- **Sound after a long Voice Cut.** On a Mac, the edit of a long recording
  could play silent after Voice Cut finished until VoiceCut was restarted.
  The player now rests its audio output while nothing plays and wakes it to
  play, which brings the sound back.

## New in 0.4.0

- **Fast planners for smaller Macs.** Kev 9B and Kev 4B, decision models that
  answer typed questions about every line instead of writing text, plan an
  hour of narration in about 10–20 minutes on Apple Silicon from 12 GB of
  memory, a little less precisely than Gemma 4. New projects pick the best
  planner your computer runs well: Gemma 4 31B, else Kev 9B, else Kev 4B,
  else Gemma 4 through OpenRouter. TypeSafe Jev, a hosted decision model, is
  in the OpenRouter list.
- **See how long it will take.** Voice Cut shows how far each stage has got
  and the time left, learned from how fast your own computer ran before.
- **Try another model without waiting again.** Running Voice Cut again with
  another planner reuses the transcription: only the take selection and the
  render run again, and going back to a model you used is instant.
- **Your own pause lengths.** Settings › Pauses sets how long Voice Cut's
  pauses are — inside a sentence, after a comma, between sentences and
  paragraphs — for every project at once. Pauses you resized keep their length.
- The top bar fits narrow windows: it hides its text labels instead of
  letting the controls overlap. The language is now chosen as `en` or `ru`.
- The “Back to Voice Cut's edit” button is gone: running Voice Cut again
  with the same settings brings its edit back at once.

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
