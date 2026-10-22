/** Typed client of the editor backend; every path is same-origin `/api/...`. */
import type {
  EditorConfig,
  JobEvent,
  ModelList,
  ProjectState,
  ProjectSummary,
  RunSettings,
  SettingsState,
  WireTimeline,
} from './types';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function failure(response: Response): Promise<ApiError> {
  let message = `${response.status} ${response.statusText}`;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === 'string') message = body.detail;
    else if (body.detail !== undefined) message = JSON.stringify(body.detail);
  } catch {
    // keep the status line
  }
  return new ApiError(response.status, message);
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) throw await failure(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

const project = (id: string) => `/api/projects/${encodeURIComponent(id)}`;

export const api = {
  config: () => call<EditorConfig>('/api/config'),
  projects: () => call<ProjectSummary[]>('/api/projects'),
  project: (id: string) => call<ProjectState>(project(id)),
  deleteProject: (id: string) => call<void>(project(id), { method: 'DELETE' }),
  importPath: (path: string) => call<ProjectState>('/api/projects/import', json('POST', { path })),
  locateSource: (id: string, path: string) => call<ProjectState>(`${project(id)}/locate-source`, json('POST', { path })),

  // `.vc` documents (desktop app only).
  createDocument: (source: string, path: string) =>
    call<ProjectState>('/api/documents/create', json('POST', { source, path })),
  saveDocumentAs: (id: string, path: string) => call<ProjectState>(`${project(id)}/save-as`, json('POST', { path })),

  /** Upload a file with progress (fetch cannot report upload progress). */
  importFile(file: File, onProgress: (fraction: number) => void): Promise<ProjectState> {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open('POST', '/api/projects/import');
      request.responseType = 'json';
      request.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      request.onload = () => {
        if (request.status >= 200 && request.status < 300) resolve(request.response as ProjectState);
        else {
          const detail = (request.response as { detail?: unknown } | null)?.detail;
          reject(new ApiError(request.status, typeof detail === 'string' ? detail : request.statusText));
        }
      };
      request.onerror = () => reject(new ApiError(0, 'the upload failed'));
      const form = new FormData();
      form.append('file', file, file.name);
      request.send(form);
    });
  },

  saveTimeline: (id: string, timeline: WireTimeline) =>
    call<{ revision: string; output_frames: number }>(`${project(id)}/timeline`, json('PUT', timeline)),

  async render(id: string, revision: string, start: number, frames: number, signal?: AbortSignal) {
    const query = new URLSearchParams({ rev: revision, start: String(start), frames: String(frames) });
    const response = await fetch(`${project(id)}/render?${query}`, signal ? { signal } : {});
    if (!response.ok) throw await failure(response);
    const channels = Number(response.headers.get('X-Channels'));
    const count = Number(response.headers.get('X-Frames'));
    const data = new Float32Array(await response.arrayBuffer());
    const planar: Float32Array[] = [];
    for (let channel = 0; channel < channels; channel += 1) {
      planar.push(data.subarray(channel * count, (channel + 1) * count));
    }
    return planar;
  },

  async peaks(url: string): Promise<Int16Array> {
    const response = await fetch(url);
    if (!response.ok) throw await failure(response);
    return new Int16Array(await response.arrayBuffer());
  },

  async finePeaks(id: string, spp: number, start: number, count: number): Promise<Int16Array> {
    const query = new URLSearchParams({ spp: String(spp), start: String(start), count: String(count) });
    const response = await fetch(`${project(id)}/peaks/fine?${query}`);
    if (!response.ok) throw await failure(response);
    return new Int16Array(await response.arrayBuffer());
  },

  settings: () => call<SettingsState>('/api/settings'),
  saveKey: (backend: string, key: string) =>
    call<SettingsState>(`/api/settings/keys/${encodeURIComponent(backend)}`, json('PUT', { key })),
  models: (backend: string, refresh = false) =>
    call<ModelList>(`/api/planners/${encodeURIComponent(backend)}/models${refresh ? '?refresh=true' : ''}`),
  saveSettings: (id: string, settings: RunSettings) =>
    call<RunSettings>(`${project(id)}/settings`, json('PUT', settings)),

  runVoiceCut: (id: string, settings: Partial<RunSettings>) =>
    call<{ job_id: string }>(`${project(id)}/voicecut`, json('POST', settings)),
  cancelVoiceCut: (id: string) => call<{ status: string }>(`${project(id)}/cancel`, { method: 'POST' }),
  prepare: (id: string) => call<{ job_id: string | null }>(`${project(id)}/prepare`, { method: 'POST' }),

  startExport: (id: string, body: { timeline: WireTimeline; destination: string; overwrite: boolean }) =>
    call<{ job_id: string; export_id: string }>(`${project(id)}/export`, json('POST', body)),
  revealExport: (id: string, exportId: string) =>
    call<{ ok: boolean }>(`${project(id)}/exports/${encodeURIComponent(exportId)}/reveal`, { method: 'POST' }),
  downloadUrl: (id: string, exportId: string) =>
    `${project(id)}/exports/${encodeURIComponent(exportId)}/download`,
};

export function isTerminal(event: JobEvent): boolean {
  return event.type === 'complete' || event.type === 'error' || event.type === 'cancelled';
}

/**
 * Follow a job's events.  The server replays everything from `since`, so a
 * dropped connection resumes exactly where it stopped.  Returns `unsubscribe`.
 */
export function followJob(jobId: string, onEvent: (event: JobEvent) => void): () => void {
  let received = 0;
  let source: EventSource | null = null;
  let timer: number | undefined;
  let closed = false;
  const open = () => {
    if (closed) return;
    source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events?since=${received}`);
    source.onmessage = (message: MessageEvent<string>) => {
      let event: JobEvent;
      try {
        event = JSON.parse(message.data) as JobEvent;
      } catch {
        return;
      }
      received += 1;
      onEvent(event);
      if (isTerminal(event)) close();
    };
    source.onerror = () => {
      source?.close();
      source = null;
      if (closed) return;
      // A restarted server no longer knows the job: report it instead of
      // reconnecting forever.
      fetch(`/api/jobs/${encodeURIComponent(jobId)}`)
        .then((response) => {
          if (response.status === 404) {
            onEvent({ type: 'error', message: 'the editor server restarted; the job was lost' });
            close();
          } else if (!closed) {
            timer = window.setTimeout(open, 1000);
          }
        })
        .catch(() => {
          if (!closed) timer = window.setTimeout(open, 1000);
        });
    };
  };
  const close = () => {
    closed = true;
    source?.close();
    window.clearTimeout(timer);
  };
  open();
  return close;
}
