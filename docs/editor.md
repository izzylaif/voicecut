# VoiceCut Editor

The VoiceCut Editor is a local, browser-based correction editor for VoiceCut's
automatic edit. VoiceCut still makes the first edit with its full production
pipeline; the editor shows that edit as a timeline over the **original**
recording, lets you fix it by hand, and exports the result through VoiceCut's
own renderer.

```
Open a recording (audio or video)
    ↓
Voice Cut  →  the production pipeline runs, with live stage progress
    ↓
Timeline = Voice Cut's edit: one clip per kept stretch of the recording
    ↓
Drag clip edges into the removed material or trim them, split, delete,
resize or insert pauses, undo/redo, listen — the preview is the exported
sound, sample for sample
    ↓
Export audio or video
```

The command-line `voicecut` tool is unchanged.

## Installation

The repository installer provisions the editor with the rest of VoiceCut:

```bash
./scripts/install.sh
source .venv/bin/activate
```

It installs the `editor` Python extra (FastAPI, uvicorn), installs the frontend
dependencies in `frontend/` and builds the browser bundle into
`src/voicecut/editor/static/` (not committed). Manual equivalent:

```bash
.venv/bin/python -m pip install -e ".[audio,cloud,editor]"
cd frontend && npm install && npm run build && cd ..
```

Building the frontend needs Node.js 22 or newer. The editor needs FFmpeg on
`PATH`; if there is none, `voicecut-editor` uses the FFmpeg of VoiceCut's own
MFA environment (`.mfa-env/bin`) when it exists.

## Launching

```bash
voicecut-editor                      # open the editor in the browser
voicecut-editor recording.wav        # open a file right away
voicecut-editor --language ru --planner-backend gemini
voicecut-editor --port 9000 --no-browser --projects-dir ~/voicecut-projects
```

The server listens on `http://127.0.0.1:8765` by default (this computer only).
The Voice Cut button uses the same planner credentials as the CLI: the `.env`
in the repository unless `--env-file` says otherwise; keys can be added in the
editor's **Settings** (see below). Run `voicecut-editor --help` for every option.
The editor uses only standard web APIs (Web Audio, `<video>`, canvas); it is
developed and tested in Chromium-based browsers.

## Workflow

### 1. Open a recording

Drop a file on the start page, choose one, or type the absolute path of a file
on this machine (**Open**): a path is used in place, a dropped or chosen file is
copied into the project. WAV, MP3, M4A, FLAC, AAC, AIFF, OGG, Opus, MOV, MP4,
MKV and WebM are accepted. Opening the same content again reopens its project
with your edits; the start page lists recent projects.

The recording appears at once as one clip. For video, the editor also builds a
small preview copy in the background (a few seconds for a minute of 1080p);
you can listen and edit meanwhile, and the picture appears when it is ready.

### 2. Voice Cut

Choose the **planner**, its **model** and the recording's **language** in the
top bar, then press **Voice Cut**:

- *Planner* — Gemini, OpenAI, DeepSeek, OpenRouter, or a local model through
  Ollama. Gemini models also *listen* to the audio of every window while they
  choose the takes (marked “hears audio”); the others plan from the transcript
  only (“text only”).
- *Model* — the list is fetched live from the provider with your key and
  filtered to text models that can plan, with the default on top
  (`gemini-3.8-flash` for Gemini, `google/gemma-4-31b-it` for OpenRouter), then
  newest first. Type in the search field to filter, or to use any model name
  that is not listed. For local models the list shows the recommended models
  and the ones your Ollama already has; any model of the Ollama library works.
- *Language* — English or Russian.

The choice is saved with the project and used for new projects too. Until you
choose, new projects plan with Gemma 4 31B on this computer where it runs well
(Apple Silicon with 32 GB of memory or more, or an NVIDIA GPU with 24 GB), and
with the same model through OpenRouter elsewhere. Pressing **Voice Cut** with a
local model that is not downloaded yet offers the download and starts Voice
Cut when it is there. If the planner's API key is missing or the provider
rejects it, the model list says so and Voice Cut refuses to start until the key
is added.

Voice Cut edits what is on the timeline. Split the recording and delete what
you do not want first, and only the parts that are left are transcribed,
planned and cut; the panel's log says how much that is. After a Voice Cut,
deleting a clip of its edit (or trimming one) and running it again leaves that
out too, while everything Voice Cut itself had removed from those parts is
decided again. Put the whole recording back on the timeline to edit all of it.

The production pipeline runs in a worker process — exactly what
`voicecut <file>` runs with those options — and its stages appear live in the
side panel:

```
Checking runtimes · Preparing media · Analyzing speech · Transcribing ·
Recovering hidden retries · Recovering skipped speech · Choosing takes ·
Aligning and rendering the edit · Publishing
```

Each stage shows its duration (or *cached* / *skipped*); the pipeline's log is
one click away, and failures are shown in the panel. **Cancel** stops the run.
When it finishes, the timeline becomes Voice Cut's edit and the transcript
appears. A run that fails or is cancelled keeps the previous result editable.
Running Voice Cut again replaces the other manual edits (the editor asks
first).

**Settings** (the gear icon) holds the API keys of the cloud planners
(`GEMINI_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`) and the breath option.
Keys are written into the editor's env file — the same file `voicecut` reads —
changing only that key's line; other lines are kept. A key is checked with its
provider before it is saved, is shown only masked (`••••abcd`), and is never
sent back to the browser or logged. A key set as an environment variable of the
`voicecut-editor` process takes precedence over the file, as in the CLI; the
dialog says so.

**Appearance.** The editor has a light and a dark theme. *Settings →
Appearance* (also reachable from *Settings…* on the start page) chooses
**System** (the default), **Light** or **Dark**. System follows the light or
dark mode of the operating system, live: switching macOS to dark mode while
the editor is open switches the editor too. The choice belongs to the user,
not to a project; in a browser it is remembered for the editor's address, and
in the VoiceCut app the app remembers it. Both themes are drawn on their own —
the light timeline has mint clips with a dark green waveform and darker amber
and red marks — rather than one inverted from the other.

### 3. Correct the edit

The timeline shows, top to bottom:

- **overview** — the whole original recording with the kept parts highlighted,
  the stretch the timeline currently shows outlined, and the playhead's place
  in the recording; click it to jump there;
- **ruler** — time in the edited result; click or drag to move the playhead;
- **picture** (video only) — thumbnails of each clip;
- **sound** — each clip with the waveform of exactly the audio it plays and
  its words, and each pause as a block of its own with its length: hatched
  grey for Voice Cut's pauses, hatched blue for pauses you added.

Clips sit exactly where they land in the exported file: the layout uses the
exporter's own arithmetic, crossfades and pauses included.

| Action | How |
| --- | --- |
| Play / pause | `Space`, or the play button |
| Listen faster or slower | the `1×` button next to split/delete (0.5×, 1×, 1.5×, 2×, 2.5×); `<` / `>` |
| Move the playhead | click/drag the ruler, or click a clip; `←`/`→` 10 ms, `⇧←`/`⇧→` 1 s; click a word in the transcript |
| Previous / next cut | `↑` / `↓` |
| Lengthen or shorten a clip | drag its left or right edge |
| Lengthen or shorten a pause | drag its left or right edge |
| Insert a pause at the playhead | `P`, the pause button next to split, or right-click → *Insert a 1 s pause here* |
| Split at the playhead | `S` or `⌘B` |
| Trim the clip under the playhead to the playhead | `⌥[` (start) / `⌥]` (end) |
| Select clips and pauses | click; `⇧`-click selects everything from the last one clicked; `⌘`-click adds or removes one; `⌘A` selects all; `Esc` clears |
| Move clips | drag a clip: it and the other selected clips go to the cut nearest the pointer |
| Copy clips | drag a clip with `⌥` held; or `⌘C` and `⌘V` (pastes at the playhead, splitting the clip there); `⌘D` duplicates after the selection; `⌘X` cuts |
| Delete selected clips or pauses | `Delete` / `Backspace` |
| Everything possible at one spot | right-click the timeline: insert a pause, split, copy, cut, duplicate, paste, delete |
| Undo / redo | `⌘Z` / `⇧⌘Z` |
| Back to Voice Cut's edit | the reset button in the toolbar (undoable) |
| Zoom | `⌘+` / `⌘−`, `⌘`-scroll or pinch, the slider; `⇧Z` fits the whole edit |
| Scroll | scroll or swipe horizontally |
| Snapping on/off | `N`; hold `⌥` while dragging to ignore it once |

Letter shortcuts follow the physical key, so they work with any keyboard
layout (with a Russian layout, `S` is the key that types «ы»).

**Dragging edges.** Every clip is a window onto the original recording, so a
clip edge can be dragged outward into material Voice Cut removed — a word it
cut short, a sentence it dropped — or inward to trim. The edit ripples: the
dragged edge stays under the pointer and nothing else changes length.

- Dragging a clip's **end** moves everything after it.
- Dragging a clip's **start** pulls material in from (or gives it back to) the
  left: the rest of the clip and everything after it stay where they are on
  screen, and everything before it moves.

A clip cannot run into the material of a neighbour that comes after it in
the recording; when its edge meets that neighbour the two play straight
through, shown as a dotted line. A clip you moved or copied is not held back:
its edges reach anywhere in the recording. Near the side of the timeline the view scrolls along while
you drag. Material Voice Cut had removed is drawn in amber in the waveform,
the words and the transcript, so you always see what you brought back. While
dragging, the edge snaps to Voice Cut's own cut points and to word edges; a
label shows how much the edge moved and the clip's new length. Dragging an
edge back onto Voice Cut's exact sample restores Voice Cut's fade there.
When the whole edit is zoomed out so far that a clip or pause is only a few
pixels wide, clicking it moves the playhead instead of grabbing an edge; zoom
in to drag.

**Pauses.** A pause is part of the timeline in its own right, not a side
effect of the clips around it:

- Trimming, extending or splitting a clip never changes the pause next to it;
  the pause rides along with the ripple.
- Dragging a pause's **end** edge lengthens or shortens it and moves everything
  after it; dragging its **start** edge does the same but keeps what follows in
  place on screen, like a clip's start. A pause can be 10 ms to 10 minutes.
- `P` (or the right-click menu) inserts a pause at the playhead: inside a clip
  the clip is split there and a 1 s pause goes between the halves; at a cut
  (within 10 ms of a clip edge) the pause goes into the cut; on a pause, or at
  a cut that already has one, that pause gets 1 s longer. Drag its edges
  afterwards for any other length.
- Deleting a pause leaves a direct cut between the two clips.
- Deleting a clip between two pauses keeps **one** pause, the longer of the
  two: removing a sentence neither leaves a double silence nor glues the
  sentences around it together. Deleting a clip next to a single pause keeps
  that pause.

A pause you have not resized plays Voice Cut's own room tone, sample for
sample. A resized or inserted pause is filled with clean room tone from the
same recording — the quiet stretches without speech or breath that Voice Cut
itself takes room tone from (`cut_render.build_ambience_bank`) — and joins the
clips beside it with the same equal-power crossfades. Its room tone is chosen
deterministically, so the preview and every export of the same edit are the
same samples.

Video projects have no pauses: Voice Cut's video edits are direct cuts and
there is no picture to hold during a pause, so pause insertion is disabled
there.

The preview plays exactly what the export contains (see below). The viewer
shows the picture for video. For audio it shows subtitles: the words of the
clip being heard — the same numbered block as in the transcript panel — stand
still on screen while a green highlight slides from word to word, and the next
block appears when playback reaches the next clip (during a pause the last
block stays). A very long clip is shown in parts that break after a sentence.
The transcript panel lists the text of every clip and follows playback.

Edits are saved automatically after every change (the top bar says *Saved*).
Reloading the page or restarting `voicecut-editor` reopens the saved edit.
Voice Cut's own result is never modified.

### 4. Export

Press **Export**, choose the format and where to save (the default follows the
CLI's `<name>_edited.<ext>`: next to the source for files opened by path, in
`~/Downloads` for copied files), and confirm. Audio exports as WAV (24-bit),
FLAC, AAC or MP3; video projects also export MP4 or MOV. An existing file is
replaced only if you tick *Replace the existing file*. The dialog shows the
stages and, at the end, **Show in Finder** and **Download**.

- An untouched Voice Cut edit of the whole recording is copied from VoiceCut's
  own published file when the format matches.
- Otherwise the edit is rendered once from the canonical WAV with VoiceCut's
  overlap-add renderer and published with VoiceCut's own publishers; video is
  cut from the original file with the same direct cuts as the CLI.

## What the export keeps

VoiceCut's audio output is more than cut points: every join is a short
equal-power crossfade, and a pause may be retimed with clean room tone (which
is also how a breath in it is removed). The editor keeps all of that wherever
it is still valid:

| Voice Cut's work | Kept when | Otherwise |
| --- | --- | --- |
| A pause between two clips, before the first or after the last word | it is still there (pauses survive edits around them) | deleted by you, or by deleting a clip between two pauses (the longer one stays) |
| The room tone of that pause | the pause keeps its length and the fades of the clips beside it | clean room tone from the recording's ambience, same crossfades |
| Fade at a clip edge | the edge is still Voice Cut's own | 2 ms declick fade |

Where two clips continue the same audio (after a split, or an edge dragged
onto its neighbour) there is no fade at all, and the recording's own start and
end get none either. Manual cuts are not re-checked against the alignment;
that is what a manual override is for.

## Preview = export

The preview does not approximate the export. When you play, the server renders
the stretch of the edit under the playhead with the same pieces, fades and
room tone the export uses (`plan.render_range`, which matches
`cut_render.render_pieces` bit for bit), and the browser plays those samples
back to back on an audio clock running at the recording's sample rate. Only a
few seconds are fetched ahead, so hour-long recordings play as easily as short
ones. The WAV export differs from the preview only by its 24-bit quantisation.

For video, the picture follows the audio clock: two `<video>` elements take
turns, one playing the current clip while the other waits, already positioned,
on the first frame of the next clip.

**Playback speed.** The `1×` button plays the preview at 0.5× to 2.5× to listen
through an edit quickly or check a cut slowly; the choice is remembered in the
browser. The voice keeps its pitch: the same rendered samples pass through a
WSOLA time stretcher (30 ms frames, each aligned to the waveform it continues)
before they are played, and the picture runs at the same rate on the audio
clock. Speed affects only listening — at 1× the preview is the export sample
for sample, and the export is always rendered at normal speed.

## Orientation of phone video

Phone recordings often store their frames sideways and carry a rotation (or
mirroring) tag that players apply. The editor never asks the browser to
interpret that tag: its preview copy is made by FFmpeg, which applies the tag
while transcoding — exactly as it does when VoiceCut renders the exported
video. Preview and export therefore always show the picture the same way up.

## Time model

Everything is measured in samples of the canonical WAV that VoiceCut edits.
A timeline is a list of clips and pauses (never two pauses in a row). A clip
is a source interval `[start, end)` with the origin of each edge (`voicecut`,
`manual`, or `source` for the recording's start/end). A pause is the length
heard between the crossfades of its neighbours, with its origin (`voicecut`
and the join it came from, or `manual`). The timeline's output time is
computed from the items with the exporter's piece arithmetic. Nothing is
quantised to frames, so repeated edits cannot drift.

`timeline.json` is schema 3. An edit saved by an earlier editor (schema 2,
clips only) is converted when it is opened: a Voice Cut pause is placed where
the old rule kept it (both facing edges Voice Cut's own, the clips neighbours
in Voice Cut's edit), so the converted edit sounds exactly as before.

## Architecture

```
 Browser (React + TypeScript, frontend/)            Python (voicecut.editor)
 ┌─────────────────────────────────────┐   HTTP    ┌────────────────────────────────┐
 │ model/timeline.ts  clips and pauses  │◄────────►│ server.py   FastAPI routes      │
 │ model/segments.ts  clip edges        │   + SSE   │ projects.py project storage     │
 │ model/layout.ts    exporter's layout │           │ ambience.py room tone for pauses│
 │ store.ts           history, autosave │           │ worker.py   pipeline subprocess │
 │ timeline/view.ts   canvas timeline   │           │ timeline.py proposal, validation│
 │ timeline/peaks.ts  waveform levels   │           │ plan.py     pieces, range render│
 │ audio/player.ts    streaming player  │           │ peaks.py    waveform peaks      │
 │ audio/stretch.ts   speed, same pitch │           │ proxy.py    video preview       │
 │ video/videoSync.ts two-deck video    │           │ export.py   render + publish    │
 │ theme.ts           light and dark    │           │ jobs.py     jobs, SSE           │
 └─────────────────────────────────────┘           │ settings.py keys, model lists   │
                                                    └────────────────────────────────┘
                                                               │
                                       full_pipeline.run_full_pipeline · cut_render ·
                                       media.publish_audio · video_render
```

- **Projects.** Each opened file is a project under `.voicecut-cache/editor/`
  (`--projects-dir`). Opening decodes the canonical WAV (the same
  `media.extract_audio_to_wav` the pipeline uses) and computes the waveform's
  base level; video projects then build their preview copy and thumbnails.
- **Voice Cut.** The server starts `python -m voicecut.editor.worker`, which
  calls `run_full_pipeline` with a progress callback and a streaming
  subprocess runner and writes JSON events (stages, log lines, result) that the
  server relays over Server-Sent Events. Clients replay events from any index,
  so a reloaded page picks the run up where it is.
- **Parts of the recording.** When the timeline keeps less than the whole
  recording, `scope.timeline_scope` works out which parts Voice Cut edits and
  `scope.write_excerpt` writes them, in order, into one WAV, sample for sample
  from the canonical WAV (`voicecut/parts/`). The pipeline edits that excerpt
  (`--pause-policy cuts` for a video), and `scope.map_plan` maps its sealed
  plan back onto the recording: intervals, fades, joins, room tone and words.
  An interval running across the end of one part into the next becomes two
  clips joined by a cut with the 2 ms declick fade, as a manual cut gets. The
  mapped plan, with its parts in `editor_scope`, is the proposal from then on.
- **Proposal.** `timeline.load_proposal` reads VoiceCut's sealed
  `05_final/final_boundary_plan.json` (planner
  `authoritative_single_pass_boundary_plan_v2`, renderer `cut_render_v1`) once
  and exposes the kept intervals, their fades, the room tone at each join, the
  lead-in/lead-out and the words.
- **Timeline.** The browser keeps an undo history of immutable timelines
  (clips and the pauses between them). Each finished gesture is saved
  (`PUT /timeline`); the server validates it, derives its pieces
  (`plan.derive_pieces`) and answers with a revision.
- **Room tone.** `ambience.ambience_runs` finds the recording's clean quiet
  stretches with the pipeline's own `cut_render.build_ambience_bank` (cached in
  `media/ambience.json`); a resized or inserted pause takes its room tone from
  that bank, seeded by the pause's id.
- **Layout.** `frontend/src/model/layout.ts` ports `derive_pieces` and
  `cut_render.place_pieces`; `tests/test_editor_plan.py` computes layout cases
  in Python and `frontend/tests/layout.test.ts` checks the port against them.
- **Playback.** `GET /render?rev=…&start=…&frames=…` renders any stretch of the
  output (`plan.render_range`); the player fetches fixed chunks ahead of the
  playhead and schedules them sample-exactly.
- **Theme.** `frontend/src/theme.ts` resolves the appearance to
  `<html data-theme="light|dark">`, following `prefers-color-scheme` for
  System. `styles.css` keys every colour on it (tints are `color-mix`es of the
  theme's tokens) and the timeline canvas takes its palette from
  `timeline/palette.ts`, redrawing when the theme changes. `index.html` sets the
  theme before the first paint, so a page never flashes the other one. In the
  VoiceCut app the theme module also asks the app for the choice (`appearance`)
  and reports changes (`set_appearance`), so that native windows match.
- **Waveform.** One base level (min/max per 256 samples) covers the whole
  recording; the browser derives coarser levels and fetches finer ones
  (`/peaks/fine`) for the zoomed-in stretch only.
- **Export.** `export.export_timeline` builds the editor boundary plan (planner
  `video_render.EDITOR_BOUNDARY_PLAN_PLANNER`), renders it with
  `cut_render.render_pieces`, verifies that kept speech was copied untouched,
  and publishes with `media.publish_audio` or `video_render.render_edited_video`.

### HTTP API

| Route | Purpose |
| --- | --- |
| `GET /api/config` | Languages, planner backends, defaults |
| `GET /api/settings`, `PUT /api/settings/keys/{planner}` | Planners with masked key status; save or remove a key `{key}` |
| `GET /api/planners/{planner}/models?refresh=` | The provider's planner models, or why they cannot be listed |
| `PUT /api/projects/{id}/settings` | Save the project's planner, model, language and breath option |
| `GET /api/projects`, `POST /api/projects/import` | List; open a file (multipart upload or `{"path"}`) |
| `GET`/`DELETE /api/projects/{id}` | Project state (media, run, proposal, saved timeline, exports, running jobs) |
| `GET /api/projects/{id}/peaks`, `/peaks/fine` | Waveform levels (int16 min/max pairs) |
| `GET /api/projects/{id}/proxy.mp4`, `/filmstrip/{n}` | Video preview copy and thumbnail sheets |
| `POST /api/projects/{id}/prepare` | Rebuild the video preview |
| `PUT /api/projects/{id}/timeline` | Save the edit; returns its revision and output length |
| `GET /api/projects/{id}/render` | Planar float32 samples of a stretch of the edit |
| `POST /api/projects/{id}/voicecut`, `/cancel` | Start or cancel Voice Cut |
| `POST /api/projects/{id}/export` | Export `{timeline, destination, overwrite}` |
| `GET /api/projects/{id}/exports/{e}/download`, `POST …/reveal` | Download; show in Finder |
| `GET /api/jobs/{job}`, `/api/jobs/{job}/events?since=N` | Job state; its events as SSE |

## Artifacts

```
.voicecut-cache/editor/<project>/
  project.json                  settings, run state, preview state, recent exports
  source/<file>                 a copied file (files opened by path stay in place)
  media/source_audio.wav        canonical WAV (+ media_input.json)
  media/peaks_256.bin           waveform base level
  media/ambience.json           room-tone stretches for resized and added pauses
  media/proxy.mp4, filmstrip/   video preview copy and thumbnail sheets
  voicecut/<name>_edited.<ext>  VoiceCut's own result
  voicecut/.voicecut-cache/…    the pipeline's work directory (05_final/final_boundary_plan.json)
  timeline.json                 your edit (autosaved)
  exports/<export_id>/          editor_boundary_plan.json, manifests (the render is deleted once published)
```

## Development

```bash
# Terminal 1: the API only
voicecut-editor --dev --no-browser

# Terminal 2: Vite with hot reload; /api is proxied to the backend
cd frontend && npm run dev
```

Checks:

```bash
.venv/bin/python -m pytest -q        # FFmpeg must be on PATH
ruff check src tests && ruff format --check src tests
cd frontend && npm run typecheck && npm test && npm run build
```

Python tests cover proposals and timeline validation, piece derivation (an
untouched timeline reproduces Voice Cut's output bit for bit; trimming next to
a pause keeps it; resized and inserted pauses are clean room tone and change
the length by exactly their own; range renders equal full renders), the
conversion of schema-2 edits, waveform peaks, projects, the preview copy's orientation
for rotated and mirrored phone video, audio and video exports, and the HTTP
API with a stand-in pipeline worker. Frontend tests cover the layout against
the Python fixture, edits (pauses survive trims and splits, deleting a clip
between two pauses keeps the longer one), undo history, waveform levels, the player's
sample-exact scheduling, the time stretcher (duration follows the rate,
pitch and stereo alignment are kept), and the themes (System follows the
system; both palettes have every colour and keep text and timeline marks at
readable contrast). None of them download models or call planner APIs.

## Limitations

- One recording per project; no multi-camera, titles or effects — the editor
  exists to correct Voice Cut's cut.
- Clips you move or copy are kept only until the next Voice Cut, which edits
  the recording again in recording order.
- The clipboard holds clips of this project only, inside VoiceCut.
- Pauses are for audio projects; video edits are direct cuts.
- Manual cuts are not checked against the alignment.
- Running Voice Cut again with other settings replaces the edit (undo history
  starts over); only what you deleted stays out.
- Undo history lives in the browser tab; the saved edit survives reloads, the
  history does not.
- The preview copy of a long 4K video takes a while to build (hardware
  transcoding is used on Apple Silicon); audio editing works meanwhile.
