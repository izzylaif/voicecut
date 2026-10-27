# Troubleshooting

This guide covers the production `voicecut` command installed by
[`scripts/install.sh`](../scripts/install.sh). Start with the short checklist,
then use the section that matches the stage named in the error.

For supported options, see [Configuration](configuration.md). For the artifact
flow and safety policy, see [Architecture](architecture.md).

## Quick checklist

From the repository root:

```bash
source .venv/bin/activate
voicecut --help
ffmpeg -version
ffprobe -version
micromamba --version
```

Verify the pinned MFA environment:

```bash
MFA_ROOT_DIR="$PWD/.voicecut-cache/runtime/mfa" \
HF_HOME="$PWD/.voicecut-cache/runtime/mfa/huggingface" \
  micromamba run -p "$PWD/.mfa-env" mfa version
```

The final command must report MFA `3.4.1`.

For a diagnosable run, use a dedicated work directory:

```bash
voicecut input.wav \
  -o input_edited.wav \
  --work-dir .voicecut-cache/input-debug
```

Do not manually edit files inside that directory.

## `voicecut: command not found`

Activate the primary environment from the repository root:

```bash
source .venv/bin/activate
voicecut --help
```

If `.venv/bin/voicecut` does not exist, rerun:

```bash
./scripts/install.sh
source .venv/bin/activate
```

Do not invoke a guessed `.venv-audio/bin/voicecut` path. The supported layout
uses `.venv` for the public CLI and `.venv-mlx` as an internal helper.

## Missing Python modules

Errors such as `No module named voicecut`, `silero_vad`, `mlx_whisper`, or
`whisperx` usually mean the wrong interpreter was used or installation was
partial.

The public command should run from `.venv`; VoiceCut launches the configured
MLX and alignment interpreters automatically. Repair the complete layout with:

```bash
./scripts/install.sh
source .venv/bin/activate
```

If you intentionally override `--asr-python`, confirm that the selected interpreter contains the required
package. Remove the override before diagnosing the standard installation.

## Unsupported machine or Python version

The installer currently requires Apple Silicon macOS and Python 3.11 or 3.12.
On a supported Mac, point the installer at a compatible Homebrew Python:

```bash
brew install python@3.12
VOICECUT_PYTHON=/opt/homebrew/bin/python3.12 ./scripts/install.sh
```

If a repository-local environment was built with another Python version,
remove only the affected `.venv` or `.venv-mlx` directory and rerun the
installer. Do not delete an unrelated system or user environment.

## Planner preflight failures

VoiceCut validates planner credentials and SDK availability before media work.
This prevents a long transcription from finishing only to discover a missing
API key.

### Missing API key

Add the selected provider key to `.env` or export it:

```dotenv
GEMINI_API_KEY=...
OPENAI_API_KEY=...
DEEPSEEK_API_KEY=...
```

Confirm that `--env-file` points to the intended file and that the variable
matches `--planner-api-key-env` when using a custom name.

### Cloud SDK unavailable

Reinstall the cloud extra in the primary environment:

```bash
source .venv/bin/activate
python -m pip install -e ".[audio,cloud]"
```

### Invalid custom endpoint

`--planner-base-url` is accepted only for OpenAI and DeepSeek. The URL must be
HTTP(S), include a host, and contain no credentials, query, or fragment.
OpenAI overrides must implement the Responses API.

Never embed a key in the URL. VoiceCut rejects credential-bearing URLs and
redacts validated endpoints from subprocess error output.

### Local planner does not start

The `ollama` planner needs [Ollama](https://ollama.com/download): running
(`ollama serve`, or the Ollama app) or installed, in which case VoiceCut
starts it for the run. `OLLAMA_HOST` selects another server. The model must be
downloaded first (`ollama pull gemma4:31b-it-qat`, or Download in the editor's model
list); the error names it. "requires more system memory" means the model does
not fit: choose a smaller one. In the desktop app, Ollama's log is
`logs/ollama.log` in the data directory.

A local run that seems stuck is usually reading a long prompt or reasoning:
the log reports its progress every 30 seconds. When diagnosing edit quality,
first compare with a strong cloud planner in a separate work directory.

## MFA runtime problems

### `.mfa-env` missing or wrong version

Check the isolated runtime:

```bash
MFA_ROOT_DIR="$PWD/.voicecut-cache/runtime/mfa" \
HF_HOME="$PWD/.voicecut-cache/runtime/mfa/huggingface" \
  micromamba run -p "$PWD/.mfa-env" mfa version
```

If the prefix is missing or does not report `3.4.1`, remove only the
repository-local `.mfa-env` and rerun `./scripts/install.sh`. Do not install MFA
into either Python virtual environment.

### First alignment is slow or downloads a model

The first English run downloads `english_us_arpa`; the first Russian run
downloads the revision-pinned Russian MFA bundle. This is expected. Later runs
reuse the model cache selected by `--mfa-cache-root`.

Changing the cache root causes a separate model download. Ensure the machine
has network access and enough disk space. Once the model is cached, VoiceCut
runs MFA with `HF_HUB_OFFLINE=1`: on slow or filtered networks MFA's Hugging
Face checks could otherwise stall each run for many minutes.

### `mfa_word_mapping_failed` or alignment failure

MFA aligns every kept transcript line. A word it cannot map (an unusual token,
a mis-heard acronym) keeps its Whisper timing for that word only; the waveform
analysis still decides where its sound starts and stops, so a mapping failure
does not clip the word. If MFA cannot run at all, the whole render falls back
to Whisper timing and the run prints a `whisper_timing_fallback` warning. Check
`05_final/mfa_alignment/metadata/` and `output/`, and the `alignment` record in
`05_final/final_boundary_plan.json`.

## Russian-language runs

Russian must be selected explicitly:

```bash
voicecut input_ru.wav --language ru
```

Expected differences from English:

- hidden-retry CTC is reported as `skipped_unsupported_language`;
- the revision-pinned Russian MFA model may download on first use;
- breath cleanup is on as in English, although Respiro-en is not validated for
  Russian; `--breath-cleanup off` keeps the breaths if it misbehaves.

The CTC skip is informational, not an error. Do not force the English CTC model
onto a Russian transcript.

If Cyrillic narration was accidentally processed as English, use a new work
directory with `--language ru`. One explicit work directory cannot be reused
with a different language configuration.

## Respiro-en and breath cleanup

### Pinned runtime missing or hash mismatch

Run `./scripts/install.sh`. The installer downloads the exact pinned
`modules.py`, checkpoint, and license and checks their SHA-256 hashes. VoiceCut
does not download from a moving branch during execution.

Do not replace the files manually. Provenance is documented in
[the Respiro-en upstream record](upstream/respiro-en.md).

### Detector failure warning

When the breath detector cannot load or run, VoiceCut keeps cleaning pauses by
waveform energy alone and records `detector_failed_energy_only` as the
`breath_cleanup_status` in `05_final/final_render_manifest.json`. A breath that
touches a word without any silence in between may then stay attached to it.

### Breath remains in the result

With `--breath-cleanup replace`, everything audible inside a pause other than a
word's own head and tail is replaced by ambience. A breath can only remain when
it is glued to a word -- no 20 ms of silence between them and below the
Respiro-en threshold. Lower `--breath-threshold` (for example `0.35`) to treat
weaker detections as breath, and inspect the join in
`05_final/final_boundary_plan.json` (`left_tail`, `right_head`).

### No clean ambience

If the recording has no stretch of at least 250 ms of clean room tone (tight
narration with a breath in every pause), room tone is taken from shorter clean
stretches of at least 80 ms (`ambience.status` = `short_runs` in the boundary
plan). If there are none either (`unavailable`), the rest of each pause is
silence (`silent_room_tone` on its join), so pauses still get their length. In
a video, such a pause keeps its recorded gap, breath included
(`kept_recorded_gap`).

## Output already exists

VoiceCut does not replace an existing destination by default:

```text
... already exists; pass --overwrite to replace it
```

Either choose a new destination or explicitly allow replacement:

```bash
voicecut input.wav -o output.wav --overwrite
```

Input and output must differ. A symbolic-link output is refused, and input or
output paths may not overlap the work-directory tree.

## Cache and work-directory errors

### Work directory belongs to another configuration

An explicit work directory is immutable with respect to input and production
configuration. Use a different directory when changing the source, language,
planner/model, alignment settings, or renderer policy:

```bash
voicecut input.wav \
  --work-dir .voicecut-cache/input-new-model \
  --planner-model NEW_MODEL
```

Do not modify `pipeline_config.json` to force reuse. It protects against stale
semantic plans and renders.

### Another process owns the cache

VoiceCut uses a non-blocking lock per work directory. Wait for the owner to
finish or select another directory. Do not run two processes against the same
cache.

If no process remains after an abnormal termination, a new process can acquire
the operating-system lock; the presence of the lock file itself is not the
lock.

### Resume an interrupted long recording

Rerun the exact same command with the same work directory. VoiceCut validates
and reuses completed stages. Whisper and the optional English CTC worker retain
validated per-region checkpoints, so the run can continue without starting at
the first region.

If configuration or implementation changed, use the new content-addressed
default or select a fresh work directory. A software fingerprint deliberately
prevents old output from being treated as current.

### Output appears unchanged

Check `pipeline_run.json`: `cuts` and `pauses_normalized` count the edits, and
`pipeline_warnings` lists anything unusual. `04_semantic_plan/take_plan.json`
holds the reconstructed narration; compare it with what you expected.

## Long recordings and planner failures

The planner decides about four minutes per request. Each request is cached in
`04_semantic_plan/calls/` under the exact prompt and audio, so rerunning the
same command after an interruption or an API outage resumes at the first
unfinished window. Transient API errors are retried with backoff; an answer
that fails validation is retried with the error message, up to three attempts,
after which the run stops with an error that names the window.

Relevant artifacts:

- `04_semantic_plan/take_plan.json`: kept items, `windows` (what each request
  kept, revoked and deferred), `review` (regions re-decided after the duplicate
  check) and `remaining_duplicate_suspects`;
- `04_semantic_plan/calls/*.json`: prompts and raw answers.

## Awkward or clipped-sounding cuts

Find the join in `05_final/final_boundary_plan.json` (`joins`, in output
order). Each records the two words, whether they were contiguous in the
source, the pause class, the original and desired gap, how far the left word's
tail and the right word's head were extended, and whether the boundary was
`dense` (connected speech, cut at the quietest point with a 4 ms fade).

- A wrong take or a cut in the middle of a sentence is a planner decision:
  check the item in `take_plan.json`.
- A clipped word usually means the transcript or MFA put the word boundary in
  the wrong place: check `words[*].alignment` (`mfa` or `whisper`) and the core
  times in the boundary plan.
- A pause that feels too long or short comes from the class the planner chose
  for it (`break_after`).

## Video publication problems

Video output requires a video input. Output formats are `.mkv`, `.mov`, `.mp4`,
and `.webm`.

The video renderer uses only selected source intervals with direct visual cuts.
It does not insert semantic pause time or frozen frames. Publication validates
the stream type, output duration, and audio/video synchronization.

If stage `06 MEDIA PUBLICATION` fails, inspect:

- `00_media/media_input.json` for selected source streams and offsets;
- `05_final/final_render_manifest.json` for the source interval timeline;
- `06_publication/video_render_manifest.json` when it was created;
- FFmpeg/FFprobe availability and codec support.

Converting a MOV input to MP4 is not required for processing. Choose `.mp4` as
the output when browser-compatible H.264/AAC delivery is desired.

## Unsupported input or output format

VoiceCut probes the input with FFmpeg, so some additional readable input
containers may work. Published output extensions are restricted.

Supported audio outputs:

```text
.aac .aif .aiff .flac .m4a .mp3 .oga .ogg .opus .wav
```

Supported video outputs:

```text
.mkv .mov .mp4 .webm
```

Choose the output container through `-o`. A video output cannot be created from
an audio-only input.

## Inspect a run

| File | What it answers |
| --- | --- |
| `pipeline_config.json` | Which exact input, models, language, and settings own this cache? |
| `pipeline_run.json` | Which stages were created or reused, and which warnings occurred? |
| `04_semantic_plan/take_plan.json` | Which word ranges were kept, with which pause classes, and why? |
| `05_final/final_boundary_plan.json` | Where is every cut, how long is every pause, where did ambience come from? |
| `05_final/final_render_manifest.json` | What was rendered (duration, cut and pause counts, detector status)? |
| `06_publication/publication.json` | Which final media file was validated and published? |

## Reporting an issue

Include:

- the VoiceCut commit;
- macOS, Python, FFmpeg, micromamba, and MFA versions;
- the exact command with secrets removed;
- `pipeline_config.json`, `pipeline_run.json`, and relevant manifests;
- the delivery status and stage named in the error;
- a short publication-safe source excerpt only when you have permission to
  share it.

Do not publish `.env`, provider keys, full private recordings, model weights,
or an entire work directory. Planner raw responses can contain transcript text,
so review them before sharing.
