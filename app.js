const styles = [
  {
    id: "simple",
    label: "简单拼接",
  },
  {
    id: "mix",
    label: "口播环境混剪",
  },
];

const uploadLimits = {
  maxBatchFiles: 300,
  maxTotalAssets: 1000,
};

const demoDurations = ["2.0s", "9.3s", "7.6s", "5.9s", "3.2s", "10.5s", "8.8s", "6.1s", "4.4s", "2.7s", "9.0s", "7.3s"];
const swatches = ["#172332", "#241a33", "#173128", "#332315", "#361a26", "#183037", "#223015", "#351e18"];

const state = {
  activeStyle: "simple",
  talking: [],
  environment: [],
  tracks: [],
  generatedOutputs: [],
  logs: [],
  progressTimer: null,
  floatingAssetsOpen: true,
};

const api = {
  upload: (lane) => `/api/upload?lane=${encodeURIComponent(lane)}`,
  generate: "/api/generate",
  deleteAsset: "/api/delete_asset",
  chooseDirectory: "/api/choose_directory",
  debugReport: "/api/debug_report",
  saveOutputs: "/api/save_outputs",
  reset: "/api/reset",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function icon(name) {
  return `<svg><use href="#${name}"></use></svg>`;
}

function uid() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

function currentStyle() {
  return styles.find((style) => style.id === state.activeStyle) || styles[0];
}

function laneName(lane) {
  return lane === "talking" ? "口播" : "环境";
}

function fileKind(file) {
  if (file.type.startsWith("image/")) return "图片";
  if (file.type.startsWith("video/")) return "视频";
  return "文件";
}

function totalAssets() {
  return state.talking.length + state.environment.length;
}

function outputCount() {
  return state.tracks.length;
}

function durationFor(index) {
  return demoDurations[index % demoDurations.length];
}

function colorFor(lane, index) {
  if (lane === "talking") return swatches[index % swatches.length];
  return swatches[(index + 2) % swatches.length];
}

function secondsFromDuration(duration) {
  return Number.parseFloat(String(duration).replace("s", "")) || 0;
}

function createTrack() {
  return {
    id: uid(),
    clips: [],
  };
}

function findAsset(lane, assetId) {
  return state[lane].find((asset) => asset.id === assetId);
}

function clipFromAsset(asset, lane) {
  return {
    clipId: uid(),
    assetId: asset.id,
    lane,
    title: asset.name,
    detail: "按轨道顺序拼接",
    duration: asset.duration,
    color: asset.color,
  };
}

function setUploadMessage(message, isWarning = false) {
  const box = $("#upload-limit");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("warning", isWarning);
}

function normalizeServerAsset(asset, lane, index) {
  return {
    id: asset.id,
    name: asset.name,
    size: asset.size,
    type: asset.type,
    url: asset.url,
    kind: asset.kind,
    duration: asset.duration || durationFor(index),
    color: colorFor(lane, index),
  };
}

async function uploadFilesToServer(lane, files) {
  const form = new FormData();
  files.forEach((file) => form.append("files", file, file.name));
  const response = await fetch(api.upload(lane), {
    method: "POST",
    body: form,
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || "上传失败");
  }
  return payload.assets || [];
}

async function addFiles(lane, fileList) {
  const incoming = Array.from(fileList || []);
  if (!incoming.length) return;

  const currentTotal = totalAssets();
  const remaining = uploadLimits.maxTotalAssets - currentTotal;
  if (remaining <= 0) {
    setUploadMessage(`已达到当前前端总素材上限 ${uploadLimits.maxTotalAssets} 个，请先清空或等待后端分批上传能力。`, true);
    writeLog("UPLOAD_LIMIT", "上传被前端上限拦截", { lane, selected: incoming.length, currentTotal });
    return;
  }

  const accepted = incoming.slice(0, remaining);
  const overBatch = incoming.length > uploadLimits.maxBatchFiles;
  const overTotal = incoming.length > remaining;

  setUploadMessage(`正在上传 ${accepted.length} 个${laneName(lane)}素材...`, false);

  try {
    const serverAssets = await uploadFilesToServer(lane, accepted);
    const files = serverAssets.map((asset, index) => normalizeServerAsset(asset, lane, state[lane].length + index));
    state[lane].push(...files);

    if (overBatch || overTotal) {
      const messages = [];
      if (overBatch) messages.push(`单次选择 ${incoming.length} 个，建议单批不超过 ${uploadLimits.maxBatchFiles} 个`);
      if (overTotal) messages.push(`总上限 ${uploadLimits.maxTotalAssets} 个，本次只导入 ${accepted.length} 个`);
      setUploadMessage(`${messages.join("；")}。`, true);
    } else {
      setUploadMessage(`已上传 ${files.length} 个${laneName(lane)}素材到本地服务。`, false);
    }

    writeLog("ADD_ASSETS", `添加${laneName(lane)}素材`, {
      lane,
      selected: incoming.length,
      imported: files.length,
      files: files.map((file) => ({ name: file.name, size: file.size, type: file.type, id: file.id })),
    });
    renderAll();
  } catch (error) {
    setUploadMessage(`上传失败：${error.message}`, true);
    writeLog("UPLOAD_ERROR", "上传失败", { lane, error: error.message });
  }
}

function addTrack() {
  state.tracks.push(createTrack());
  writeLog("ADD_TRACK", "添加成片轨道", { trackCount: state.tracks.length });
  renderAll();
}

function removeTrack(trackId) {
  const index = state.tracks.findIndex((track) => track.id === trackId);
  if (index < 0) return;
  state.tracks.splice(index, 1);
  writeLog("REMOVE_TRACK", "移除成片轨道", { trackIndex: index + 1, trackCount: state.tracks.length });
  renderAll();
}

function clearTracks() {
  state.tracks = [];
  writeLog("CLEAR_TRACKS", "清空成片轨道", {});
  renderAll();
}

function addClipToTrack(trackId, lane, assetId, targetIndex = null) {
  const track = state.tracks.find((item) => item.id === trackId);
  const asset = findAsset(lane, assetId);
  if (!track || !asset) return;

  const clip = clipFromAsset(asset, lane);
  const insertAt = targetIndex === null ? track.clips.length : Math.max(0, Math.min(targetIndex, track.clips.length));
  track.clips.splice(insertAt, 0, clip);
  writeLog("ADD_CLIP_TO_TRACK", "素材拖入成片轨道", {
    trackId,
    lane,
    asset: asset.name,
    position: insertAt + 1,
  });
  renderAll();
}

function moveClipToTrack(sourceTrackId, clipId, targetTrackId, targetIndex = null) {
  const sourceTrack = state.tracks.find((track) => track.id === sourceTrackId);
  const targetTrack = state.tracks.find((track) => track.id === targetTrackId);
  if (!sourceTrack || !targetTrack) return;

  const sourceIndex = sourceTrack.clips.findIndex((clip) => clip.clipId === clipId);
  if (sourceIndex < 0) return;

  const originalTargetLength = targetTrack.clips.length;
  let insertAt = targetIndex === null ? originalTargetLength : Math.max(0, Math.min(targetIndex, originalTargetLength));
  const [clip] = sourceTrack.clips.splice(sourceIndex, 1);
  if (sourceTrackId === targetTrackId && sourceIndex < insertAt) insertAt -= 1;
  insertAt = Math.max(0, Math.min(insertAt, targetTrack.clips.length));
  targetTrack.clips.splice(insertAt, 0, clip);

  writeLog("MOVE_CLIP", "调整轨道内素材顺序", {
    sourceTrackId,
    targetTrackId,
    clip: clip.title,
    from: sourceIndex + 1,
    to: insertAt + 1,
  });
  renderAll();
}

function removeClip(trackId, clipId) {
  const track = state.tracks.find((item) => item.id === trackId);
  if (!track) return;
  const index = track.clips.findIndex((clip) => clip.clipId === clipId);
  if (index < 0) return;
  const [clip] = track.clips.splice(index, 1);
  writeLog("REMOVE_CLIP", "从成片轨道移除素材", { trackId, clip: clip.title });
  renderAll();
}

async function removeAsset(lane, assetId) {
  const list = state[lane];
  const index = list.findIndex((asset) => asset.id === assetId);
  if (index < 0) return;

  const [asset] = list.splice(index, 1);
  let removedClips = 0;
  state.tracks.forEach((track) => {
    const before = track.clips.length;
    track.clips = track.clips.filter((clip) => clip.assetId !== assetId);
    removedClips += before - track.clips.length;
  });

  try {
    const response = await fetch(api.deleteAsset, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ assetId }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "删除素材失败");
    }
    setUploadMessage(`已移除 ${asset.name}`, false);
    writeLog("REMOVE_ASSET", "移除素材", {
      lane,
      assetId,
      name: asset.name,
      removedClips,
    });
  } catch (error) {
    setUploadMessage(`素材已从界面移除，但服务端删除失败：${error.message}`, true);
    writeLog("REMOVE_ASSET_ERROR", "服务端删除素材失败", {
      lane,
      assetId,
      name: asset.name,
      error: error.message,
    });
  }

  renderAll();
}

function writeLog(type, message, detail = {}) {
  const entry = {
    id: uid(),
    time: new Date().toISOString(),
    type,
    message,
    detail,
    snapshot: {
      activeStyle: state.activeStyle,
      talkingCount: state.talking.length,
      environmentCount: state.environment.length,
      trackCount: state.tracks.length,
      clipCount: state.tracks.reduce((total, track) => total + track.clips.length, 0),
    },
  };
  state.logs.unshift(entry);
  state.logs = state.logs.slice(0, 160);
  console.info("[VideoWorkbench]", entry);
  renderLogs();
}

function summarizeLog(entry) {
  if (entry.type === "ADD_ASSETS") return `选择 ${entry.detail.selected} 个，导入 ${entry.detail.imported} 个`;
  if (entry.type === "ADD_TRACK") return `当前 ${entry.detail.trackCount} 条`;
  if (entry.type === "REMOVE_TRACK") return `剩余 ${entry.detail.trackCount} 条`;
  if (entry.type === "ADD_CLIP_TO_TRACK") return `${entry.detail.asset} -> 第 ${entry.detail.position} 位`;
  if (entry.type === "MOVE_CLIP") return `${entry.detail.clip}，${entry.detail.from} -> ${entry.detail.to}`;
  if (entry.type === "REMOVE_CLIP") return entry.detail.clip;
  if (entry.type === "UPLOAD_SLOT_CLICK") return `准备上传${laneName(entry.detail.lane)}素材`;
  if (entry.type === "GENERATE_START") return `预计 ${entry.detail.outputCount} 支`;
  if (entry.type === "GENERATE_DONE") return `${entry.detail.outputCount} 支完成`;
  if (entry.type === "GENERATE_ERROR") return entry.detail.error;
  if (entry.type === "RESET") return "清空素材、轨道、输出和进度";
  if (entry.type === "CLEAR_TRACKS") return "轨道已清空";
  if (entry.type === "UPLOAD_LIMIT") return `选择 ${entry.detail.selected} 个`;
  if (entry.type === "UPLOAD_ERROR") return entry.detail.error;
  if (entry.type === "ERROR") return entry.detail.error || "未知错误";
  if (entry.type === "EXPORT_LOGS") return `${entry.detail.logCount} 条日志`;
  return JSON.stringify(entry.detail);
}

function renderLogs() {
  const list = $("#log-list");
  if (!list) return;

  list.innerHTML = state.logs
    .slice(0, 12)
    .map((entry) => {
      const time = new Date(entry.time).toLocaleTimeString("zh-CN", { hour12: false });
      return `
        <article class="log-entry">
          <time>${time}</time>
          <div>
            <strong>${entry.message}</strong>
            <span>${summarizeLog(entry)}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

async function fetchDebugReport() {
  try {
    const response = await fetch(api.debugReport);
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "读取后端调试报告失败");
    }
    return payload.report;
  } catch (error) {
    return {
      error: error.message,
      hint: "后端调试报告读取失败，请同时提供软件文件夹里的 logs/workbench.log",
    };
  }
}

async function exportLogs() {
  const backendReport = await fetchDebugReport();
  const payload = {
    exportedAt: new Date().toISOString(),
    app: "video-batch-workbench",
    version: "0.6.4",
    limits: uploadLimits,
    currentUrl: window.location.href,
    userAgent: navigator.userAgent,
    activeStyle: state.activeStyle,
    assets: {
      talking: state.talking,
      environment: state.environment,
    },
    tracks: state.tracks,
    logs: state.logs,
    backend: backendReport,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  link.href = url;
  link.download = `剪辑工作台日志_${stamp}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  writeLog("EXPORT_LOGS", "导出运行日志", {
    logCount: state.logs.length,
    backendLogs: backendReport?.recentBackendLogs?.length || 0,
    hasFfmpegError: Boolean(backendReport?.lastFfmpegError),
  });
}

function uploadSlot(label, lane) {
  return `
    <button class="slot-card add-slot" type="button" data-upload-slot="${lane}" aria-label="上传${label}">
      <span class="slot-inner">
        <span class="slot-plus">${icon("icon-plus")}</span>
        <span>上传${label}</span>
      </span>
    </button>
  `;
}

function renderRail(lane) {
  const rail = $(`#${lane}-rail`);
  const list = state[lane];
  const count = $(`#${lane}-count`);
  count.textContent = `${list.length} 个`;

  if (!list.length) {
    rail.innerHTML = uploadSlot(`${laneName(lane)}素材`, lane);
    return;
  }

  rail.innerHTML = list
    .map((item) => {
      const isVideo = item.type.startsWith("video/");
      const isImage = item.type.startsWith("image/");
      const thumb = isVideo
        ? `<video src="${item.url}" muted playsinline></video>`
        : isImage
          ? `<img src="${item.url}" alt="">`
          : "";
      return `
        <article class="asset-card" title="${item.name}" draggable="true" data-drag-source="pool" data-lane="${lane}" data-asset-id="${item.id}" style="background:${item.color}">
          <button class="asset-remove" type="button" title="移除素材" data-remove-asset="${item.id}" data-lane="${lane}">${icon("icon-trash")}</button>
          <div class="asset-thumb">${thumb}</div>
          <span class="asset-index">${item.duration}</span>
          <div class="asset-meta">
            <strong>${item.name}</strong>
            <span>${item.kind} ${(item.size / 1024 / 1024).toFixed(1)} MB</span>
          </div>
        </article>
      `;
    })
    .join("") + uploadSlot(`${laneName(lane)}素材`, lane);
}

function compactAssetCard(item, lane) {
  return `
    <article class="floating-asset-card ${lane}" draggable="true" data-drag-source="pool" data-lane="${lane}" data-asset-id="${item.id}" title="${item.name}">
      <small>${laneName(lane)}</small>
      <strong>${item.name}</strong>
      <span>${item.duration}</span>
    </article>
  `;
}

function renderFloatingAssets() {
  const shell = $("#floating-assets");
  const body = $("#floating-assets-body");
  const count = $("#floating-assets-count");
  const panel = $("#floating-assets-panel");
  if (!shell || !body || !count || !panel) return;

  const total = totalAssets();
  count.textContent = `${total} 个素材`;
  body.innerHTML = total
    ? `
      <div class="floating-lane">
        <span>口播</span>
        <div>${state.talking.map((asset) => compactAssetCard(asset, "talking")).join("") || `<em>暂无口播</em>`}</div>
      </div>
      <div class="floating-lane">
        <span>环境</span>
        <div>${state.environment.map((asset) => compactAssetCard(asset, "environment")).join("") || `<em>暂无环境</em>`}</div>
      </div>
    `
    : "";
  panel.hidden = !state.floatingAssetsOpen;
  updateFloatingAssetsVisibility();
}

function updateFloatingAssetsVisibility() {
  const shell = $("#floating-assets");
  const pool = $(".pool-shell");
  if (!shell || !pool) return;

  const poolBottom = pool.getBoundingClientRect().bottom;
  shell.hidden = !(totalAssets() > 0 && poolBottom < 70);
}

function addTrackCard() {
  return `
    <article class="track-card add-track-card">
      <button class="empty-track" type="button" data-empty-add-track>
        <span class="empty-track-content">
          <span class="empty-plus">${icon("icon-plus")}</span>
          <span>添加成片轨道</span>
        </span>
      </button>
    </article>
  `;
}

function renderTrackList() {
  const list = $("#track-list");

  if (!state.tracks.length) {
    list.innerHTML = `
      <article class="track-card">
        <div class="track-head">
          <div class="track-title">
            <small>EMPTY</small>
            <strong>还没有成片轨道</strong>
          </div>
          <div class="track-meta"><span>点击“添加成片轨道”开始</span></div>
        </div>
        <div class="track-strip">
          <button class="empty-track" type="button" data-empty-add-track>
            <span class="empty-track-content">
              <span class="empty-plus">${icon("icon-plus")}</span>
              <span>点击添加成片轨道</span>
            </span>
          </button>
        </div>
      </article>
    `;
    return;
  }

  list.innerHTML = state.tracks
    .map((track, rowIndex) => {
      const approxSeconds = track.clips.reduce((total, clip) => total + secondsFromDuration(clip.duration), 0);
      return `
        <article class="track-card" data-track-id="${track.id}">
          <div class="track-head">
            <div class="track-title">
              <small>V${String(rowIndex + 1).padStart(2, "0")}</small>
              <strong>成片 ${rowIndex + 1} · ${currentStyle().label}</strong>
            </div>
            <div class="track-meta">
              <span><b>${track.clips.length}</b> 段</span>
              <span>≈ ${Math.round(approxSeconds)}s</span>
              <button class="track-remove" type="button" data-remove-track="${track.id}">移除</button>
            </div>
          </div>
          <div class="track-strip" data-track-strip="${track.id}">
            ${
              track.clips.length
                ? track.clips
                    .map((clip, clipIndex) => `
                      <article class="clip-card ${clip.lane}" draggable="true" data-drag-source="track" data-track-id="${track.id}" data-clip-id="${clip.clipId}" style="background:${clip.color}">
                        <em>${String(clipIndex + 1).padStart(2, "0")}</em>
                        <strong title="${clip.title}">${clip.title}</strong>
                        <span>${clip.detail}</span>
                        <span>${clip.duration}</span>
                        <div class="clip-actions">
                          <button type="button" title="移除片段" data-remove-clip="${clip.clipId}" data-track-id="${track.id}">${icon("icon-trash")}</button>
                        </div>
                      </article>
                    `)
                    .join("")
                : `<div class="empty-track">把素材拖到这里，生成这一支视频</div>`
            }
          </div>
        </article>
      `;
    })
    .join("") + addTrackCard();
}

function renderStats() {
  const count = outputCount();
  const assets = totalAssets();
  const seconds = state.tracks.reduce(
    (total, track) => total + track.clips.reduce((trackTotal, clip) => trackTotal + secondsFromDuration(clip.duration), 0),
    0
  );
  const duration = seconds ? `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.round(seconds % 60)).padStart(2, "0")}` : "--:--";
  $("#top-output-count").textContent = count;
  $("#top-asset-count").textContent = assets;
  $("#dock-output-count").textContent = count;
  $("#dock-asset-count").textContent = assets;
  $("#dock-duration").textContent = duration;
}

function renderStyles() {
  $$("[data-style]").forEach((button) => {
    const isActive = button.dataset.style === state.activeStyle;
    button.classList.toggle("active", isActive);
    const badge = button.querySelector(".style-top span");
    if (badge) badge.textContent = isActive ? "使用中" : "可用";
  });
}

function renderAll() {
  renderStyles();
  renderRail("talking");
  renderRail("environment");
  renderTrackList();
  renderOutputPanel();
  renderFloatingAssets();
  renderStats();
}

function renderOutputPanel() {
  const list = $("#output-list");
  if (!list) return;

  const outputs = state.generatedOutputs || [];
  if (!outputs.length) {
    list.innerHTML = `<div class="empty-track">生成完成后，结果视频会出现在这里</div>`;
    return;
  }

  list.innerHTML = outputs
    .map((output) => {
      return `<a class="output-card" href="${output.url}" download="${output.name}"><strong>${output.name}</strong><span>${currentStyle().label}</span><span>点击下载 · ${output.duration || "--"}s</span></a>`;
    })
    .join("");
}

function renderOutputsComplete(count) {
  const outputs = state.generatedOutputs || [];
  $("#track-list").insertAdjacentHTML(
    "beforeend",
    `<article class="track-card output-summary">
      <div class="track-head">
        <div class="track-title">
          <small>OUT</small>
          <strong>最近生成结果</strong>
        </div>
        <div class="track-meta"><span><b>${count}</b> 支完成</span></div>
      </div>
      <div class="track-strip">
        ${outputs
          .slice(0, 10)
          .map((output) => {
            return `<a class="output-card" href="${output.url}" download="${output.name}"><strong>${output.name}</strong><span>${currentStyle().label}</span><span>点击下载 · ${output.duration || "--"}s</span></a>`;
          })
          .join("")}
      </div>
    </article>`
  );
}

async function generateOutputs() {
  const count = outputCount();
  const style = currentStyle();
  const progressFill = $("#progress-fill");
  const progressValue = $("#progress-value");
  const progressLabel = $("#progress-label");

  if (!count) {
    setUploadMessage("请先添加至少一条成片轨道，再点击生成。", true);
    writeLog("GENERATE_BLOCKED", "没有成片轨道，无法生成", {});
    return;
  }

  const totalClips = state.tracks.reduce((total, track) => total + track.clips.length, 0);
  if (!totalClips) {
    setUploadMessage("请先把素材拖入至少一条成片轨道，再点击生成。", true);
    writeLog("GENERATE_BLOCKED", "成片轨道为空，无法生成", {});
    return;
  }

  if (state.progressTimer) clearInterval(state.progressTimer);

  const request = {
    styleId: style.id,
    styleLabel: style.label,
    ratio: $("#ratio").value,
    nameRule: $("#name-rule").value,
    onlyEnvironmentSubtitles: document.querySelector(".dock-check input")?.checked ?? true,
    tracks: state.tracks.map((track, index) => ({
      id: track.id,
      index: index + 1,
      clips: track.clips.map((clip) => ({
        clipId: clip.clipId,
        assetId: clip.assetId,
        lane: clip.lane,
        title: clip.title,
      })),
    })),
  };

  writeLog("GENERATE_START", "开始生成视频", {
    outputCount: count,
    styleId: style.id,
    styleLabel: style.label,
    tracks: request.tracks,
  });

  let progress = 0;
  progressFill.style.width = "0%";
  progressValue.textContent = "0%";
  progressLabel.textContent = "整理轨道";

  state.progressTimer = setInterval(() => {
    progress = Math.min(88, progress + Math.ceil(Math.random() * 7));
    if (progress > 70) {
      progressLabel.textContent = "导出成片";
    } else if (progress > 35) {
      progressLabel.textContent = "套用轨道顺序";
    }
    progressFill.style.width = `${progress}%`;
    progressValue.textContent = `${progress}%`;
  }, 360);

  try {
    const response = await fetch(api.generate, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.error || "生成失败");
      error.debug = payload.debug;
      throw error;
    }
    clearInterval(state.progressTimer);
    state.progressTimer = null;
    state.generatedOutputs = payload.outputs || [];
    progressLabel.textContent = "生成完成";
    progressFill.style.width = "100%";
    progressValue.textContent = "100%";
    renderAll();
    writeLog("GENERATE_DONE", "生成完成", {
      outputCount: state.generatedOutputs.length,
      outputs: state.generatedOutputs,
      styleId: style.id,
      styleLabel: style.label,
    });
  } catch (error) {
    clearInterval(state.progressTimer);
    state.progressTimer = null;
    progressLabel.textContent = "生成失败";
    setUploadMessage(`生成失败：${error.message}`, true);
    writeLog("GENERATE_ERROR", "生成失败", { error: error.message, debug: error.debug || null });
  }
}

function showModal(title, body) {
  const modal = $("#message-modal");
  if (!modal) return;
  $("#message-modal-title").textContent = title;
  $("#message-modal-body").textContent = body;
  modal.hidden = false;
}

function hideModal() {
  const modal = $("#message-modal");
  if (modal) modal.hidden = true;
}

async function chooseSaveDirectory() {
  setUploadMessage("正在打开文件夹选择窗口...", false);
  try {
    const response = await fetch(api.chooseDirectory, { method: "POST" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "选择文件夹失败");
    }
    if (!payload.directory) {
      setUploadMessage("已取消选择文件夹。", true);
      writeLog("CHOOSE_DIRECTORY_CANCEL", "取消选择保存目录", {});
      return;
    }
    $("#save-dir").value = payload.directory;
    setUploadMessage(`保存目录已设置为：${payload.directory}`, false);
    writeLog("CHOOSE_DIRECTORY_DONE", "选择保存目录", { directory: payload.directory });
  } catch (error) {
    setUploadMessage(`选择文件夹失败：${error.message}`, true);
    showModal("选择文件夹失败", error.message);
    writeLog("CHOOSE_DIRECTORY_ERROR", "选择保存目录失败", { error: error.message });
  }
}

async function saveAllOutputs() {
  const outputs = state.generatedOutputs || [];
  const directory = $("#save-dir")?.value.trim() || "";

  if (!outputs.length) {
    setUploadMessage("当前还没有生成结果，先生成视频后再保存。", true);
    showModal("还没有生成结果", "请先生成视频，完成后再一键保存全部。");
    writeLog("SAVE_OUTPUTS_BLOCKED", "没有可保存的生成结果", {});
    return;
  }

  if (!directory) {
    setUploadMessage("请先选择保存目录。", true);
    showModal("缺少保存目录", "请先点击“选择文件夹”，选择视频要保存到的位置。");
    writeLog("SAVE_OUTPUTS_BLOCKED", "缺少保存目录", {});
    return;
  }

  try {
    const response = await fetch(api.saveOutputs, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ directory, outputs }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "保存失败");
    }
    setUploadMessage(`已保存 ${payload.saved.length} 个视频到：${payload.directory}`, false);
    showModal("保存完成", `已保存 ${payload.saved.length} 个视频到：${payload.directory}`);
    writeLog("SAVE_OUTPUTS_DONE", "一键保存全部结果", {
      directory: payload.directory,
      count: payload.saved.length,
      files: payload.saved,
    });
  } catch (error) {
    setUploadMessage(`保存失败：${error.message}`, true);
    showModal("保存失败", error.message);
    writeLog("SAVE_OUTPUTS_ERROR", "一键保存失败", { directory, error: error.message });
  }
}

async function resetWorkbench() {
  setUploadMessage("正在清空本地素材和输出...", false);
  try {
    const response = await fetch(api.reset, { method: "POST" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "清空服务端文件失败");
    }
  } catch (error) {
    setUploadMessage(`清空服务端文件失败：${error.message}`, true);
    writeLog("RESET_SERVER_ERROR", "清空服务端文件失败", { error: error.message });
  }
  state.talking = [];
  state.environment = [];
  state.tracks = [];
  state.generatedOutputs = [];
  if (state.progressTimer) clearInterval(state.progressTimer);
  state.progressTimer = null;
  $("#progress-fill").style.width = "0%";
  $("#progress-value").textContent = "0%";
  $("#progress-label").textContent = "等待生成";
  setUploadMessage(`当前前端限制：单次最多 ${uploadLimits.maxBatchFiles} 个文件，总素材最多 ${uploadLimits.maxTotalAssets} 个。后续接后端后可再调整为分批上传、排队生成。`, false);
  writeLog("RESET", "清空工作台", {});
  renderAll();
}

function updateClock() {
  const clock = $("#clock");
  if (!clock) return;
  clock.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function dragPayloadFromEvent(event) {
  try {
    return JSON.parse(event.dataTransfer.getData("application/json"));
  } catch {
    return null;
  }
}

function targetIndexForDrop(strip, event) {
  const cards = Array.from(strip.querySelectorAll(".clip-card"));
  if (!cards.length) return 0;
  const target = cards.find((card) => {
    const rect = card.getBoundingClientRect();
    return event.clientX < rect.left + rect.width / 2;
  });
  return target ? cards.indexOf(target) : cards.length;
}

function bindDragEvents() {
  document.addEventListener("dragstart", (event) => {
    const assetCard = event.target.closest(".asset-card[draggable='true'], .floating-asset-card[draggable='true']");
    const clipCard = event.target.closest(".clip-card[draggable='true']");
    const source = assetCard || clipCard;
    if (!source) return;

    source.classList.add("dragging");
    const payload = source.dataset.dragSource === "pool"
      ? { source: "pool", lane: source.dataset.lane, assetId: source.dataset.assetId }
      : { source: "track", trackId: source.dataset.trackId, clipId: source.dataset.clipId };
    event.dataTransfer.setData("application/json", JSON.stringify(payload));
    event.dataTransfer.effectAllowed = "copyMove";
  });

  document.addEventListener("dragend", () => {
    $$(".dragging").forEach((item) => item.classList.remove("dragging"));
    $$(".drag-over").forEach((item) => item.classList.remove("drag-over"));
  });

  document.addEventListener("dragover", (event) => {
    const strip = event.target.closest("[data-track-strip]");
    if (!strip) return;
    event.preventDefault();
    strip.classList.add("drag-over");
    strip.closest(".track-card")?.classList.add("drag-over");
  });

  document.addEventListener("dragleave", (event) => {
    const strip = event.target.closest("[data-track-strip]");
    if (!strip || strip.contains(event.relatedTarget)) return;
    strip.classList.remove("drag-over");
    strip.closest(".track-card")?.classList.remove("drag-over");
  });

  document.addEventListener("drop", (event) => {
    const strip = event.target.closest("[data-track-strip]");
    if (!strip) return;
    event.preventDefault();

    const payload = dragPayloadFromEvent(event);
    if (!payload) return;

    const targetTrackId = strip.dataset.trackStrip;
    const targetIndex = targetIndexForDrop(strip, event);
    if (payload.source === "pool") {
      addClipToTrack(targetTrackId, payload.lane, payload.assetId, targetIndex);
    }
    if (payload.source === "track") {
      moveClipToTrack(payload.trackId, payload.clipId, targetTrackId, targetIndex);
    }
  });
}

function bindEvents() {
  $$("[data-upload]").forEach((button) => {
    button.addEventListener("click", () => {
      const lane = button.dataset.upload;
      $(`#${lane}-input`).click();
    });
  });

  $("#talking-input").addEventListener("change", async (event) => {
    await addFiles("talking", event.target.files);
    event.target.value = "";
  });
  $("#environment-input").addEventListener("change", async (event) => {
    await addFiles("environment", event.target.files);
    event.target.value = "";
  });

  ["talking", "environment"].forEach((lane) => {
    const rail = $(`#${lane}-rail`);
    rail.addEventListener("dragover", (event) => event.preventDefault());
    rail.addEventListener("drop", (event) => {
      event.preventDefault();
      addFiles(lane, event.dataTransfer.files);
    });
  });

  document.addEventListener("click", (event) => {
    const removeAssetButton = event.target.closest("[data-remove-asset]");
    if (removeAssetButton) {
      removeAsset(removeAssetButton.dataset.lane, removeAssetButton.dataset.removeAsset);
      return;
    }

    const removeTrackButton = event.target.closest("[data-remove-track]");
    if (removeTrackButton) {
      removeTrack(removeTrackButton.dataset.removeTrack);
      return;
    }

    const uploadSlot = event.target.closest("[data-upload-slot]");
    if (uploadSlot) {
      const lane = uploadSlot.dataset.uploadSlot;
      $(`#${lane}-input`).click();
      writeLog("UPLOAD_SLOT_CLICK", "点击素材池空位上传", { lane });
      return;
    }

    const emptyAddTrack = event.target.closest("[data-empty-add-track]");
    if (emptyAddTrack) {
      addTrack();
      return;
    }

    const floatingToggle = event.target.closest("#floating-assets-toggle");
    if (floatingToggle) {
      state.floatingAssetsOpen = !state.floatingAssetsOpen;
      writeLog("TOGGLE_FLOATING_ASSETS", state.floatingAssetsOpen ? "展开悬浮素材栏" : "收起悬浮素材栏", {
        open: state.floatingAssetsOpen,
      });
      renderFloatingAssets();
      return;
    }

    const removeClipButton = event.target.closest("[data-remove-clip]");
    if (removeClipButton) {
      removeClip(removeClipButton.dataset.trackId, removeClipButton.dataset.removeClip);
      return;
    }

    const styleButton = event.target.closest("[data-style]");
    if (styleButton) {
      if (styleButton.disabled) return;
      state.activeStyle = styleButton.dataset.style;
      writeLog("STYLE_CHANGE", "切换样式", {
        styleId: state.activeStyle,
        styleLabel: currentStyle().label,
      });
      renderAll();
    }
  });

  $("#add-track").addEventListener("click", addTrack);
  $("#clear-tracks").addEventListener("click", clearTracks);
  $("#generate").addEventListener("click", generateOutputs);
  $("#reset-demo").addEventListener("click", resetWorkbench);
  $("#export-logs").addEventListener("click", exportLogs);
  $("#choose-save-dir").addEventListener("click", chooseSaveDirectory);
  $("#save-all-outputs").addEventListener("click", saveAllOutputs);
  $("#message-modal-close").addEventListener("click", hideModal);
  $("#message-modal").addEventListener("click", (event) => {
    if (event.target.id === "message-modal") hideModal();
  });
  window.addEventListener("scroll", updateFloatingAssetsVisibility, { passive: true });
  window.addEventListener("resize", updateFloatingAssetsVisibility);

  window.addEventListener("error", (event) => {
    writeLog("ERROR", "页面脚本异常", {
      error: event.message,
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    });
  });

  bindDragEvents();
}

state.tracks.push(createTrack());
bindEvents();
renderAll();
updateClock();
setInterval(updateClock, 1000);
writeLog("APP_READY", "工作台已打开", { styles: styles.map((style) => style.id), limits: uploadLimits });
