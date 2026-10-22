# VoiceCut editor frontend

The browser side of `voicecut-editor`: React 19, TypeScript (strict) and Vite,
no editing framework. The production build goes to
`src/voicecut/editor/static/`, which the Python server serves.

## Commands

```bash
cd frontend
npm install
npm run dev          # Vite on http://localhost:5173, /api proxied to http://127.0.0.1:8765
npm run typecheck    # tsc --noEmit
npm test             # vitest in Node
npm run build        # typecheck + vite build → ../src/voicecut/editor/static
```

For development run the backend with `voicecut-editor --dev` (API only).

## Layout

```
src/types.ts               JSON exchanged with the backend
src/api.ts                 fetch wrappers, upload with progress, job event stream
src/model/timeline.ts      the editable timeline: clips and pauses, inserts, deletes, resizes
src/model/segments.ts      clip edges: trims, splits, origins, snapping
src/model/layout.ts        port of the exporter's piece layout (where every clip lands)
src/model/history.ts       undo/redo over immutable snapshots
src/model/words.ts         transcript lookups
src/model/caption.ts       subtitle blocks (one clip at a time) and their pages
src/store.ts               EditorStore: history + drag drafts, playhead, autosave, playback
src/audio/player.ts        streams rendered output chunks onto an AudioContext
src/audio/stretch.ts       pitch-preserving time stretch (WSOLA) for 0.5×–2.5× preview
src/video/videoSync.ts     two <video> decks driven by the audio clock
src/timeline/view.ts       canvas timeline: drawing and pointer gestures
src/timeline/peaks.ts      waveform levels (base + derived + fetched fine tiles)
src/components/*           start page, editor shell, viewer, transcript, transport,
                           planner/model/language controls, settings (API keys),
                           Voice Cut progress panel, export dialog
tests/*.test.ts            vitest suites; fixtures/layout_cases.json comes from
                           tests/test_editor_plan.py
```

See [docs/editor.md](../docs/editor.md) for how the pieces fit together.
