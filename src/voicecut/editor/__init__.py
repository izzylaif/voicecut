"""Browser-based VoiceCut editor.

Edit model (``timeline``, ``plan``): VoiceCut's proposal, timeline validation,
and the derivation of overlap-add pieces that both the preview and the export
render.  Media (``peaks``, ``proxy``, ``export``): waveform peaks, the upright
video preview, and exports through VoiceCut's own renderer and publishers.
Runtime (``projects``, ``jobs``, ``worker``, ``server``, ``cli``): project
storage, background jobs with Server-Sent Events, the pipeline worker
subprocess, the FastAPI server and the ``voicecut-editor`` command.
"""
