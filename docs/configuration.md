# Configuration reference

VoiceCut is designed to work with one required positional argument:

```bash
voicecut recording.wav
```

The default planner is Gemini, the default language is English, and the output
is written beside the input as `recording_edited.wav`. This guide documents the
available choices and the operational consequences of changing them.

Run `voicecut --help` for the authoritative option list in the installed
revision. Installation is covered in [Installation](installation.md), and the
reasoning behind the settings is covered in [Architecture](architecture.md).

## Common commands

English audio with Gemini:

```bash
voicecut recording.wav --planner-backend gemini
```

Russian audio:

```bash
voicecut recording_ru.wav \
  --language ru \
  --planner-backend gemini
```

Choose the destination container through the output extension:

```bash
voicecut recording.wav \
  -o recording_edited.mp3 \
  --planner-backend gemini
```

Edit a video from its narration track:

```bash
voicecut presentation.mov \
  -o presentation_edited.mp4 \
  --planner-backend gemini
```

Keep an inspectable cache for a long run:

```bash
voicecut interview.wav \
  -o interview_edited.wav \
  --work-dir .voicecut-cache/interview \
  --planner-backend gemini
```

An existing destination is never replaced unless `--overwrite` is supplied.

## Planner backends

The planner decides which exact transcript word occurrences form the intended
narration. It does not choose waveform samples or synthesize speech.

| Backend | Default model | Credential | Status |
| --- | --- | --- | --- |
| `gemini` | `gemini-3.1-pro-preview` | `GEMINI_API_KEY` | Cloud |
| `openai` | `gpt-5-mini` | `OPENAI_API_KEY` | Cloud |
| `deepseek` | `deepseek-v4-flash` | `DEEPSEEK_API_KEY` | Cloud |
| `qwen` | `mlx-community/Qwen3.5-9B-4bit` | None | Local convenience alias |
| `gemma` | `mlx-community/gemma-3-12b-it-4bit` | None | Local convenience alias |
| `local` | `mlx-community/Qwen3.5-9B-4bit` | None | Arbitrary MLX-LM-compatible model/path |

Local planner transport is implemented but its editing quality is
experimental. A strong cloud model is currently the recommended option for
reliable semantic selection. All backends must pass the same local range,
ordering, commitment, and source-grounding validation; changing a model does
not bypass those checks.

### Cloud configuration

VoiceCut reads credentials from the process environment or the selected dotenv
file, which defaults to `.env`:

```dotenv
GEMINI_API_KEY=...
OPENAI_API_KEY=...
DEEPSEEK_API_KEY=...
```

Use a provider:

```bash
voicecut input.wav --planner-backend openai
voicecut input.wav --planner-backend deepseek
```

Override its default model:

```bash
voicecut input.wav \
  --planner-backend openai \
  --planner-model gpt-5-mini
```

Use a different dotenv file or secret-variable name:

```bash
voicecut input.wav \
  --planner-backend openai \
  --env-file config/voicecut.env \
  --planner-api-key-env COMPANY_OPENAI_KEY
```

OpenAI and DeepSeek support an endpoint override:

```bash
voicecut input.wav \
  --planner-backend deepseek \
  --planner-base-url https://api.deepseek.com
```

The URL must use HTTP or HTTPS and include no credentials, query parameters,
fragments, whitespace, or control characters. OpenAI custom endpoints must
implement the Responses API. DeepSeek uses its OpenAI-compatible JSON
transport. Keep all credentials in environment variables rather than URLs or
command history.

### Local configuration

The `qwen` and `gemma` aliases select their default MLX models:

```bash
voicecut input.wav --planner-backend qwen
voicecut input.wav --planner-backend gemma
```

Choose another Hugging Face repository or a local model directory:

```bash
voicecut input.wav \
  --planner-backend local \
  --planner-model mlx-community/Qwen3.5-27B-4bit
```

After a model has been downloaded, prevent network access for model loading:

```bash
voicecut input.wav \
  --planner-backend local \
  --planner-model /path/to/model \
  --local-files-only
```

Local models use Apple unified memory. Larger models can be much slower or
exceed available memory. `--planner-python` selects the MLX helper interpreter;
the installer configures it as `.venv-mlx/bin/python`.

## Privacy and data flow

All audio decoding, waveform analysis, transcription, MFA alignment, breath
analysis, rendering, and publication happen locally.

With a cloud planner, VoiceCut sends the transcript of each planning window
(word IDs and text) to the selected provider. With Gemini it also sends, by
default, a compressed 16 kHz mono MP3 of exactly that window so the model can
hear stumbles and resolve mis-heard words; it never uploads the original file
or any video. Use `--planner-audio off` to send text only. The provider's
data-processing terms apply to everything submitted.

With `local`, `qwen`, or `gemma`, transcript processing also remains local.

VoiceCut filters unrelated provider credentials out of child-process
environments. API keys are not written to run manifests. A configured endpoint
may be recorded as non-secret configuration, so it must never contain a key.

## Languages

| Setting | English (`en`) | Russian (`ru`) |
| --- | --- | --- |
| Default | Yes | No; select explicitly |
| MLX Whisper language | `en` | `ru` |
| Hidden-retry CTC enrichment | Enabled; fail-soft optional evidence | Skipped by language policy |
| MFA model | `english_us_arpa` | Revision-pinned `MontrealCorpusTools/russian_mfa` |
| Breath-cleanup default | `replace` | `off` |

Select Russian with:

```bash
voicecut input.wav --language ru
```

English behavior remains the default and is unchanged by selecting no language
option. Russian uses the primary Whisper transcript directly because the
optional hidden-retry CTC component has not been validated for Russian.
Respiro-en likewise has not been validated as a Russian breath detector, so
Russian defaults to `--breath-cleanup off`. An explicit cleanup selection
overrides the language default, but it should be treated as experimental for
Russian.

The selected language should match the dominant spoken narration. VoiceCut is
not currently a multilingual code-switching or automatic language-detection
pipeline.

## Media formats

FFmpeg decodes one input audio stream to a canonical float PCM WAV. Common
audio input extensions are:

```text
.aac .aif .aiff .flac .m4a .mp3 .oga .ogg .opus .wav
```

Audio output extensions are:

```text
.aac .aif .aiff .flac .m4a .mp3 .oga .ogg .opus .wav
```

Video input and output extensions are:

```text
.mkv .mov .mp4 .webm
```

Other FFmpeg-readable inputs can work because stream probing, not only the
filename, determines the input kind. Output extensions are intentionally
restricted. Audio output is encoded once from the final internal WAV. MP4,
MOV, and MKV use H.264/AAC; WebM uses VP9/Opus.

### Video cut policy

Video is edited strictly from selected source-motion intervals:

- retained speech keeps the corresponding pictures at normal speed;
- removed speech removes the corresponding pictures;
- source intervals are joined with direct visual cuts;
- pauses are built from original silence only: no ambience is inserted;
- no frozen frame or slow-motion hold is inserted;
- natural timing inside each continuous retained take remains;
- the edited narration replaces the original audio track;
- publication verifies output duration and audio/video synchronization.

VoiceCut does not inspect visual meaning, shots, gestures, captions, or slide
changes. The video mode is best suited to talking heads and narrated screen
recordings where the speech track determines the edit.

## Output naming and overwrite policy

When `-o` is omitted, VoiceCut uses `<input_stem>_edited` and preserves a common
input container extension:

```bash
voicecut recording.m4a
# recording_edited.m4a
```

For an uncommon input extension, the fallback output is `.wav` for audio and
`.mp4` for recognized video input. The output and input must be different, and
neither may contain or be contained by the selected work directory.

Use `--overwrite` only when intentionally replacing an existing destination:

```bash
voicecut input.wav -o result.wav --overwrite
```

## Work directory and cache

Without `--work-dir`, VoiceCut creates a content-addressed directory below
`.voicecut-cache` beside the output. Its identity includes the input hash,
language, planner settings, model choices, renderer settings, and a fingerprint
of production source code.

Set an explicit location for a long or inspectable run:

```bash
voicecut input.wav \
  --work-dir .voicecut-cache/input-gemini \
  --planner-backend gemini
```

Completed stages are reused only when their artifacts and relevant identities
validate. Whisper and English CTC stages keep resumable per-region checkpoints,
so an interrupted long recording need not restart at the beginning.

Do not:

- edit cached JSON or WAV artifacts manually;
- run two VoiceCut processes against the same work directory;
- reuse one explicit work directory with a different input or configuration;
- put the input or output inside the work directory.

When comparing models, use separate directories:

```bash
voicecut take.wav -o take_gemini.wav \
  --work-dir .voicecut-cache/take-gemini \
  --planner-backend gemini

voicecut take.wav -o take_gemma.wav \
  --work-dir .voicecut-cache/take-gemma \
  --planner-backend gemma
```

## Breath cleanup and ambience

The language-dependent default can be overridden with:

```bash
voicecut input.wav --breath-cleanup replace
voicecut input.wav --breath-cleanup off
```

- `replace` (English default) treats every non-speech sound inside a pause --
  breaths, clicks, mouth noise -- as something to remove, and fills the pause
  with clean ambience taken from the same recording. Respiro-en breath
  probabilities also stop a word's head or tail from swallowing an adjacent
  breath.
- `off` (Russian default, because Respiro-en is not validated for Russian)
  keeps the original content of every pause; long pauses are still shortened by
  removing their middle.

`--breath-threshold` (default `0.5`) is the Respiro-en frame probability above
which a frame counts as breath. `--respiro-cache-root` points to the
hash-verified runtime. If the detector cannot run, cleaning continues on
waveform energy alone and the manifest records `detector_failed_energy_only`.

## Take-selection controls

| Option | Default | Purpose |
| --- | --- | --- |
| `--window-seconds` | `240` | Recording time decided per planner request; never an edit boundary |
| `--lookahead-seconds` | `120` | Following recording time shown undecided, so later retakes are visible |
| `--planner-audio` | `auto` | `auto`/`on`: Gemini also hears each window; `off`: text only |
| `--max-output-tokens` | `65536` | Planner response limit, including model reasoning |

Every planner request is cached by its exact prompt and audio in
`04_semantic_plan/calls/`, so an interrupted run resumes without paying for
finished windows again.

## Transcription engine

`--asr whisper` (default) transcribes with MLX Whisper. Whisper tends to write
a quick restart such as "In a tr- in a transformer" as a single "In a
transformer", which leaves the planner nothing to cut. `--asr crisperwhisper`
uses [CrisperWhisper 2.0](https://github.com/nyrahealth/CrisperWhisper), a
verbatim Whisper fine-tune that keeps false starts, repetitions and fillers as
separate timed words; the English hidden-retry CTC pass is then skipped because
the transcript already contains those retries. It is slower (about 14 minutes
per hour of audio on Apple silicon), and its model weights are released under a
non-commercial research license, so it is opt-in. Install it into the MLX
environment first:

```bash
.venv-mlx/bin/python -m pip install -e ".[mlx,crisperwhisper]"
```

## Alignment and runtime controls

| Option | Meaning |
| --- | --- |
| `--alignment-backend mfa` | Word alignment backend; currently the only choice |
| `--mfa-prefix PATH` | micromamba prefix containing MFA 3.4.1; defaults to `.mfa-env` |
| `--mfa-cache-root PATH` | Persistent MFA model/cache root |
| `--mfa-micromamba PATH` | micromamba executable |
| `--mfa-num-jobs N` | Parallel jobs used by the batched MFA command; defaults to at most four CPUs |
| `--asr whisper\|crisperwhisper` | Transcription engine (see *Transcription engine*) |
| `--asr-python PATH` | Python containing MLX Whisper, optional CrisperWhisper, and the optional English CTC helper |
| `--whisper-model NAME` | Override the MLX Whisper repository |
| `--planner-python PATH` | Python used for local MLX language models |

MFA identifies which stretch of the waveform belongs to which kept word; the
waveform energy decides where each word physically starts and stops. If MFA is
unavailable, Whisper word timing is refined by the same energy analysis and the
run reports a warning.

## Editor command

`voicecut-editor` serves the browser editor and runs the Voice Cut button with
the same pipeline and defaults as the CLI:

```bash
voicecut-editor                                   # open the editor
voicecut-editor recording.mov                     # open with a file imported
voicecut-editor --language ru --planner-backend gemini
voicecut-editor --port 9000 --no-browser --projects-dir ~/voicecut-projects
```

| Option | Description |
| --- | --- |
| `INPUT` | Optional media file to import at startup |
| `--host`, `--port` | Bind address; default `127.0.0.1:8765` |
| `--no-browser` | Do not open a browser window |
| `--projects-dir PATH` | Editor project storage; default `.voicecut-cache/editor` |
| `--language`, `--planner-backend`, `--planner-model`, `--breath-cleanup` | Defaults for the Voice Cut button (changeable in the editor settings) |
| `--env-file PATH` | dotenv file for planner credentials; default the repository `.env` |
| `--dev` | Serve the API only, for use with the Vite dev server |

See [Editor](editor.md) for the workflow and export semantics.

## Diagnostics

Every run keeps its diagnostics in the work directory: the take-plan ledger
(`04_semantic_plan/take_plan.json` with every window, revocation and review,
plus the raw requests in `calls/`), the MFA batch, and
`05_final/final_boundary_plan.json` with every join. `--debug-artifacts` is
accepted for compatibility and adds nothing. See
[Troubleshooting](troubleshooting.md#inspect-a-run) for the most useful files.

## Complete option summary

| Option | Description |
| --- | --- |
| `INPUT` | Required input audio or video path |
| `-o`, `--output PATH` | Published output path |
| `--work-dir PATH` | Persistent stage cache |
| `--overwrite` | Replace an existing non-cached destination |
| `--env-file PATH` | dotenv file; default `.env` |
| `--language en\|ru` | Source language; default `en` |
| `--planner-backend NAME` | `gemini`, `openai`, `deepseek`, `local`, `qwen`, or `gemma` |
| `--planner-model NAME` | Provider model, Hugging Face repository, or local path |
| `--planner-base-url URL` | OpenAI/DeepSeek endpoint override |
| `--planner-api-key-env NAME` | Credential environment-variable name |
| `--local-files-only` | Prevent local-model downloads |
| `--window-seconds N` | Recording time decided per planner request |
| `--lookahead-seconds N` | Undecided look-ahead shown to the planner |
| `--planner-audio auto\|on\|off` | Whether the planner listens to each window |
| `--max-output-tokens N` | Planner response limit |
| `--whisper-model NAME` | MLX Whisper model override |
| `--alignment-backend mfa` | Word alignment backend |
| `--mfa-prefix PATH` | Pinned MFA environment |
| `--mfa-cache-root PATH` | MFA model/cache root |
| `--mfa-micromamba PATH` | micromamba executable |
| `--mfa-num-jobs N` | MFA batch parallelism |
| `--breath-cleanup off\|replace` | Pause-cleaning policy |
| `--breath-threshold FLOAT` | Respiro-en frame threshold |
| `--respiro-cache-root PATH` | Verified Respiro-en runtime cache |
| `--asr-python PATH` | Advanced ASR helper runtime |
| `--planner-python PATH` | Advanced local-planner runtime |
| `--debug-artifacts` | Accepted for compatibility; no effect |
