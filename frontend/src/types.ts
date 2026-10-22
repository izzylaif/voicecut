/** JSON exchanged with the editor backend (src/voicecut/editor/server.py). */

export type Origin = 'voicecut' | 'manual' | 'source';

/** One timeline clip as stored and sent to the server (canonical samples). */
export interface WireClip {
  kind: 'clip';
  id: string;
  source_start_sample: number;
  source_end_sample: number;
  start_origin: Origin;
  end_origin: Origin;
}

/** VoiceCut's pauses: "lead", "tail" or the index of a join. */
export type PauseRef = number | 'lead' | 'tail';

/** A pause of room tone between clips (or before the first / after the last). */
export interface WirePause {
  kind: 'pause';
  id: string;
  /** Samples heard between its crossfades with the clips beside it. */
  length: number;
  origin: 'voicecut' | 'manual';
  join: PauseRef | null;
}

export type WireItem = WireClip | WirePause;

export interface WireTimeline {
  schema_version: 3;
  proposal_sha256: string | null;
  items: WireItem[];
}

export interface RoomTone {
  length: number;
  fade_in: number;
  fade_out: number;
}

export interface ProposalSegment {
  id: string;
  start: number;
  end: number;
  fade_in: number;
  fade_out: number;
}

export interface Word {
  text: string;
  start: number;
  end: number;
  kept: boolean;
}

/** VoiceCut's result, exactly as much as the layout needs. */
export interface Proposal {
  plan_sha256: string;
  pause_policy: 'semantic' | 'cuts';
  sample_rate: number;
  source_frames: number;
  output_frames: number;
  segments: ProposalSegment[];
  /** Room tone VoiceCut inserted at join i (between segments i and i + 1). */
  joins: (RoomTone | null)[];
  lead: RoomTone | null;
  tail: RoomTone | null;
  words: Word[];
}

export interface Filmstrip {
  url: string;
  interval: number;
  count: number;
  thumb_width: number;
  thumb_height: number;
  columns: number;
  rows: number;
  sheets: number;
}

export interface VideoState {
  status: 'pending' | 'preparing' | 'ready' | 'failed';
  progress: number | null;
  error: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  /** Proxy time of canonical audio time 0. */
  offset: number;
  proxy_url: string | null;
  filmstrip: Filmstrip | null;
}

export interface MediaState {
  kind: 'audio' | 'video';
  sample_rate: number;
  channels: number;
  frames: number;
  duration: number;
  peaks: { url: string; spp: number; bins: number };
  video: VideoState | null;
}

export interface RunSettings {
  language: string;
  planner_backend: string;
  planner_model: string | null;
  breath_cleanup: 'off' | 'replace' | null;
}

export interface RunState {
  status: 'idle' | 'running' | 'complete' | 'failed' | 'cancelled';
  job_id: string | null;
  work_dir: string | null;
  output: string | null;
  error: string | null;
  warnings: string[];
  started_at: string | null;
  finished_at: string | null;
}

export interface ExportRecord {
  export_id: string;
  status: string;
  output_path: string;
  output_kind: 'audio' | 'video';
  created_at: string;
  duration_seconds: number;
  reused_voicecut_output: boolean;
  manual_boundaries: number;
  kept_pauses: number;
  dropped_pauses: number;
  inserted_pauses: number;
}

export interface ProjectSummary {
  id: string;
  name: string;
  kind: 'audio' | 'video';
  created_at: string;
  run_status: RunState['status'];
  source_path: string;
}

export interface ProjectState extends ProjectSummary {
  uploaded: boolean;
  /** The source recording is not where the project says (moved or deleted). */
  source_missing: boolean;
  /** The `.vc` document this project is, in the desktop app. */
  document: { path: string; name: string } | null;
  media: MediaState;
  settings: RunSettings;
  run: RunState;
  proposal: Proposal | null;
  proposal_error: string | null;
  source_items: WireItem[];
  timeline: WireTimeline | null;
  exports: ExportRecord[];
  default_export_path: string;
  jobs: Partial<Record<'voicecut' | 'export' | 'prepare', string>>;
}

export interface EditorConfig {
  version: string;
  desktop: boolean;
  languages: string[];
  planner_backends: string[];
  defaults: RunSettings;
}

export type StageStatus = 'started' | 'completed' | 'cached' | 'skipped' | 'failed';

export type JobEvent =
  | { type: 'stage'; stage: string; status: StageStatus; label: string; at?: number }
  | { type: 'progress'; stage: string; fraction: number; at?: number }
  | { type: 'log'; stream: 'stdout' | 'stderr'; line: string; at?: number }
  | { type: 'complete'; result: Record<string, unknown>; at?: number }
  | { type: 'error'; message: string; at?: number }
  | { type: 'cancelled'; at?: number };

export interface PlannerInfo {
  id: string;
  label: string;
  listens_to_audio: boolean;
  default_model: string;
  key_env: string | null;
  key_page: string | null;
  key: { set: boolean; masked: string | null; source: 'environment' | 'env_file' | 'keychain' | 'credential_store' | null } | null;
  available: boolean;
}

export interface SettingsState {
  env_file: string;
  /** Where saved keys live: the dotenv file, or the macOS Keychain (absent from older servers). */
  key_store?: { kind: 'env_file' | 'keychain' | 'credential_store'; location: string };
  planners: PlannerInfo[];
  default_models: Record<string, string>;
  languages: string[];
  defaults: RunSettings;
}

export interface ModelInfo {
  id: string;
  label: string;
  detail: string | null;
  alias: boolean;
  default: boolean;
  /** Local (Ollama) models only: */
  downloaded?: boolean;
  size_gb?: number | null;
  /** How Ollama runs it: MLX (Apple Silicon) or GGUF (llama.cpp). */
  engine?: string | null;
  recommended?: boolean;
  /** Whether the model can reason before answering (null: not known yet). */
  can_think?: boolean | null;
  /** How it reasons in a Voice Cut (a setting of the model): off, on, or a level. */
  think?: Think;
  /** The settings it takes (null: it cannot reason). */
  think_options?: Think[] | null;
  pull?: PullState | null;
}

export type Think = boolean | 'low' | 'medium' | 'high';

/** A model download in progress (or one that failed). */
export interface PullState {
  status: string;
  completed: number;
  total: number;
  fraction: number | null;
  error: string | null;
  done: boolean;
}

/** Where the editor's Ollama runs. */
export interface OllamaStatus {
  available: boolean;
  url: string | null;
  version: string | null;
  /** `voicecut`: the copy VoiceCut installed and started; `system`: the user's. */
  source: 'system' | 'voicecut' | null;
  message: string | null;
}

export interface ModelList {
  models: ModelInfo[];
  default: string;
  error: { kind: 'missing_key' | 'invalid_key' | 'network' | 'unavailable'; message: string } | null;
  /** Local models only. */
  ollama?: OllamaStatus;
  /** This computer's memory and what a recommended local model needs. */
  memory?: { gb: number | null; needed_gb: number };
}
