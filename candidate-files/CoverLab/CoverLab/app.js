(() => {
  "use strict";

  const API = {
    state: "/api/state",
    upload: "/api/upload",
    process: "/api/process",
    demo: "/api/demo",
  };
  const ROLE_KEY = "radar-cover-lab.roles.v1";
  const ORIGIN_KEY = "radar-cover-lab.origins.v1";
  const LYRICS_KEY = "radar-cover-lab.lyrics.v1";
  const MAX_UPLOAD_BYTES = 128 * 1024 * 1024;

  const roleDefinitions = [
    { id: "original", label: "原音", filterLabel: "原音", color: "cyan" },
    { id: "accompaniment", label: "伴奏", filterLabel: "伴奏", color: "violet" },
    { id: "vocal_a", label: "人声 A", filterLabel: "人声 A", color: "green" },
    { id: "vocal_b", label: "人声 B", filterLabel: "人声 B", color: "orange" },
    { id: "result", label: "处理结果", filterLabel: "处理结果", color: "green" },
    { id: "unassigned", label: "未分类", filterLabel: "未分类", color: "muted" },
  ];
  const operations = {
    convert: { label: "格式转换", description: "将音频编码为 WAV 或 MP3。" },
    trim: { label: "音频截取", description: "按起止时间截取并从零点开始输出。" },
    pitch: { label: "音高移调", description: "调整音高，范围为 -12 到 +12 个半音。" },
    tempo: { label: "速度调整", description: "调整播放速度，范围为 0.5× 到 2×。" },
    normalize: { label: "响度标准化", description: "使用本地响度标准化滤镜处理音频。" },
    mix: { label: "双轨混音", description: "将两个本地音轨按独立增益混合。" },
  };

  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));
  const audio = $("#audioElement");
  const roleMap = readStorage(ROLE_KEY, {});
  const originMap = readStorage(ORIGIN_KEY, {});
  const lyricsMap = readStorage(LYRICS_KEY, {});
  let files = [];
  let capabilities = {};
  let selectedFileId = null;
  let activeFileId = null;
  let activeOperation = "convert";
  let activeRoleFilter = "all";
  let operationBusy = false;
  let uploadBusy = false;
  let stateAvailable = false;
  let operationFeedbackKind = "";
  let toastTimer = 0;
  let audioContext = null;
  let waveform = null;
  let waveformFileId = null;
  let waveformGeneration = 0;
  let lyricPopup = null;

  function readStorage(key, fallback) {
    try {
      const value = JSON.parse(localStorage.getItem(key) || "null");
      return value && typeof value === "object" ? value : fallback;
    } catch {
      return fallback;
    }
  }

  function saveStorage(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Browser storage can be disabled. */ }
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function notify(message, kind = "info") {
    const region = $("#toastRegion");
    region.replaceChildren();
    const toast = make("div", `toast${kind === "error" ? " is-error" : ""}`, message);
    region.append(toast);
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.remove(), 3600);
  }

  async function responseJson(response) {
    let data = {};
    try { data = await response.json(); } catch { /* A useful HTTP error may have no JSON body. */ }
    if (!response.ok) {
      const message = data.error || data.message || data.detail || `请求失败（${response.status}）`;
      throw new Error(String(message));
    }
    return data;
  }

  function getFile(fileId) {
    return files.find((file) => file.id === fileId) || null;
  }

  function roleOf(fileId) {
    return roleDefinitions.some((role) => role.id === roleMap[fileId]) ? roleMap[fileId] : "unassigned";
  }

  function roleLabel(roleId) {
    return roleDefinitions.find((role) => role.id === roleId)?.label || "未分类";
  }

  function originLabel(fileId) {
    return ({ upload: "导入文件", demo: "合成演示音频", process: "本地处理结果" })[originMap[fileId]] || "本地素材";
  }

  function formatDuration(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
    const whole = Math.floor(seconds);
    const minutes = Math.floor(whole / 60);
    const remainder = whole % 60;
    if (minutes >= 60) {
      const hours = Math.floor(minutes / 60);
      return `${hours}:${String(minutes % 60).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
    }
    return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }

  function formatSampleRate(value) {
    const rate = Number(value);
    return Number.isFinite(rate) ? `${Math.round(rate / 1000)} kHz` : "音频文件";
  }

  function displayName(file) {
    return file && typeof file.name === "string" && file.name ? file.name : "未命名音频";
  }

  function sortFiles(list) {
    return [...list].sort((a, b) => displayName(a).localeCompare(displayName(b), "zh-CN", { numeric: true }));
  }

  function setEngineState(online, label) {
    const dot = $("#engineDot");
    const badge = $(".engine-badge");
    dot.classList.toggle("is-offline", !online);
    badge.classList.toggle("is-offline", !online);
    $("#engineLabel").textContent = label;
    $("#footerEngineStatus").textContent = online ? "本地 API 已连接" : "本地 API 未连接";
  }

  function setOperationState(text, state = "idle") {
    $("#operationStatus").textContent = text;
    const dot = $("#processDot");
    dot.classList.toggle("is-busy", state === "busy");
    dot.classList.toggle("is-ready", state === "ready");
    dot.classList.toggle("is-error", state === "error");
  }

  function setOperationFeedback(message, kind = "") {
    const target = $("#operationFeedback");
    target.textContent = message;
    target.className = `operation-feedback${kind ? ` is-${kind}` : ""}`;
    operationFeedbackKind = kind;
  }

  function renderRoleFilters() {
    const container = $("#roleFilters");
    container.replaceChildren();
    const filters = [{ id: "all", label: "全部" }, ...roleDefinitions.map((role) => ({ id: role.id, label: role.filterLabel }))];
    for (const filter of filters) {
      const button = make("button", `filter-button${activeRoleFilter === filter.id ? " is-active" : ""}`, filter.label);
      button.type = "button";
      button.addEventListener("click", () => {
        activeRoleFilter = filter.id;
        renderRoleFilters();
        renderTrackRows();
      });
      container.append(button);
    }
  }

  function createRoleSelect(file) {
    const select = make("select", "role-select");
    select.setAttribute("aria-label", `设置 ${displayName(file)} 的音轨类型`);
    for (const role of roleDefinitions) {
      const option = make("option", "", role.label);
      option.value = role.id;
      option.selected = roleOf(file.id) === role.id;
      select.append(option);
    }
    select.addEventListener("change", () => {
      roleMap[file.id] = select.value;
      saveStorage(ROLE_KEY, roleMap);
      renderTrackRows();
      renderVersionSelect();
      renderPlayer();
    });
    select.addEventListener("click", (event) => event.stopPropagation());
    return select;
  }

  function renderTrackRows() {
    const body = $("#trackRows");
    const query = $("#searchInput").value.trim().toLocaleLowerCase();
    const filtered = sortFiles(files).filter((file) => {
      const matchesRole = activeRoleFilter === "all" || roleOf(file.id) === activeRoleFilter;
      const matchesText = !query || displayName(file).toLocaleLowerCase().includes(query);
      return matchesRole && matchesText;
    });
    body.replaceChildren();
    for (const file of filtered) {
      const row = make("tr", `track-row${file.id === selectedFileId ? " is-selected" : ""}`);
      row.addEventListener("click", () => selectFile(file.id));

      const nameCell = make("td");
      const nameWrap = make("div", "track-name-cell");
      const play = make("button", "track-play-mark", "▶");
      play.type = "button";
      play.title = `试听 ${displayName(file)}`;
      play.setAttribute("aria-label", `试听 ${displayName(file)}`);
      play.addEventListener("click", (event) => { event.stopPropagation(); playFile(file.id); });
      const copy = make("div", "track-title-wrap");
      const name = make("button", "track-title-button", displayName(file));
      name.type = "button";
      name.title = displayName(file);
      name.addEventListener("click", (event) => { event.stopPropagation(); selectFile(file.id); });
      const subtitle = make("span", "track-subtitle", `${originLabel(file.id)} · ${formatSampleRate(file.sample_rate)} · ${Number(file.channels) || "?"} 声道`);
      copy.append(name, subtitle);
      nameWrap.append(play, copy);
      nameCell.append(nameWrap);

      const roleCell = make("td");
      roleCell.append(createRoleSelect(file));
      const durationCell = make("td", "duration-cell", formatDuration(file.duration));
      const actionCell = make("td");
      const actions = make("div", "row-actions");
      const load = make("button", "row-action", "⋯");
      load.type = "button";
      load.title = "在处理工作区中载入";
      load.setAttribute("aria-label", `在处理工作区中载入 ${displayName(file)}`);
      load.addEventListener("click", (event) => { event.stopPropagation(); selectFile(file.id); $("#toolsSection").scrollIntoView({ behavior: "smooth", block: "center" }); });
      actions.append(load);
      actionCell.append(actions);
      row.append(nameCell, roleCell, durationCell, actionCell);
      body.append(row);
    }
    $("#trackCount").textContent = String(files.length);
    $("#sidebarTrackCount").textContent = String(files.length);
    $("#catalogMeta").textContent = files.length ? `${files.length} 个文件 · WAV / MP3` : "WAV / MP3";
    const empty = $("#emptyState");
    empty.hidden = filtered.length !== 0;
    if (!files.length) {
      $("#workspaceSummary").textContent = stateAvailable ? "等待导入或载入演示" : "本地引擎未连接";
      $(".empty-state strong").textContent = "这里还没有音频";
      $(".empty-state span").textContent = "导入 WAV / MP3，或载入合成演示音频。";
    } else if (!filtered.length) {
      $("#workspaceSummary").textContent = `${files.length} 条音轨`;
      $(".empty-state strong").textContent = "没有匹配的音轨";
      $(".empty-state span").textContent = "调整搜索内容或音轨类型筛选。";
    } else {
      const outputCount = files.filter((file) => originMap[file.id] === "process").length;
      $("#workspaceSummary").textContent = `${files.length} 条音轨 · ${outputCount} 个处理结果`;
    }
    $("#emptyImportButton").onclick = () => $("#audioFileInput").click();
  }

  function appendFileOptions(select, includeCurrent = true) {
    select.replaceChildren();
    if (!files.length) {
      const option = make("option", "", "暂无本地音频");
      option.value = "";
      select.append(option);
      select.disabled = true;
      return;
    }
    select.disabled = false;
    const ordered = sortFiles(files);
    for (const file of ordered) {
      const role = roleLabel(roleOf(file.id));
      const option = make("option", "", `${role} · ${displayName(file)}`);
      option.value = file.id;
      select.append(option);
    }
    if (includeCurrent && selectedFileId && getFile(selectedFileId)) select.value = selectedFileId;
  }

  function renderVersionSelect() {
    const select = $("#versionSelect");
    select.replaceChildren();
    for (const file of sortFiles(files)) {
      const option = make("option", "", `${roleLabel(roleOf(file.id))} · ${displayName(file)}`);
      option.value = file.id;
      option.title = displayName(file);
      select.append(option);
    }
    select.disabled = !files.length;
    if (activeFileId && getFile(activeFileId)) select.value = activeFileId;
    else if (selectedFileId && getFile(selectedFileId)) select.value = selectedFileId;
  }

  function renderPlayer() {
    const selected = getFile(selectedFileId);
    const active = getFile(activeFileId);
    $("#selectedFileName").textContent = selected ? displayName(selected) : "未选择音频";
    $("#sourcePlayButton").disabled = !selected;
    $("#playButton").disabled = !active && !selected;
    $("#compactPlayButton").disabled = !active && !selected;
    $("#stopButton").disabled = !active;
    $("#restartButton").disabled = !active && !selected;
    $("#seekInput").disabled = !active;
    $("#compactSeek").disabled = !active;
    $("#playerTitle").textContent = active ? displayName(active) : selected ? displayName(selected) : "未选择曲目";
    $("#compactTitle").textContent = active ? displayName(active) : selected ? displayName(selected) : "未选择曲目";
    const subline = active ? `${roleLabel(roleOf(active.id))} · ${originLabel(active.id)}` : selected ? `${roleLabel(roleOf(selected.id))} · ${originLabel(selected.id)}` : "选择素材开始试听";
    $("#playerSubtitle").textContent = subline;
    $("#compactSubtitle").textContent = active ? `${roleLabel(roleOf(active.id))} · 本地播放` : "本地音频";
    const playing = !audio.paused && !audio.ended;
    const symbol = playing ? "Ⅱ" : "▶";
    $("#playIcon").textContent = symbol;
    $("#compactPlayIcon").textContent = symbol;
    $("#playButton").setAttribute("aria-label", playing ? "暂停" : "播放");
    $("#compactPlayButton").setAttribute("aria-label", playing ? "暂停" : "播放");
    $("#versionSelect").disabled = !files.length;
    if (active && $("#versionSelect").value !== active.id) $("#versionSelect").value = active.id;
  }

  function renderWorkspace() {
    renderRoleFilters();
    renderTrackRows();
    appendFileOptions($("#sourceSelect"));
    renderVersionSelect();
    renderPlayer();
    renderOperationPanel();
    renderLyricsPanel();
  }

  function selectFile(fileId) {
    if (!getFile(fileId)) return;
    selectedFileId = fileId;
    $("#sourceSelect").value = fileId;
    renderTrackRows();
    renderPlayer();
    renderOperationPanel();
    renderLyricsPanel();
    loadWaveform(fileId);
  }

  function setActiveFile(fileId) {
    const file = getFile(fileId);
    if (!file) return;
    activeFileId = fileId;
    selectedFileId = fileId;
    const resolved = new URL(file.url, window.location.href).href;
    if (audio.src !== resolved) {
      audio.src = resolved;
      audio.load();
    }
    $("#sourceSelect").value = fileId;
    renderTrackRows();
    renderPlayer();
    renderOperationPanel();
    renderLyricsPanel();
    loadWaveform(fileId);
  }

  async function playFile(fileId) {
    const file = getFile(fileId);
    if (!file) return;
    setActiveFile(fileId);
    try {
      await audio.play();
    } catch (error) {
      notify(error?.message || "浏览器无法播放该音频文件。", "error");
    }
  }

  function setRangeValue(input, display, suffix = "") {
    display.textContent = `${input.value}${suffix}`;
  }

  function addParameterField(container, title, control, extraClass = "") {
    const field = make("div", `parameter-field${extraClass ? ` ${extraClass}` : ""}`);
    const label = make("label", "", title);
    if (control.id) label.htmlFor = control.id;
    field.append(label, control);
    container.append(field);
  }

  function addRangeControl(container, config) {
    const field = make("div", "parameter-field");
    const label = make("label", "", config.label);
    label.htmlFor = config.id;
    const rangeWrap = make("div", "range-field");
    const input = document.createElement("input");
    input.type = "range";
    input.id = config.id;
    input.min = String(config.min);
    input.max = String(config.max);
    input.step = String(config.step);
    input.value = String(config.value);
    const value = make("output", "range-value", `${config.value}${config.suffix}`);
    input.addEventListener("input", () => setRangeValue(input, value, config.suffix));
    rangeWrap.append(input, value);
    field.append(label, rangeWrap);
    container.append(field);
  }

  function renderOperationPanel() {
    const source = getFile($("#sourceSelect").value || selectedFileId);
    $("#selectedFileName").textContent = source ? displayName(source) : "未选择音频";
    $("#sourcePlayButton").disabled = !source;
    const config = operations[activeOperation];
    $("#operationDescription").textContent = config.description;
    $$("#operationTabs [data-operation]").forEach((button) => button.classList.toggle("is-active", button.dataset.operation === activeOperation));
    const container = $("#operationParams");
    container.replaceChildren();

    if (!files.length) {
      container.append(make("div", "operation-empty", "导入或载入音频后可使用本地处理工具。"));
    } else if (activeOperation === "trim") {
      const grid = make("div", "parameter-grid");
      const start = document.createElement("input");
      start.type = "number"; start.id = "trimStart"; start.className = "number-input"; start.min = "0"; start.step = "0.01"; start.value = "0";
      const end = document.createElement("input");
      end.type = "number"; end.id = "trimEnd"; end.className = "number-input"; end.min = "0"; end.step = "0.01"; end.value = source ? String(Math.max(0.01, Math.min(Number(source.duration) || 0.01, 30))) : "30";
      if (source) { start.max = String(source.duration); end.max = String(source.duration); }
      addParameterField(grid, "开始位置（秒）", start);
      addParameterField(grid, "结束位置（秒）", end);
      container.append(grid);
    } else if (activeOperation === "pitch") {
      const grid = make("div", "parameter-grid");
      addRangeControl(grid, { label: "半音", id: "pitchSemitones", min: -12, max: 12, step: 1, value: 0, suffix: " st" });
      const note = make("div", "parameter-field");
      note.append(make("span", "", "输出音高"), make("div", "range-value", "可逆的音高变化"));
      grid.append(note);
      container.append(grid);
    } else if (activeOperation === "tempo") {
      const grid = make("div", "parameter-grid");
      addRangeControl(grid, { label: "播放速度", id: "tempoValue", min: 0.5, max: 2, step: 0.05, value: 1, suffix: "×" });
      const note = make("div", "parameter-field");
      note.append(make("span", "", "速度范围"), make("div", "range-value", "0.50× — 2.00×"));
      grid.append(note);
      container.append(grid);
    } else if (activeOperation === "mix") {
      const grid = make("div", "parameter-grid");
      const otherField = make("div", "parameter-field");
      const otherLabel = make("label", "", "混入音轨");
      otherLabel.htmlFor = "otherSourceSelect";
      const other = document.createElement("select");
      other.id = "otherSourceSelect";
      other.className = "control-select";
      for (const file of sortFiles(files.filter((item) => item.id !== source?.id))) {
        const option = make("option", "", `${roleLabel(roleOf(file.id))} · ${displayName(file)}`);
        option.value = file.id;
        other.append(option);
      }
      if (!other.options.length) {
        const option = make("option", "", "需要另一条音轨"); option.value = ""; other.append(option); other.disabled = true;
      }
      otherField.append(otherLabel, other);
      grid.append(otherField);
      addRangeControl(grid, { label: "主音轨增益", id: "vocalGain", min: 0, max: 2, step: 0.05, value: 1, suffix: "×" });
      addRangeControl(grid, { label: "混入音轨增益", id: "otherGain", min: 0, max: 2, step: 0.05, value: 1, suffix: "×" });
      container.append(grid);
    } else if (activeOperation === "normalize") {
      const grid = make("div", "parameter-grid");
      const detail = make("div", "parameter-field");
      detail.append(make("span", "", "标准化目标"), make("div", "range-value", "-19 LUFS · 峰值 -1.5 dBTP"));
      const method = make("div", "parameter-field");
      method.append(make("span", "", "处理位置"), make("div", "range-value", "本机 FFmpeg"));
      grid.append(detail, method);
      container.append(grid);
    } else {
      const grid = make("div", "parameter-grid");
      const detail = make("div", "parameter-field");
      detail.append(make("span", "", "输入格式"), make("div", "range-value", source?.name?.split(".").pop()?.toUpperCase() || "WAV / MP3"));
      const output = make("div", "parameter-field");
      output.append(make("span", "", "输出编码"), make("div", "range-value", "PCM / MP3"));
      grid.append(detail, output);
      container.append(grid);
    }

    const unavailable = !capabilities[activeOperation];
    const noMixPair = activeOperation === "mix" && files.filter((file) => file.id !== source?.id).length === 0;
    $("#processButton").disabled = operationBusy || !stateAvailable || !source || unavailable || noMixPair;
    $("#formatSelect").disabled = operationBusy || !stateAvailable || !source;
    if (unavailable && stateAvailable) setOperationFeedback("此处理能力未在本地 API 中启用。", "error");
    else if (noMixPair) setOperationFeedback("混音需要至少两条已载入的音轨。", "");
    else if (!operationBusy && !operationFeedbackKind) setOperationFeedback("处理在本机执行，完成后会加入素材库。", "");
  }

  function readLyrics(fileId) {
    if (!fileId || !lyricsMap[fileId] || typeof lyricsMap[fileId] !== "object") return { raw: "", offset: 0 };
    return { raw: String(lyricsMap[fileId].raw || ""), offset: Number(lyricsMap[fileId].offset) || 0 };
  }

  function parseLyrics(raw) {
    const entries = [];
    let headerOffset = 0;
    const offsetMatch = raw.match(/^\s*\[offset\s*:\s*([-+]?\d+)\s*\]/im);
    if (offsetMatch) headerOffset = Number(offsetMatch[1]) || 0;
    const linePattern = /\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]/g;
    for (const line of raw.split(/\r?\n/)) {
      linePattern.lastIndex = 0;
      const matches = [...line.matchAll(linePattern)];
      if (!matches.length) continue;
      const text = line.replace(linePattern, "").trim();
      for (const match of matches) {
        const fraction = match[3] || "0";
        const milliseconds = Number(`0.${fraction}`) * 1000;
        const seconds = Number(match[1]) * 60 + Number(match[2]) + milliseconds / 1000;
        entries.push({ time: seconds + headerOffset / 1000, text });
      }
    }
    entries.sort((a, b) => a.time - b.time);
    return entries;
  }

  function renderLyricsPanel() {
    const file = getFile(selectedFileId);
    const input = $("#lyricsEditor");
    const offset = $("#lyricsOffset");
    const record = readLyrics(selectedFileId);
    const changed = input.dataset.fileId !== (selectedFileId || "");
    if (changed) {
      input.dataset.fileId = selectedFileId || "";
      input.value = record.raw;
      offset.value = String(Math.max(-5000, Math.min(5000, record.offset)));
    }
    input.disabled = !file;
    offset.disabled = !file;
    $("#importLyricsButton").disabled = !file;
    $("#popoutLyricsButton").disabled = !file;
    $("#lyricsFileLabel").textContent = file ? displayName(file) : "先选择一条音轨";
    setRangeValue(offset, $("#offsetValue"), " ms");
    renderLyricsLines();
  }

  function lyricClock(seconds) {
    const centiseconds = Math.max(0, Math.floor(seconds * 100));
    const minutes = Math.floor(centiseconds / 6000);
    const remainder = Math.floor(centiseconds / 100) % 60;
    const fraction = centiseconds % 100;
    return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}.${String(fraction).padStart(2, "0")}`;
  }

  function currentLyricTime() {
    if (!selectedFileId || selectedFileId !== activeFileId) return 0;
    return Number(audio.currentTime) || 0;
  }

  function renderLyricsLines() {
    const file = getFile(selectedFileId);
    const container = $("#lyricsLines");
    const record = readLyrics(selectedFileId);
    const entries = parseLyrics(record.raw);
    const now = currentLyricTime();
    const adjusted = entries.map((entry) => ({ ...entry, time: Math.max(0, entry.time + record.offset / 1000) }));
    let currentIndex = -1;
    for (let index = 0; index < adjusted.length; index += 1) {
      if (adjusted[index].time <= now + 0.015) currentIndex = index;
      else break;
    }
    container.replaceChildren();
    if (!file) {
      container.append(make("div", "lyrics-placeholder", "选择音轨后可导入并同步显示 LRC 歌词。"));
      syncPopupLyrics([], -1, null);
      return;
    }
    if (!adjusted.length) {
      container.append(make("div", "lyrics-placeholder", "导入歌词后，将在播放时同步定位。"));
      syncPopupLyrics([], -1, file);
      $("#lyricsPreviewTime").textContent = formatDuration(currentLyricTime());
      return;
    }
    adjusted.forEach((entry, index) => {
      const line = make("div", `lyric-line${index === currentIndex ? " is-current" : ""}`);
      const time = make("span", "lyric-time", lyricClock(entry.time));
      const text = make("span", "", entry.text || "♪");
      line.append(time, text);
      container.append(line);
    });
    const currentNode = container.querySelector(".lyric-line.is-current");
    if (currentNode) currentNode.scrollIntoView({ block: "nearest" });
    $("#lyricsPreviewTime").textContent = formatDuration(currentLyricTime());
    syncPopupLyrics(adjusted, currentIndex, file);
  }

  function syncPopupLyrics(entries, currentIndex, file) {
    if (!lyricPopup || lyricPopup.closed) { lyricPopup = null; return; }
    const doc = lyricPopup.document;
    const title = doc.getElementById("radarLyricTitle");
    const mainLine = doc.getElementById("radarLyricCurrent");
    const nextLine = doc.getElementById("radarLyricNext");
    const timer = doc.getElementById("radarLyricTime");
    if (!title || !mainLine || !nextLine || !timer) return;
    title.textContent = file ? displayName(file) : "RADAR Cover Lab";
    mainLine.textContent = currentIndex >= 0 ? (entries[currentIndex]?.text || "♪") : "♪";
    nextLine.textContent = currentIndex >= 0 ? (entries[currentIndex + 1]?.text || "") : (entries[0]?.text || "");
    timer.textContent = formatDuration(currentLyricTime());
  }

  function openLyricsWindow() {
    const file = getFile(selectedFileId);
    if (!file) return;
    lyricPopup = window.open("", "radar-cover-lyrics", "width=430,height=300,resizable=yes");
    if (!lyricPopup) { notify("浏览器阻止了独立歌词窗。", "error"); return; }
    const doc = lyricPopup.document;
    doc.documentElement.lang = "zh-CN";
    doc.title = "RADAR Cover Lab · Lyrics";
    doc.head.replaceChildren();
    const style = doc.createElement("style");
    style.textContent = "*{box-sizing:border-box}body{margin:0;padding:26px;background:#09111f;color:#e8f0fa;font:14px -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif}.top{display:flex;justify-content:space-between;align-items:center;color:#8292a8;font-size:11px}.brand{color:#57d7ce;font-weight:700;letter-spacing:1px}.song{margin-top:30px;color:#9aaabe;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.current{margin-top:22px;color:#e1f5c4;font-size:23px;line-height:1.5;min-height:70px}.next{color:#708198;font-size:14px;line-height:1.5;min-height:24px}";
    doc.head.append(style);
    doc.body.replaceChildren();
    const header = doc.createElement("div"); header.className = "top";
    const brand = doc.createElement("strong"); brand.className = "brand"; brand.textContent = "RADAR / LYRICS";
    const timer = doc.createElement("span"); timer.id = "radarLyricTime"; timer.textContent = "00:00";
    header.append(brand, timer);
    const title = doc.createElement("div"); title.className = "song"; title.id = "radarLyricTitle"; title.textContent = displayName(file);
    const current = doc.createElement("div"); current.className = "current"; current.id = "radarLyricCurrent";
    const next = doc.createElement("div"); next.className = "next"; next.id = "radarLyricNext";
    doc.body.append(header, title, current, next);
    lyricPopup.focus();
    renderLyricsLines();
  }

  function setCompactMode(enabled) {
    document.body.classList.toggle("is-compact", enabled);
    $("#compactToggle").setAttribute("aria-pressed", String(enabled));
  }

  function syncSeekControls() {
    const duration = Number(audio.duration);
    const current = Number(audio.currentTime) || 0;
    const fraction = Number.isFinite(duration) && duration > 0 ? current / duration : 0;
    const value = String(Math.round(Math.max(0, Math.min(1, fraction)) * 1000));
    $("#seekInput").value = value;
    $("#compactSeek").value = value;
    $("#currentTimeLabel").textContent = formatDuration(current);
    $("#durationLabel").textContent = Number.isFinite(duration) && duration > 0 ? formatDuration(duration) : "00:00";
    $("#compactTime").textContent = formatDuration(current);
    $("#lyricsPreviewTime").textContent = formatDuration(currentLyricTime());
    drawWaveform();
  }

  function seekToFraction(value) {
    const duration = Number(audio.duration);
    if (!Number.isFinite(duration) || duration <= 0) return;
    audio.currentTime = (Number(value) / 1000) * duration;
    syncSeekControls();
  }

  function togglePlay() {
    if (!activeFileId) {
      if (selectedFileId) playFile(selectedFileId);
      return;
    }
    if (audio.paused) {
      audio.play().catch((error) => notify(error?.message || "浏览器无法播放该音频文件。", "error"));
    } else audio.pause();
  }

  function setVolume(value) {
    const volume = Math.max(0, Math.min(1, Number(value)));
    audio.volume = volume;
    $("#volumeInput").value = String(volume);
    $("#compactVolume").value = String(volume);
  }

  function setupCanvas() {
    const canvas = $("#waveformCanvas");
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const width = Math.max(1, Math.round(rect.width * dpr));
    const height = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, width: rect.width, height: rect.height };
  }

  function drawWaveform() {
    const canvasInfo = setupCanvas();
    if (!canvasInfo) return;
    const { ctx, width, height } = canvasInfo;
    ctx.clearRect(0, 0, width, height);
    const midpoint = height / 2;
    const bars = waveform;
    if (!bars || !bars.length) {
      ctx.strokeStyle = "rgba(117,139,162,.23)";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, midpoint); ctx.lineTo(width, midpoint); ctx.stroke();
      return;
    }
    const progress = activeFileId === waveformFileId && Number(audio.duration) > 0 ? audio.currentTime / audio.duration : 0;
    const gap = 1.35;
    const barWidth = Math.max(1, width / bars.length - gap);
    for (let i = 0; i < bars.length; i += 1) {
      const x = (i / bars.length) * width;
      const amplitude = Math.max(1, bars[i] * (height * .44));
      ctx.fillStyle = i / bars.length <= progress ? "#57d7ce" : "rgba(107,139,161,.55)";
      ctx.fillRect(x, midpoint - amplitude, barWidth, amplitude * 2);
    }
    if (activeFileId === waveformFileId && Number(audio.duration) > 0) {
      const x = Math.min(width - 1, Math.max(0, progress * width));
      ctx.fillStyle = "#d9fff8";
      ctx.fillRect(x, 2, 1, height - 4);
    }
  }

  async function loadWaveform(fileId) {
    const file = getFile(fileId);
    const generation = ++waveformGeneration;
    waveform = null;
    waveformFileId = fileId;
    drawWaveform();
    if (!file) { $("#waveStatus").textContent = "载入曲目以分析实际波形"; return; }
    $("#waveStatus").textContent = "正在从音频文件读取波形";
    try {
      const response = await fetch(file.url, { cache: "no-store" });
      if (!response.ok) throw new Error("音频数据不可用");
      const buffer = await response.arrayBuffer();
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) throw new Error("浏览器不支持音频解码");
      if (!audioContext) audioContext = new AudioContextClass();
      const decoded = await audioContext.decodeAudioData(buffer.slice(0));
      if (generation !== waveformGeneration) return;
      const channelCount = decoded.numberOfChannels;
      const data = [];
      for (let channel = 0; channel < channelCount; channel += 1) data.push(decoded.getChannelData(channel));
      const peakCount = Math.max(180, Math.min(900, Math.floor(Math.max(360, $("#waveformCanvas").getBoundingClientRect().width * 1.4))));
      const samplesPerPeak = Math.max(1, Math.floor(decoded.length / peakCount));
      const peaks = [];
      for (let peak = 0; peak < peakCount; peak += 1) {
        const start = peak * samplesPerPeak;
        const end = Math.min(decoded.length, start + samplesPerPeak);
        let maximum = 0;
        for (const channel of data) {
          for (let sample = start; sample < end; sample += 1) maximum = Math.max(maximum, Math.abs(channel[sample]));
        }
        peaks.push(maximum);
      }
      waveform = peaks;
      $("#waveStatus").textContent = `基于实际音频数据 · ${formatSampleRate(decoded.sampleRate)} · ${channelCount} 声道`;
      drawWaveform();
    } catch (error) {
      if (generation !== waveformGeneration) return;
      $("#waveStatus").textContent = error?.message || "无法读取波形；仍可使用播放器。";
      waveform = null;
      drawWaveform();
    }
  }

  document.addEventListener("voice-output", (event) => {
    const id = event.detail?.id;
    if (typeof id !== "string") return;
    originMap[id] = "process"; roleMap[id] = "result";
    saveStorage(ORIGIN_KEY, originMap); saveStorage(ROLE_KEY, roleMap);
  });

  async function refreshState(options = {}) {
    try {
      const response = await fetch(API.state, { cache: "no-store" });
      const data = await responseJson(response);
      files = Array.isArray(data.files) ? data.files.filter((file) => file && typeof file.id === "string" && typeof file.url === "string") : [];
      capabilities = data.capabilities && typeof data.capabilities === "object" ? data.capabilities : {};
      stateAvailable = true;
      setEngineState(true, "本地引擎已连接");
      setOperationState("待命", "idle");
      for (const file of files) {
        if (!roleMap[file.id]) roleMap[file.id] = "unassigned";
        if (!originMap[file.id]) originMap[file.id] = "unknown";
      }
      saveStorage(ROLE_KEY, roleMap);
      saveStorage(ORIGIN_KEY, originMap);
      if (!getFile(selectedFileId)) selectedFileId = files[0]?.id || null;
      if (!getFile(activeFileId)) {
        activeFileId = null;
        if (audio.src) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
      }
      renderWorkspace();
      renderCapabilities();
      document.dispatchEvent(new CustomEvent("catalog-updated", {detail: data}));
      if (selectedFileId && !waveform) loadWaveform(selectedFileId);
      if (options.message) notify(options.message);
      return data;
    } catch (error) {
      stateAvailable = false;
      setEngineState(false, "本地引擎未连接");
      $("#capabilityNote").textContent = "连接本地 API 后读取能力状态。";
      $("#operationFeedback").textContent = error?.message || "无法读取本地素材库。";
      $("#operationFeedback").className = "operation-feedback is-error";
      renderWorkspace();
      return null;
    }
  }

  function renderCapabilities() {
    if (!stateAvailable) return;
    const labels = [
      ["timbre_conversion", "声线转换"],
      ["voice_training", "模型训练"],
      ["mixed_audio_conversion", "带伴奏换声"],
      ["stem_separation", "独立分轨导出"],
    ];
    const available = labels.filter(([key]) => Boolean(capabilities[key])).map(([, label]) => label);
    const unavailable = labels.filter(([key]) => !capabilities[key]).map(([, label]) => label);
    $("#capabilityNote").textContent = available.length ? `本地 API 报告可用：${available.join("、")}。请在 AI 声线区执行。` : `本地 API 未提供：${unavailable.join("、")}。`;
  }

  async function loadDemo() {
    if (!stateAvailable || uploadBusy) { notify("请先等待当前本地操作完成。", "error"); return; }
    const button = $("#demoButton");
    button.disabled = true;
    $("#uploadButton").disabled = true;
    const previous = new Set(files.map((file) => file.id));
    try {
      const response = await fetch(API.demo, { method: "POST" });
      const result = await responseJson(response);
      await refreshState();
      for (const file of files) if (!previous.has(file.id)) originMap[file.id] = "demo";
      if (result.file?.id && getFile(result.file.id)) originMap[result.file.id] = "demo";
      saveStorage(ORIGIN_KEY, originMap);
      renderWorkspace();
      const demoFile = files.find((file) => originMap[file.id] === "demo");
      if (demoFile) {
        selectFile(demoFile.id);
        notify("合成演示音频已载入。此音频为虚构波形，不代表真实人声。", "info");
      } else {
        notify("演示请求已完成，现有素材库已同步。", "info");
      }
    } catch (error) {
      notify(error?.message || "无法生成合成演示音频。", "error");
    } finally {
      button.disabled = false;
      $("#uploadButton").disabled = false;
    }
  }

  async function uploadFiles(fileList) {
    const candidates = Array.from(fileList || []);
    if (!candidates.length) return;
    if (uploadBusy) { notify("文件导入正在进行。", "error"); return; }
    if (!stateAvailable) { notify("请先启动本地音频服务。", "error"); return; }
    uploadBusy = true;
    $("#uploadButton").disabled = true;
    $("#demoButton").disabled = true;
    let uploaded = 0;
    const errors = [];
    for (const file of candidates) {
      if (file.size > MAX_UPLOAD_BYTES) { errors.push(`${file.name} 超过 128 MiB 限制`); continue; }
      const extension = file.name.split(".").pop()?.toLowerCase();
      if (!["wav", "mp3"].includes(extension)) { errors.push(`${file.name}：仅支持 WAV / MP3`); continue; }
      try {
        const response = await fetch(API.upload, {
          method: "POST",
          headers: {
            "Content-Type": "application/octet-stream",
            "X-Filename": encodeURIComponent(file.name),
          },
          body: file,
        });
        const data = await responseJson(response);
        if (!data.file || typeof data.file.id !== "string") throw new Error("服务端未返回音频记录");
        originMap[data.file.id] = "upload";
        roleMap[data.file.id] = roleMap[data.file.id] || "unassigned";
        uploaded += 1;
      } catch (error) {
        errors.push(`${file.name}：${error?.message || "导入失败"}`);
      }
    }
    saveStorage(ORIGIN_KEY, originMap);
    saveStorage(ROLE_KEY, roleMap);
    await refreshState();
    uploadBusy = false;
    $("#uploadButton").disabled = false;
    $("#demoButton").disabled = false;
    if (uploaded && files.length) {
      const latest = files.find((file) => originMap[file.id] === "upload");
      if (latest) selectFile(latest.id);
    }
    if (errors.length) notify(`${uploaded ? `已导入 ${uploaded} 个文件；` : ""}${errors.join("；")}`, "error");
    else if (uploaded) notify(`已将 ${uploaded} 个文件保存到本地素材库。`);
  }

  function createResultRow(file) {
    const row = make("div", "result-row");
    const name = make("span", "result-name", displayName(file));
    name.title = displayName(file);
    const meta = make("span", "result-meta", `${formatDuration(file.duration)} · ${formatSampleRate(file.sample_rate)}`);
    const play = make("button", "row-action", "▶");
    play.type = "button";
    play.title = "试听处理结果";
    play.setAttribute("aria-label", "试听处理结果");
    play.addEventListener("click", () => playFile(file.id));
    const choose = make("button", "row-action", "↗");
    choose.type = "button";
    choose.title = "载入此结果";
    choose.setAttribute("aria-label", "载入此结果");
    choose.addEventListener("click", () => selectFile(file.id));
    const download = make("a", "row-action", "下载");
    download.href = file.url;
    download.download = displayName(file);
    download.title = "下载处理结果";
    download.setAttribute("aria-label", "下载处理结果");
    row.append(name, meta, play, choose, download);
    return row;
  }

  async function runOperation() {
    if (operationBusy || !stateAvailable) return;
    const sourceId = $("#sourceSelect").value;
    if (!sourceId || !getFile(sourceId)) { notify("请选择处理对象。", "error"); return; }
    const body = { operation: activeOperation, file_id: sourceId, format: $("#formatSelect").value };
    if (activeOperation === "trim") {
      body.start = Number($("#trimStart").value);
      body.end = Number($("#trimEnd").value);
    } else if (activeOperation === "pitch") {
      body.semitones = Number($("#pitchSemitones").value);
    } else if (activeOperation === "tempo") {
      body.tempo = Number($("#tempoValue").value);
    } else if (activeOperation === "mix") {
      body.other_id = $("#otherSourceSelect").value;
      body.vocal_gain = Number($("#vocalGain").value);
      body.other_gain = Number($("#otherGain").value);
    }
    operationBusy = true;
    $("#processButton").disabled = true;
    $("#processButton").textContent = "正在处理…";
    setOperationState("本地处理", "busy");
    setOperationFeedback("正在调用本地音频处理服务。", "");
    try {
      const response = await fetch(API.process, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await responseJson(response);
      if (!data.file || typeof data.file.id !== "string") throw new Error("服务端未返回处理后的音频");
      originMap[data.file.id] = "process";
      roleMap[data.file.id] = "result";
      saveStorage(ORIGIN_KEY, originMap);
      saveStorage(ROLE_KEY, roleMap);
      const fresh = await refreshState();
      const result = files.find((file) => file.id === data.file.id) || data.file;
      const resultRows = $("#resultRows");
      resultRows.prepend(createResultRow(result));
      $("#recentResults").hidden = false;
      selectedFileId = result.id;
      renderWorkspace();
      loadWaveform(result.id);
      setOperationState("已完成", "ready");
      setOperationFeedback(`${operations[activeOperation].label}完成：${displayName(result)}`, "success");
      notify("处理完成，结果已加入本地素材库。");
      void fresh;
    } catch (error) {
      setOperationState("处理失败", "error");
      setOperationFeedback(error?.message || "本地音频处理失败。", "error");
      notify(error?.message || "本地音频处理失败。", "error");
    } finally {
      operationBusy = false;
      $("#processButton").innerHTML = "<span aria-hidden=\"true\">↗</span>开始处理";
      renderOperationPanel();
    }
  }

  async function importLyricsFile(file) {
    if (!file || !selectedFileId) return;
    try {
      const raw = await file.text();
      const previous = readLyrics(selectedFileId);
      lyricsMap[selectedFileId] = { raw, offset: previous.offset };
      saveStorage(LYRICS_KEY, lyricsMap);
      $("#lyricsEditor").value = raw;
      renderLyricsLines();
      notify(`已导入歌词文件：${file.name}`);
    } catch (error) {
      notify(error?.message || "无法读取歌词文件。", "error");
    }
  }

  function updateLyricsFromEditor() {
    if (!selectedFileId) return;
    const previous = readLyrics(selectedFileId);
    lyricsMap[selectedFileId] = { raw: $("#lyricsEditor").value, offset: previous.offset };
    saveStorage(LYRICS_KEY, lyricsMap);
    renderLyricsLines();
  }

  function updateLyricsOffset() {
    if (!selectedFileId) return;
    const previous = readLyrics(selectedFileId);
    lyricsMap[selectedFileId] = { raw: previous.raw, offset: Number($("#lyricsOffset").value) || 0 };
    saveStorage(LYRICS_KEY, lyricsMap);
    setRangeValue($("#lyricsOffset"), $("#offsetValue"), " ms");
    renderLyricsLines();
  }

  function bindEvents() {
    $("#uploadButton").addEventListener("click", () => $("#audioFileInput").click());
    $("#emptyImportButton").addEventListener("click", () => $("#audioFileInput").click());
    $("#audioFileInput").addEventListener("change", (event) => {
      uploadFiles(event.target.files);
      event.target.value = "";
    });
    $("#demoButton").addEventListener("click", loadDemo);
    $("#refreshButton").addEventListener("click", () => refreshState({ message: "素材库已刷新。" }));
    $("#searchInput").addEventListener("input", renderTrackRows);
    $("#sourceSelect").addEventListener("change", () => {
      selectedFileId = $("#sourceSelect").value || null;
      renderTrackRows(); renderPlayer(); renderOperationPanel(); renderLyricsPanel(); loadWaveform(selectedFileId);
    });
    $$("#operationTabs [data-operation]").forEach((button) => button.addEventListener("click", () => {
      activeOperation = button.dataset.operation;
      setOperationState("待命", "idle");
      setOperationFeedback("处理在本机执行，完成后会加入素材库。", "");
      renderOperationPanel();
    }));
    $("#processButton").addEventListener("click", runOperation);
    $("#sourcePlayButton").addEventListener("click", () => selectedFileId && playFile(selectedFileId));
    $("#versionSelect").addEventListener("change", () => playFile($("#versionSelect").value));
    $("#playButton").addEventListener("click", togglePlay);
    $("#compactPlayButton").addEventListener("click", togglePlay);
    $("#stopButton").addEventListener("click", () => {
      audio.pause();
      try { audio.currentTime = 0; } catch { /* Metadata may not be ready yet. */ }
      syncSeekControls();
    });
    $("#restartButton").addEventListener("click", () => {
      if (!activeFileId && selectedFileId) { playFile(selectedFileId); return; }
      audio.currentTime = 0;
      audio.play().catch((error) => notify(error?.message || "无法播放该音频。", "error"));
    });
    $("#seekInput").addEventListener("input", (event) => seekToFraction(event.target.value));
    $("#compactSeek").addEventListener("input", (event) => seekToFraction(event.target.value));
    $("#volumeInput").addEventListener("input", (event) => setVolume(event.target.value));
    $("#compactVolume").addEventListener("input", (event) => setVolume(event.target.value));
    $("#compactToggle").addEventListener("click", () => setCompactMode(true));
    $("#playerCompactToggle").addEventListener("click", () => setCompactMode(true));
    $("#compactExit").addEventListener("click", () => setCompactMode(false));
    $("#lyricsEditor").addEventListener("input", updateLyricsFromEditor);
    $("#lyricsOffset").addEventListener("input", updateLyricsOffset);
    $("#importLyricsButton").addEventListener("click", () => $("#lrcFileInput").click());
    $("#lrcFileInput").addEventListener("change", (event) => {
      importLyricsFile(event.target.files?.[0]);
      event.target.value = "";
    });
    $("#popoutLyricsButton").addEventListener("click", openLyricsWindow);
    $$(".nav-item[data-target]").forEach((button) => button.addEventListener("click", () => {
      $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item === button));
      document.getElementById(button.dataset.target)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }));

    audio.addEventListener("play", renderPlayer);
    audio.addEventListener("pause", renderPlayer);
    audio.addEventListener("ended", renderPlayer);
    audio.addEventListener("loadedmetadata", () => { syncSeekControls(); renderPlayer(); });
    audio.addEventListener("durationchange", syncSeekControls);
    audio.addEventListener("timeupdate", () => {
      syncSeekControls();
      renderLyricsLines();
    });
    audio.addEventListener("error", () => {
      if (activeFileId) notify("音频文件无法载入或浏览器不支持其编码。", "error");
    });

    const dropTarget = $(".track-table-wrap");
    dropTarget.addEventListener("dragover", (event) => { event.preventDefault(); dropTarget.classList.add("is-dragging"); });
    dropTarget.addEventListener("dragleave", (event) => {
      if (!dropTarget.contains(event.relatedTarget)) dropTarget.classList.remove("is-dragging");
    });
    dropTarget.addEventListener("drop", (event) => {
      event.preventDefault(); dropTarget.classList.remove("is-dragging");
      uploadFiles(event.dataTransfer?.files);
    });
    window.addEventListener("resize", drawWaveform);
    window.addEventListener("storage", (event) => {
      if (event.key === LYRICS_KEY) {
        Object.assign(lyricsMap, readStorage(LYRICS_KEY, {}));
        renderLyricsPanel();
      }
    });
  }

  function init() {
    bindEvents();
    setVolume($("#volumeInput").value);
    renderWorkspace();
    renderLyricsPanel();
    refreshState();
  }

  init();
})();
