# Planner benchmark

Which model should choose the takes? We ran VoiceCut's take selection with
many models on the same real recording and compared each edit with the one its
author made by hand. This page collects the results measured so far.

## How it is measured

- **Recording:** one 64-minute English voice-over for a YouTube video, read
  from a script, with many restarts, stumbles and re-read paragraphs. It was
  transcribed once (8,449 words) and every planner reads the same transcript;
  planners that hear audio also get the recording.
- **Reference:** the author's final edit, checked by ear. Speech the author
  removed as an editorial choice rather than as a retake (126 seconds) is not
  counted for anyone.
- **Score:** the mean of two F1 scores, one on the words that should be cut and
  one on the words that should stay. 1.000 is the author's own cut. For scale:
  cutting everything scores 0.36, keeping everything 0.30, cutting every other
  word 0.50.
- **Time:** take selection only, per 10 minutes of recording. Transcription
  and rendering are the same for every planner. Cloud times depend on how busy
  the provider was.
- The author's edit began from a plan by Gemini 3.1 Pro with the audio, so that
  planner is left out, and Gemini models may have a small head start.

## The whole recording

"Left in" is speech the author cut that the planner kept (abandoned takes,
repeats); "cut by mistake" is speech the author kept that the planner cut.
Both are minutes of the 64-minute recording.

| Model | Runs on | Score | Time per 10 min | Left in | Cut by mistake |
| --- | --- | ---: | ---: | ---: | ---: |
| Gemini 3.8 Flash, hears the audio | Gemini API | **0.974** | 3.0 min | 0.6 min | 0.5 min |
| Gemini 3.8 Flash | Gemini API | **0.964** | 4.1 min | 0.7 min | 0.8 min |
| Gemini 3.1 Pro | OpenRouter | **0.955** | 4.4 min | 0.9 min | 0.8 min |
| Qwen 3.8 27B, thinking | OpenRouter | **0.950** | 25 min | 1.2 min | 0.9 min |
| GLM-5.3, low thinking | OpenRouter | **0.923** | 1.1 min | 1.5 min | 1.4 min |
| Gemma 4 31B (the default) | OpenRouter | **0.910** | 1.3 min | 1.7 min | 1.8 min |
| Gemma 4 31B, 4-bit QAT (the default) | Ollama on an A100 40 GB | **0.903** | 2.8 min | 2.2 min | 1.6 min |
| | Ollama on an M1 Max 64 GB | the same model | about 16 min | | |
| Gemma 4 31B, thinking | OpenRouter | **0.903** | 1.4 min | 2.0 min | 1.8 min |
| Gemma 4 31B | Gemini API | **0.895** | 2.7 min | 2.6 min | 1.6 min |
| Qwen 3.8 27B, low thinking | OpenRouter | **0.894** | 2.7 min | 2.4 min | 1.8 min |
| Qwen 3.8 27B, low thinking, 4-bit | Ollama on an A100 40 GB | **0.887** | 10 min¹ | 2.2 min | 2.2 min |
| Qwen 3.6 35B A3B, thinking | OpenRouter | **0.852** | 4.8 min | 3.7 min | 2.2 min |
| Qwen 3.6 35B A3B, low thinking | OpenRouter | **0.844** | 3.3 min | 3.4 min | 2.8 min |
| Qwen 3.5 122B A10B | OpenRouter | **0.773** | 1.0 min | 6.4 min | 2.7 min |

¹ Through Ollama's Vulkan backend, which is slower than CUDA.

**No edit.** These models kept answering with plans that break VoiceCut's
checks (overlapping or out-of-place word ranges, broken JSON), so no edit came
out: DeepSeek V4.1 Flash (with and without thinking), Gemini 3.5 Flash-Lite,
Gemma 4 26B A4B, Granite 4.0 H Micro and 4.2 8B, Ministral 8B and 14B,
Nemotron 3 Nano 30B A3B and 3.5 Lightning, Phi-4 (its 16k context is too
short), Qwen3 Coder 30B A3B, Qwen3 Coder Next, Qwen 3.5 9B, and Qwen 3.6 35B
A3B and Qwen 3.8 27B without thinking. With the checks relaxed, the small ones
that then finish score 0.48–0.76. Ollama 0.40.8 could not start Gemma 4 26B
A4B at all.

## Decision models

VoiceCut 0.4 can also plan with decision models: models that answer typed
questions with probabilities and never write text, so they cannot break the
answer format. For every transcript line the planner asks which of the next
lines says it again and whether the line is narration at all, and cuts the
lines that are said again or are not narration.

They were measured on two 10-minute parts of the same recording: minutes 0–10,
an easy start, and minutes 30–40, a hard part where 70% of the words have to
go. The first part alone made the small models look as good as Gemma; the
second did not. The language models' rows are the same two parts of their
whole-recording runs above.

| Planner | Type | Runs on | Minutes 0–10 | Minutes 30–40 | Mean | Time per 10 min | Memory |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Gemini 3.8 Flash, hears the audio | LLM | Gemini API | 0.999 | 0.955 | **0.977** | 3.0 min | |
| Qwen 3.8 27B, thinking | LLM | OpenRouter | 0.989 | 0.942 | **0.966** | 25 min | |
| Gemini 3.1 Pro | LLM | OpenRouter | 0.998 | 0.915 | **0.957** | 4.4 min | |
| Gemma 4 31B | LLM | OpenRouter | 0.930 | 0.919 | **0.925** | 1.3 min | |
| Gemma 4 31B, 4-bit QAT | LLM | Ollama on an A100 | 0.938 | 0.853 | **0.895** | 2.8 min; M1 Max about 16 min | 20 GB |
| TypeSafe Jev 1.14 | decision | OpenRouter | 0.950 | 0.828 | **0.889** | about 4 s | |
| Kev 9B | decision | M1 Max 64 GB | 0.924 | 0.784 | **0.854** | 3.0 min | 26 GB; 12 GB at 8 bits² |
| Kev 4B | decision | M1 Max 64 GB | 0.875 | 0.761 | **0.818** | 1.5 min | 14 GB; 7 GB at 8 bits² |
| Ministral 14B, relaxed checks | LLM | OpenRouter | 0.685 | 0.561 | **0.623** | | |

² VoiceCut runs Kev at 8 bits on a Mac with too little memory for its full
weights; the edit stays the same and planning is about 1.5 times slower.

Kev and Jev lag behind Gemma where the speaker re-reads a whole paragraph in
other words: the last fragment of an abandoned attempt survives, because the
line that replaces it comes 15–30 lines later and is worded differently.

Other open decision models were tried on the first 10 minutes only, where Kev
9B scored 0.924–0.942: Decider 12B 0.910, Open-Jev 9B 0.888, JEV-9B 0.886,
SemIf 0.768, Open-Jev 2B 0.731, Decider 4B 0.706, Kev 0.8B 0.555, Decider 2B
0.392, and the Laya encoders 0.32–0.46. VoiceCut does not offer them.

## Which planner to pick

- **Gemma 4 31B on your own computer, the default,** where it runs well: a Mac
  with Apple Silicon and 32 GB of memory or more, or an NVIDIA GPU with 24 GB.
  Nothing leaves the computer. About 3 minutes per 10 minutes of recording on
  an A100, about 16 on an M1 Max.
- **Kev 9B or Kev 4B** on a Mac with less memory. Five to ten times faster than
  Gemma on the same Mac, but they leave more retakes in on hard passages, so
  listen through the cut.
- **Gemma 4 31B through OpenRouter** on any computer: the same model, about a
  minute per 10 minutes of recording. TypeSafe Jev takes seconds and plans
  about as well as local Gemma, but it is a closed model behind an alpha API.
- **Gemini 3.8 Flash with the audio** came closest to the hand edit.
- **Language models of 14B parameters or less** are not worth it here: most
  cannot hold the plan's format, and the rest leave many retakes in.

## Caveats

- One recording, one speaker, English. Another recording may rank models
  differently.
- Each model ran once, so differences of about 0.01 mean little.
- Decision models keep or cut whole lines (speech between pauses), so they
  cannot cut a restart in the middle of a line. The best score possible that
  way is 0.975 on this recording.
- Only two models ran as local 4-bit builds, Gemma 4 31B and Qwen 3.8 27B.
  The other open models ran through cloud providers; a local 4-bit build of
  one may plan a little differently.
