// The VoiceCut website: the example players, the download buttons and the
// table of tested models.

const REPOSITORY = "vectozavr/voicecut";
const RELEASES_PAGE = `https://github.com/${REPOSITORY}/releases/latest`;
// The installers of a release, by what their names end with
// (VoiceCut-<version>-universal.dmg and so on; up to 0.2.0 the Mac's was
// -arm64.dmg, for Apple Silicon only).
const ASSETS = {
  mac: /-(universal|arm64)\.dmg$/i,
  windows: /-x64-setup\.exe$/i,
  appimage: /-x86_64\.AppImage$/i,
  deb: /-amd64\.deb$/i,
};
const PLATFORMS = {
  mac: {
    label: "Download for Mac",
    asset: "mac",
    meta: "macOS 14 or later · Apple Silicon or Intel",
  },
  windows: {
    label: "Download for Windows",
    asset: "windows",
    meta: "Windows 10 and 11 · x64",
  },
  linux: {
    label: "Download for Linux",
    asset: "appimage",
    meta: "AppImage · x86_64",
  },
};

// ---------------------------------------------------------------------------
// Example players: one plays at a time, the bar shows and sets the position.

const ICONS = {
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z"/></svg>',
  pause:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1.2"/><rect x="14" y="5" width="4" height="14" rx="1.2"/></svg>',
};

function clock(seconds) {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

const media = [...document.querySelectorAll("audio, video")];

for (const element of media) {
  element.addEventListener("play", () => {
    for (const other of media) {
      if (other !== element && !other.paused) other.pause();
    }
  });
}

for (const track of document.querySelectorAll("[data-player]")) {
  const audio = track.querySelector("audio");
  const button = track.querySelector(".play");
  const bar = track.querySelector("[data-seek]");
  const time = track.querySelector("[data-time]");
  const total = Number(bar.getAttribute("aria-valuemax")) || 0;

  audio.removeAttribute("controls");
  track.classList.add("enhanced");

  const duration = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : total);
  const render = () => {
    const playing = !audio.paused && !audio.ended;
    button.innerHTML = playing ? ICONS.pause : ICONS.play;
    button.setAttribute("aria-pressed", String(playing));
    const length = duration();
    const position = audio.currentTime || 0;
    bar.style.setProperty("--progress", length ? Math.min(1, position / length) : 0);
    bar.setAttribute("aria-valuenow", String(Math.round(position)));
    time.textContent = playing || position > 0 ? `${clock(position)} / ${clock(length)}` : clock(length);
  };
  const seek = (fraction) => {
    const length = duration();
    if (!length) return;
    const target = Math.min(Math.max(fraction, 0), 1) * length;
    if (audio.readyState === 0) {
      audio.addEventListener("loadedmetadata", () => (audio.currentTime = target), { once: true });
      audio.load();
    } else {
      audio.currentTime = target;
    }
    render();
  };

  button.addEventListener("click", () => {
    if (audio.paused || audio.ended) audio.play().catch(() => undefined);
    else audio.pause();
  });
  bar.addEventListener("click", (event) => {
    const box = bar.getBoundingClientRect();
    seek((event.clientX - box.left) / box.width);
  });
  bar.addEventListener("keydown", (event) => {
    const length = duration();
    if (!length) return;
    const step = event.shiftKey ? 10 : 5;
    if (event.key === "ArrowRight") seek((audio.currentTime + step) / length);
    else if (event.key === "ArrowLeft") seek((audio.currentTime - step) / length);
    else if (event.key === "Home") seek(0);
    else if (event.key === "End") seek(1);
    else if (event.key === " " || event.key === "Enter") button.click();
    else return;
    event.preventDefault();
  });
  for (const name of ["play", "pause", "ended", "timeupdate", "loadedmetadata", "seeked"]) {
    audio.addEventListener(name, render);
  }
  render();
}

// ---------------------------------------------------------------------------
// Downloads: find the visitor's system and the latest release's installers.

function detectSystem() {
  const agent = navigator.userAgent || "";
  const platform = navigator.userAgentData?.platform || navigator.platform || "";
  const touch = navigator.maxTouchPoints > 1;
  if (/iPhone|iPad|iPod/i.test(agent) || (/Mac/i.test(platform) && touch)) return "mobile";
  if (/Android/i.test(agent) || /Android/i.test(platform)) return "mobile";
  if (/Mac/i.test(platform) || /Macintosh/i.test(agent)) return "mac";
  if (/Win/i.test(platform) || /Windows/i.test(agent)) return "windows";
  if (/CrOS/i.test(agent)) return "other";
  if (/Linux|X11|BSD/i.test(platform) || /Linux|X11/i.test(agent)) return "linux";
  return "other";
}

// For a release whose Mac app is for Apple Silicon only. Best effort:
// Chromium reports the architecture, other browsers may name the graphics
// card. Safari tells neither, and is then taken as Apple Silicon.
async function looksLikeIntelMac() {
  try {
    const values = await navigator.userAgentData?.getHighEntropyValues?.(["architecture"]);
    if (values?.architecture) return values.architecture === "x86";
  } catch {
    // Not available.
  }
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    const info = gl?.getExtension("WEBGL_debug_renderer_info");
    const renderer = info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : "";
    return /Intel|AMD|Radeon|NVIDIA/i.test(renderer);
  } catch {
    return false;
  }
}

async function fetchJSON(url, timeout) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { signal: controller.signal, headers: { Accept: "application/vnd.github+json" } });
    if (!response.ok) throw new Error(`${url}: ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// The live answer from GitHub, else the copy the site was built with.
async function latestRelease() {
  const sources = [`https://api.github.com/repos/${REPOSITORY}/releases/latest`, "release.json"];
  for (const source of sources) {
    try {
      const release = await fetchJSON(source, 6000);
      if (release && Array.isArray(release.assets) && release.assets.length) return release;
    } catch {
      // Try the next source.
    }
  }
  return null;
}

function findAssets(release) {
  const found = {};
  for (const [key, pattern] of Object.entries(ASSETS)) {
    const asset = release.assets.find((candidate) => pattern.test(candidate.name));
    if (asset) found[key] = asset;
  }
  return found;
}

function megabytes(bytes) {
  const value = bytes / 1_000_000;
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} MB`;
}

function releaseDate(text) {
  const date = new Date(text);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });
}

function showRelease(release, assets) {
  const version = String(release.tag_name || "").replace(/^v/, "");
  const page = release.html_url || RELEASES_PAGE;
  for (const link of document.querySelectorAll("[data-asset]")) {
    const asset = assets[link.dataset.asset];
    if (asset) link.href = asset.browser_download_url;
  }
  const files = {
    mac: [assets.mac],
    windows: [assets.windows],
    linux: [assets.appimage, assets.deb],
  };
  for (const [key, list] of Object.entries(files)) {
    const line = document.querySelector(`[data-asset-file="${key}"]`);
    const present = list.filter(Boolean);
    if (line && present.length) {
      line.textContent = present.map((asset) => `${asset.name} · ${megabytes(asset.size)}`).join("\n");
    }
  }
  const releaseLine = document.querySelector("[data-release-line]");
  if (releaseLine && version) {
    const date = releaseDate(release.published_at);
    releaseLine.innerHTML = "";
    releaseLine.append(`Version ${version}${date ? ` · ${date}` : ""} · `);
    const notes = document.createElement("a");
    notes.href = page;
    notes.textContent = "Release notes";
    releaseLine.append(notes);
  }
  return version;
}

async function setUpDownloads() {
  const system = detectSystem();
  const primary = document.querySelector('[data-download="primary"]');
  const label = primary.querySelector("[data-download-label]");
  const meta = document.querySelector("[data-download-meta]");
  const note = document.querySelector("[data-download-note]");
  const choice = PLATFORMS[system];

  if (choice) {
    label.textContent = choice.label;
    meta.textContent = `Free · ${choice.meta}`;
    document.querySelector(`[data-platform="${system}"]`)?.classList.add("is-current");
  } else {
    // Phones, tablets and anything unknown: show every installer.
    label.textContent = "Download VoiceCut";
    primary.href = "#download";
    meta.textContent =
      system === "mobile"
        ? "VoiceCut runs on Mac, Windows and Linux computers"
        : "Free for macOS, Windows and Linux";
  }

  const release = await latestRelease();
  if (!release) return;
  const assets = findAssets(release);
  const version = showRelease(release, assets);
  const appleSiliconOnly = /-arm64\.dmg$/i.test(assets.mac?.name || "");
  if (appleSiliconOnly) {
    const requirement = document.querySelector('[data-platform="mac"] .platform-req');
    if (requirement) requirement.innerHTML = "macOS 14 Sonoma or later<br />Apple Silicon (M1 or newer)";
    if (system === "mac" && (await looksLikeIntelMac())) {
      note.textContent = `VoiceCut ${version} for Mac needs Apple Silicon (M1 or newer); this Mac seems to have an Intel processor.`;
      note.hidden = false;
    }
  }
  if (choice) {
    const asset = assets[choice.asset];
    if (asset) {
      const details = system === "mac" && appleSiliconOnly ? "macOS 14 or later · Apple Silicon" : choice.meta;
      primary.href = asset.browser_download_url;
      meta.textContent = `Free · ${version ? `Version ${version} · ` : ""}${details}`;
    }
  }
}

setUpDownloads();

// ---------------------------------------------------------------------------
// Tested models: the buttons above the table show one kind of planner.

for (const bench of document.querySelectorAll("[data-bench]")) {
  const filters = bench.querySelector(".bench-filters");
  const buttons = [...filters.querySelectorAll("button")];
  const rows = [...bench.querySelectorAll("tbody tr")];
  filters.hidden = false;
  for (const button of buttons) {
    button.addEventListener("click", () => {
      const kind = button.dataset.filter;
      for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
      for (const row of rows) row.hidden = kind !== "all" && !row.dataset.kind.split(" ").includes(kind);
    });
  }
}
