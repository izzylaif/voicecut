# VoiceCut promo

A one-minute, motion-designed film about VoiceCut, made entirely in code with
[Remotion](https://www.remotion.dev): no screen recording. Every waveform,
word and cut in it comes from VoiceCut's real run on the example recording
of its website (38.0 s of takes → 23.8 s), and the film plays that
recording's first take and its clean result.

`out/voicecut-promo.mp4` — 1920×1080, 60 fps, about 68 s, sound at −14 LUFS.

## Story

| Scene | Narrator | Picture |
| --- | --- | --- |
| Mess | (the recording itself) then *Real recordings are messy: retakes, false starts, breaths, long pauses.* | Three pieces of the raw take play with live captions and a fast-forward between them: the first take, the same sentence again (tagged *Retake* as it is heard), and *with familiar — with the familiar words* (tagged *False start*). The whole waveform then lights up its retake, false start, 9 breaths and long pauses. |
| Reveal | *VoiceCut does it for you.* | A cut sweeps across; the app icon. |
| App | *Drop in a recording, pick a model — it can run locally, right on your computer — and press Voice Cut.* / *It transcribes everything you said, reads it like an editor, and keeps the best take of every line.* | The editor, rebuilt as motion graphics: the file drops in, Local (Ollama) · Gemma 4 31B is chosen, Voice Cut runs its stages; the transcript types in, the abandoned take and the false start are struck out. |
| Detail | *It cuts only in the silence between words, and turns breaths into your own room tone.* | One pause up close: two cuts fall into silence, the breath flattens into room tone. |
| Result | (the recording, edited) | Removed parts fall away, the clips close up (38.0 s → 23.8 s), then the same passage plays clean: *…such as write a story. And it might begin with the familiar words, once upon a time.* |
| Free | *VoiceCut is free and open source. Run it on your own computer, or with your own key for OpenRouter, Gemini, OpenAI or DeepSeek.* | Free and open source; "On your computer" and "Or with your own key" cards. |
| Outro | *Record with all your retakes. Get back one clean take.* | The tagline, the icon and vectozavr.github.io/voicecut. |

No price is shown anywhere: VoiceCut is free, and the film says so.

## Making it

Only the sources are in git: the code, VoiceCut's plan of the example
(`data/example_en_plan.json`, `data/example_en_take_plan.json`) and its
result (`data/example_en_edited.flac`), the script (`data/script.json`) and
the narrator's takes (`data/vo/`). Everything else is rebuilt: the raw take
is the repository's own `examples/media/audio/example_en.wav`, the sound
effects and the pad are synthesized (`scripts/sfx.py`), and `node_modules`
(with the browser Remotion renders in) comes from `npm install`.

From the repository's checkout, with its `.venv` and `.mfa-env` set up
(`scripts/install.sh`):

```bash
cd ads
npm install
npm run assets     # recordings, narrator lines, effects -> public/, src/data/
npm run studio     # preview and scrub
npm run render     # -> out/voicecut-promo.mp4 (sound at -14 LUFS)
```

The narrator is Gemini TTS (`gemini-2.5-pro-preview-tts`, voice Charon),
played 8% faster. `npm run assets` records a line again only when its text,
the voice or the direction in `data/script.json` changed, which needs
`GEMINI_API_KEY`; otherwise it reuses the takes in `data/vo/`. After
recording, check what was said and update the word timings the visuals
follow (MLX Whisper, on a Mac):

```bash
PATH=../.mfa-env/bin:$PATH ../.venv-mlx/bin/python scripts/verify_vo.py
PATH=../.mfa-env/bin:$PATH ../.venv-mlx/bin/python scripts/vo_words.py
```

Timing lives in `src/timeline.ts`: each scene starts from the narrator's
lines, and the visuals hit the words they name (`said('vo04', 'press')`).
Edit a line in `data/script.json`, record it again and everything follows.
Every sound effect's level is one number, `SFX` in `src/Promo.tsx`. Render
without the pad with `npm run render -- --props='{"music": false}'`.

Remotion is free for individuals and companies of up to three people; larger
companies need its company license.
