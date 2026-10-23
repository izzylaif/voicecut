import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, CircleHelp, Download, FileAudio, FileVideo, Settings, Sparkles } from 'lucide-react';
import { api, followJob } from '../api';
import { confirmAction, desktop, documentOpened, editableTarget, editText, invoke, onMenu } from '../desktop';
import { findLocalModel, useLocalModels } from '../localModels';
import { EditorStore, errorText } from '../store';
import { Timeline } from '../timeline/Timeline';
import type { ProjectState, RunSettings, SettingsState } from '../types';
import { useStore } from '../useStore';
import { ExportDialog } from './ExportDialog';
import { reduceProgress, RunPanel, startProgress, type JobProgress } from './RunPanel';
import { SettingsDialog } from './SettingsDialog';
import { Transcript } from './Transcript';
import { Transport } from './Transport';
import { UpdateButton } from './UpdateButton';
import { Viewer } from './Viewer';
import { downloadLocalModel, modelOf, plannerOf, VoiceCutControls } from './VoiceCutControls';

const mod = navigator.platform.toLowerCase().includes('mac') ? '⌘' : 'Ctrl+';

const SHORTCUTS: [string, string][] = [
  ['Space', 'Play / pause'],
  ['< / >', 'Slower / faster preview (0.5× to 2.5×, same pitch)'],
  ['Drag a clip edge', 'Trim or extend it; later clips follow'],
  ['⌥ while dragging an edge', 'Ignore snapping'],
  ['Drag a clip', 'Move it (and the other selected clips) to another cut'],
  ['⌥ while dragging a clip', 'Drop a copy instead'],
  ['⇧-click', 'Select every clip and pause from the last one clicked'],
  [mod + 'click', 'Select one more, or one less'],
  [mod + 'A', 'Select everything'],
  [mod + 'C / ' + mod + 'X', 'Copy / cut the selected clips'],
  [mod + 'V', 'Paste them at the playhead (splits a clip)'],
  [mod + 'D', 'Duplicate the selected clips after them'],
  ['S or ' + mod + 'B', 'Split at the playhead'],
  ['P', 'Insert a 1 s pause at the playhead (splits a clip)'],
  ['Drag a pause edge', 'Make the pause longer or shorter'],
  ['Right-click', 'Pause, split and delete at that spot'],
  ['Delete', 'Delete the selected clips or pauses'],
  [mod + 'Z / ⇧' + mod + 'Z', 'Undo / redo'],
  ['⌥[ / ⌥]', 'Trim the clip under the playhead to the playhead'],
  ['← / →', 'Move the playhead 10 ms (⇧: 1 s)'],
  ['↑ / ↓', 'Previous / next cut'],
  [mod + '+ / ' + mod + '−, pinch', 'Zoom'],
  ['⇧Z', 'Fit the whole edit'],
  ['N', 'Snapping on / off'],
];

export function Editor({ initial, onClose }: { initial: ProjectState; onClose: () => void }) {
  const [store] = useState(() => new EditorStore(initial));
  useStore(store);
  const [run, setRun] = useState<JobProgress | null>(null);
  const [exporting, setExporting] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [settings, setSettings] = useState<RunSettings>(initial.settings);
  const [settingsState, setSettingsState] = useState<SettingsState | null>(null);
  // The local model the Voice Cut button is downloading: Voice Cut runs once it is there.
  const [awaitedModel, setAwaitedModel] = useState<string | null>(null);
  const localModels = useLocalModels(false);
  const project = store.project;

  useEffect(() => () => store.dispose(), [store]);

  // In the desktop app this window shows the project's document.
  useEffect(() => {
    if (initial.document) documentOpened(initial.id, initial.document.path, initial.document.name);
  }, [initial]);

  useEffect(() => {
    api.settings().then(setSettingsState, (error: unknown) => store.notify(`Cannot load settings: ${errorText(error)}`, 'error'));
  }, [store]);

  /** Change the Voice Cut settings and remember them for this project. */
  const changeSettings = useCallback(
    (next: RunSettings) => {
      setSettings(next);
      api.saveSettings(store.id, next).catch((error: unknown) => store.notify(errorText(error), 'error'));
    },
    [store],
  );

  const refresh = useCallback(async () => {
    try {
      store.updateProject(await api.project(store.id));
    } catch (error) {
      store.notify(`Cannot refresh the project: ${errorText(error)}`, 'error');
    }
  }, [store]);

  const followRun = useCallback(
    (jobId: string) => {
      setRun(startProgress(jobId));
      return followJob(jobId, (event) => {
        setRun((current) => (current ? reduceProgress(current, event) : current));
        if (event.type === 'complete') {
          void refresh().then(() => store.notify('Voice Cut’s edit is on the timeline'));
        } else if (event.type === 'error' || event.type === 'cancelled') {
          void refresh();
        }
      });
    },
    [refresh, store],
  );

  // Follow jobs that were already running when the project was opened.
  useEffect(() => {
    const stops: (() => void)[] = [];
    if (initial.jobs.voicecut) stops.push(followRun(initial.jobs.voicecut));
    return () => stops.forEach((stop) => stop());
  }, [initial, followRun]);

  const prepareJob = project.jobs.prepare;
  const videoStatus = project.media.video?.status;
  useEffect(() => {
    if (!prepareJob || videoStatus === 'ready' || videoStatus === 'failed') return;
    let lastProgress = 0;
    return followJob(prepareJob, (event) => {
      if (event.type === 'progress' && store.project.media.video) {
        const now = performance.now();
        if (now - lastProgress < 250) return;
        lastProgress = now;
        store.project = {
          ...store.project,
          media: { ...store.project.media, video: { ...store.project.media.video, status: 'preparing', progress: event.fraction } },
        };
        store.changed();
      } else if (event.type === 'complete' || event.type === 'error') {
        void refresh();
      }
    });
  }, [prepareJob, videoStatus, refresh, store]);

  const startVoiceCut = async () => {
    const planner = settingsState ? plannerOf(settings, settingsState.planners) : undefined;
    if (planner?.key_env && !planner.key?.set) {
      store.notify(`Add your ${planner.label} API key first`, 'error');
      setSettingsOpen(true);
      return;
    }
    const local = planner?.id === 'ollama' ? findLocalModel(modelOf(settings, settingsState)) : undefined;
    if (local && !local.downloaded) {
      if ((local.pull && !local.pull.done) || (await downloadLocalModel(local, { forVoiceCut: true }))) {
        setAwaitedModel(local.id);
        store.notify(`Downloading ${local.label || local.id}: Voice Cut starts when it is ready`);
      }
      return;
    }
    if (store.proposal && store.isEdited) {
      const ok = await confirmAction(
        'Voice Cut edits the recording again, in recording order, leaving out what you deleted. Your other manual edits, and clips you moved or copied, are replaced with a new automatic edit. Continue?',
        { title: 'Voice Cut again?', ok: 'Voice Cut' },
      );
      if (!ok) return;
    }
    try {
      const { job_id } = await api.runVoiceCut(store.id, settings, store.wireTimeline());
      followRun(job_id);
    } catch (error) {
      store.notify(`Cannot start Voice Cut: ${errorText(error)}`, 'error');
    }
  };

  useEffect(() => {
    if (!awaitedModel) return;
    const planner = settingsState ? plannerOf(settings, settingsState.planners) : undefined;
    const model = findLocalModel(awaitedModel);
    if (planner?.id !== 'ollama' || modelOf(settings, settingsState) !== awaitedModel || model?.pull?.error) {
      setAwaitedModel(null); // another planner was chosen, or the download failed (the picker says why)
    } else if (model?.downloaded) {
      setAwaitedModel(null);
      void startVoiceCut();
    }
    // startVoiceCut is a new function every render; the download decides when it runs.
  }, [awaitedModel, localModels.list, settings, settingsState]);

  const cancelVoiceCut = () => {
    api.cancelVoiceCut(store.id).catch((error: unknown) => store.notify(errorText(error), 'error'));
  };

  /** Save the edit now (it is also saved after every change). */
  const saveNow = useCallback(async () => {
    try {
      await store.flushSave();
      store.notify('Saved');
    } catch (error) {
      store.notify(`Not saved: ${errorText(error)}`, 'error');
    }
  }, [store]);

  /** Desktop: copy the document elsewhere and keep editing the copy. */
  const saveAs = useCallback(async () => {
    const document = store.project.document;
    if (!document) return;
    try {
      await store.flushSave().catch(() => undefined);
      const folder = document.path.slice(0, document.path.lastIndexOf('/'));
      const path = await invoke<string | null>('choose_document_location', {
        name: `${document.name} copy`,
        directory: folder,
      });
      if (!path) return;
      const state = await api.saveDocumentAs(store.id, path);
      store.updateProject(state);
      if (state.document) {
        documentOpened(state.id, state.document.path, state.document.name);
        store.notify(`Saved as ${state.document.name}`);
      }
    } catch (error) {
      store.notify(`Cannot save: ${errorText(error)}`, 'error');
    }
  }, [store]);

  /** Point the project at its source recording after it was moved. */
  const locateSource = async () => {
    const name = project.source_path.slice(project.source_path.lastIndexOf('/') + 1);
    try {
      const path = await invoke<string | null>('choose_source', { name });
      if (path) store.updateProject(await api.locateSource(store.id, path));
    } catch (error) {
      store.notify(errorText(error), 'error');
    }
  };

  // Keyboard shortcuts.
  const modal = exporting || settingsOpen;
  const modalRef = useRef(modal);
  modalRef.current = modal;

  // The desktop app's menu commands.
  useEffect(
    () =>
      onMenu((message) => {
        const free = !modalRef.current;
        switch (message.command) {
          case 'flush':
            void store
              .flushSave()
              .catch(() => undefined)
              .finally(() => void invoke('flush_done', { token: message.token }));
            break;
          case 'save':
            void saveNow();
            break;
          case 'save-as':
            void saveAs();
            break;
          case 'export':
            setExporting(true);
            break;
          case 'settings':
            setSettingsOpen(true);
            break;
          case 'undo':
            if (!editText('undo') && free) store.undo();
            break;
          case 'redo':
            if (!editText('redo') && free) store.redo();
            break;
          case 'select-all':
            if (!editText('select-all') && free) store.selectAll();
            break;
          case 'zoom-in':
            store.zoom(1.6);
            break;
          case 'zoom-out':
            store.zoom(1 / 1.6);
            break;
          case 'zoom-fit':
            store.zoomToFit();
            break;
          case 'shortcuts':
            setHelpOpen((open) => !open);
            break;
        }
      }),
    [store, saveNow, saveAs],
  );
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (modalRef.current) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT')) return;
      const command = event.metaKey || event.ctrlKey;
      const key = event.key;
      // Letters by physical key, so shortcuts work with any keyboard layout
      // (on a Russian layout S types "ы").
      const code = event.code;
      let handled = true;
      if (key === ' ' || code === 'Space') store.togglePlay();
      else if (command && code === 'KeyZ') event.shiftKey ? store.redo() : store.undo();
      else if (command && code === 'KeyY') store.redo();
      else if (command && code === 'KeyB') store.splitAtPlayhead();
      else if (command && (code === 'Equal' || key === '+' || key === '=')) store.zoom(1.6);
      else if (command && (code === 'Minus' || key === '-')) store.zoom(1 / 1.6);
      else if (command && code === 'KeyA') store.selectAll();
      else if (command && code === 'KeyC') store.copySelection();
      else if (command && code === 'KeyX') store.cutSelection();
      else if (command && code === 'KeyV') store.paste();
      else if (command && code === 'KeyD') store.duplicateSelection();
      else if (command) handled = false;
      else if (key === '<' || (event.shiftKey && code === 'Comma')) store.stepRate(-1);
      else if (key === '>' || (event.shiftKey && code === 'Period')) store.stepRate(1);
      else if (code === 'KeyS') store.splitAtPlayhead();
      else if (code === 'KeyP') store.insertPause();
      else if (key === 'Delete' || key === 'Backspace') store.deleteSelection();
      else if (code === 'BracketLeft' && event.altKey) store.trimToPlayhead('start');
      else if (code === 'BracketRight' && event.altKey) store.trimToPlayhead('end');
      else if (key === 'ArrowLeft') store.nudge(event.shiftKey ? -1 : -0.01);
      else if (key === 'ArrowRight') store.nudge(event.shiftKey ? 1 : 0.01);
      else if (key === 'ArrowUp') store.jumpEdit(-1);
      else if (key === 'ArrowDown') store.jumpEdit(1);
      else if (key === 'Home') store.setPlayhead(0);
      else if (key === 'End') store.setPlayhead(store.layout.total);
      else if (code === 'KeyZ' && event.shiftKey) store.zoomToFit();
      else if (code === 'KeyN') store.toggleSnapping();
      else if (key === 'Escape') store.select(null);
      else if (key === '?' || (event.shiftKey && code === 'Slash')) setHelpOpen((open) => !open);
      else handled = false;
      if (handled) event.preventDefault();
    };
    // Edit › Copy, Cut and Paste of the desktop app's menu arrive as clipboard events.
    const onClipboard = (event: ClipboardEvent) => {
      if (modalRef.current || editableTarget()) return;
      event.preventDefault();
      if (event.type === 'copy') store.copySelection();
      else if (event.type === 'cut') store.cutSelection();
      else store.paste();
    };
    window.addEventListener('keydown', onKey);
    for (const type of ['copy', 'cut', 'paste'] as const) document.addEventListener(type, onClipboard);
    return () => {
      window.removeEventListener('keydown', onKey);
      for (const type of ['copy', 'cut', 'paste'] as const) document.removeEventListener(type, onClipboard);
    };
  }, [store]);

  // Flush the autosave before the page goes away.
  useEffect(() => {
    const onLeave = () => void store.flushSave().catch(() => undefined);
    window.addEventListener('pagehide', onLeave);
    return () => window.removeEventListener('pagehide', onLeave);
  }, [store]);

  const running = run?.status === 'running';
  const save = store.save;

  return (
    <div className="editor">
      <header className="topbar">
        <div className="topbar-left">
          {!desktop && (
            <button type="button" className="icon-button" title="All projects" onClick={onClose}>
              <ArrowLeft size={16} />
            </button>
          )}
          {project.media.kind === 'video' ? <FileVideo size={16} /> : <FileAudio size={16} />}
          <span className="project-name" title={project.source_path}>
            {project.document ? (
              <>
                {project.document.name} <span className="muted">· {project.name}</span>
              </>
            ) : (
              project.name
            )}
          </span>
          <span className={`save-state save-${save.state}`} title={save.state === 'error' ? save.message : undefined}>
            {save.state === 'saved'
              ? 'Saved'
              : save.state === 'error'
                ? 'Not saved — retrying on the next change'
                : 'Saving…'}
          </span>
        </div>
        <div className="topbar-right">
          <UpdateButton />
          <button type="button" className="icon-button" title="Keyboard shortcuts (?)" onClick={() => setHelpOpen((open) => !open)}>
            <CircleHelp size={16} />
          </button>
          <VoiceCutControls
            settings={settings}
            state={settingsState}
            disabled={running}
            onChange={changeSettings}
            onOpenSettings={() => setSettingsOpen(true)}
          />
          <button type="button" className="button accent" disabled={running} onClick={() => void startVoiceCut()}>
            <Sparkles size={14} /> {store.proposal ? 'Voice Cut again' : 'Voice Cut'}
          </button>
          <button type="button" className="icon-button" title="Settings: appearance, API keys and Voice Cut options" onClick={() => setSettingsOpen(true)}>
            <Settings size={16} />
          </button>
          <button type="button" className="button primary" onClick={() => setExporting(true)}>
            <Download size={14} /> Export
          </button>
        </div>
      </header>

      {project.source_missing && (
        <div className="banner">
          The original recording {project.source_path} was moved or deleted.{' '}
          {project.media.kind === 'video'
            ? 'Find it to run Voice Cut or to export video; the edit and audio export work without it.'
            : 'Find it to run Voice Cut again; the edit and export work without it.'}
          {desktop && (
            <button type="button" className="button subtle banner-action" onClick={() => void locateSource()}>
              Locate…
            </button>
          )}
        </div>
      )}

      {project.proposal_error && (
        <div className="banner">
          This project’s earlier Voice Cut result cannot be edited ({project.proposal_error}). Run Voice Cut again.
        </div>
      )}

      <main className={`stage-area${project.media.kind === 'video' ? ' with-video' : ''}`}>
        <Viewer store={store} />
        <aside className="side">
          {run && (
            <RunPanel
              progress={run}
              onCancel={cancelVoiceCut}
              onClose={() => setRun(null)}
              onRetry={() => void startVoiceCut()}
            />
          )}
          <Transcript store={store} />
        </aside>
      </main>

      <Transport store={store} />
      <Timeline store={store} />

      {store.message && <div className={`toast toast-${store.message.tone}`}>{store.message.text}</div>}
      {exporting && <ExportDialog store={store} onClose={() => setExporting(false)} />}
      {settingsOpen && settingsState && (
        <SettingsDialog
          state={settingsState}
          settings={settings}
          onState={setSettingsState}
          onSettings={changeSettings}
          onClose={() => setSettingsOpen(false)}
        />
      )}
      {helpOpen && (
        <div className="popover help" onClick={() => setHelpOpen(false)}>
          <h3>Keyboard</h3>
          <dl>
            {SHORTCUTS.map(([keys, action]) => (
              <div key={keys}>
                <dt>{keys}</dt>
                <dd>{action}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </div>
  );
}
