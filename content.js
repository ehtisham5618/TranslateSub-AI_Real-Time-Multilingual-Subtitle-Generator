const OVERLAY_ID = "usg-subtitle-overlay";
const WS_URL = "ws://127.0.0.1:8001/ws";
const CHUNK_DURATION_MS = 1000;
const RUNTIME_KEY = "__usgRuntime";
const IS_TOP_FRAME = window.top === window.self;

const previousRuntime = window[RUNTIME_KEY];
if (previousRuntime) {
  if (typeof previousRuntime.stop === "function") {
    previousRuntime.stop();
  }
  if (previousRuntime.observer) {
    previousRuntime.observer.disconnect();
  }
  if (previousRuntime.intervalId) {
    clearInterval(previousRuntime.intervalId);
  }
}

let subtitlesEnabled = true;
let currentVideo = null;
let currentOverlay = null;
let noVideoStreak = 0;
let subtitleLine = "";
let subtitleHistory = [];
let lastSubtitleUpdateTs = 0;
let lastPlaybackTime = null;
let lastPlaybackWallTs = 0;
let lastChunkSentAt = 0;
let lastLatencyMs = null;
let latencyEwmaMs = null;
let subtitleSettings = {
  fontSize: 18,
  fontFamily: 'Inter, sans-serif',
  fontColor: '#ffffff',
  bgOpacity: 85,
  bgColor: '#000000'
};

const SUBTITLE_APPEND_WINDOW_MS = 2800;
const PLAYBACK_JUMP_THRESHOLD_SEC = 4.0;
const PLAYBACK_DRIFT_THRESHOLD_SEC = 2.2;
const RESET_DEBOUNCE_MS = 800;

const MAX_SUBTITLE_LINES = 3;
const SUBTITLE_SILENCE_CLEAR_MS = 2600;
const SUBTITLE_CORRECTION_GUARD_MS = 2200;
const SENTENCE_BREAK_GAP_MS = 1400;
const MAX_LINE_WORDS = 22;

let syncVideo = null;
let detachSyncVideoHandlers = null;
let lastResetSignalTs = 0;

function findBestVideoElement() {
  if (
    currentVideo &&
    currentVideo.isConnected &&
    !currentVideo.ended
  ) {
    return currentVideo;
  }

  const videos = Array.from(document.querySelectorAll("video"));
  if (videos.length === 0) {
    return null;
  }

  const visibleVideos = videos.filter((video) => {
    const rect = video.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && video.isConnected;
  });

  if (visibleVideos.length === 0) {
    return videos[0] || null;
  }

  const playingVideos = visibleVideos.filter(
    (video) => !video.paused && !video.ended && video.readyState > 1
  );
  if (playingVideos.length > 0) {
    return playingVideos[0];
  }

  visibleVideos.sort((a, b) => {
    const rectA = a.getBoundingClientRect();
    const rectB = b.getBoundingClientRect();
    return rectB.width * rectB.height - rectA.width * rectA.height;
  });

  return visibleVideos[0];
}

function getOverlayContainer(video) {
  return video.closest(".html5-video-player") || video.parentElement;
}

function getSupportedAudioMimeType() {
  const mimeTypes = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4"
  ];

  for (const mimeType of mimeTypes) {
    if (MediaRecorder.isTypeSupported(mimeType)) {
      return mimeType;
    }
  }

  return "";
}

class AudioStreamer {
  constructor() {
    this.ws = null;
    this.mediaRecorder = null;
    this.audioStream = null;
    this.video = null;
    this.isRunning = false;
    this.isStarting = false;
    this.startToken = 0;
    this.nextConnectAttemptAt = 0;
    this.reconnectDelayMs = 600;
  }

  async start(video) {
    if (!video) {
      return;
    }

    if (this.isStarting) {
      return;
    }

    if (
      this.isRunning &&
      this.video === video &&
      this.ws &&
      this.ws.readyState === WebSocket.OPEN
    ) {
      return;
    }

    if (Date.now() < this.nextConnectAttemptAt) {
      return;
    }

    this.isStarting = true;
    const token = ++this.startToken;
    this.stopInternal();
    this.video = video;

    try {
      await this.connectWebSocket();
      if (token !== this.startToken) {
        return;
      }

      this.nextConnectAttemptAt = 0;

      const mediaStream = this.getVideoCaptureStream(video);
      const audioTracks = mediaStream.getAudioTracks();

      if (audioTracks.length === 0) {
        throw new Error("No audio track available on the current video.");
      }

      this.audioStream = new MediaStream(audioTracks);

      const recorderOptions = {};
      const supportedMimeType = getSupportedAudioMimeType();
      if (supportedMimeType) {
        recorderOptions.mimeType = supportedMimeType;
      }

      this.mediaRecorder = new MediaRecorder(this.audioStream, recorderOptions);
      this.mediaRecorder.ondataavailable = (event) => {
        this.handleChunk(event.data);
      };
      this.mediaRecorder.onerror = (event) => {
        console.error("MediaRecorder error:", event.error || event);
      };

      this.mediaRecorder.start(CHUNK_DURATION_MS);
      this.isRunning = true;
      console.log("Audio capture started");
    } catch (error) {
      console.error("Failed to start audio capture:", error);
      this.nextConnectAttemptAt = Date.now() + this.reconnectDelayMs;
      this.stop();
    } finally {
      if (token === this.startToken) {
        this.isStarting = false;
      }
    }
  }

  stop() {
    this.startToken += 1;
    this.isStarting = false;
    this.stopInternal();
  }

  stopInternal() {
    this.isRunning = false;

    if (this.mediaRecorder && this.mediaRecorder.state !== "inactive") {
      this.mediaRecorder.stop();
    }
    this.mediaRecorder = null;

    if (this.audioStream) {
      this.audioStream.getTracks().forEach((track) => track.stop());
    }
    this.audioStream = null;

    if (this.ws) {
      this.ws.close();
    }
    this.ws = null;
    this.video = null;
  }

  sendResetSignal(reason = "client_reset") {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }

    const payload = {
      type: "stream_reset",
      reason,
      timestamp: Date.now(),
      videoTimeSec: this.video ? Number(this.video.currentTime || 0) : null,
      playbackRate: this.video ? Number(this.video.playbackRate || 1) : 1
    };

    try {
      this.ws.send(JSON.stringify(payload));
    } catch (error) {
      console.error("Failed to send reset signal:", error);
    }
  }

  getVideoCaptureStream(video) {
    if (typeof video.captureStream === "function") {
      return video.captureStream();
    }

    if (typeof video.mozCaptureStream === "function") {
      return video.mozCaptureStream();
    }

    throw new Error("captureStream API is not supported in this browser.");
  }

  async handleChunk(chunkBlob) {
    if (!this.isRunning || !this.video) {
      return;
    }

    if (this.video.paused || this.video.ended) {
      return;
    }

    if (!(chunkBlob instanceof Blob) || chunkBlob.size === 0) {
      return;
    }

    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }

    try {
      this.ws.send(chunkBlob);
      lastChunkSentAt = Date.now();
    } catch (error) {
      console.error("Failed to send audio chunk:", error);
    }
  }

  connectWebSocket() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(WS_URL);
      let settled = false;

      ws.addEventListener("open", () => {
        this.ws = ws;
        settled = true;
        console.log("WebSocket connected");
        resolve();
      });

      ws.addEventListener("message", (event) => {
        if (this.ws !== ws) {
          return;
        }

        try {
          const payload = JSON.parse(String(event.data || "{}"));
          if (payload.type === "subtitle" && typeof payload.text === "string") {
            // Approximate end-to-end latency: when the subtitle arrives relative to our
            // most recent audio chunk send time. This is a best-effort estimate.
            const now = Date.now();
            if (lastChunkSentAt && now - lastChunkSentAt >= 0 && now - lastChunkSentAt <= 15000) {
              lastLatencyMs = now - lastChunkSentAt;
              latencyEwmaMs = latencyEwmaMs == null ? lastLatencyMs : (0.25 * lastLatencyMs + 0.75 * latencyEwmaMs);
              try {
                chrome.runtime.sendMessage({
                  type: "USG_LATENCY_UPDATE",
                  latencyMs: Math.round(latencyEwmaMs)
                });
              } catch (error) {
                // Ignore if extension context isn't available.
              }
            }
            updateOverlayText(payload.text);
          }
        } catch (error) {
          // Ignore non-JSON websocket messages.
        }
      });

      ws.addEventListener("close", () => {
        if (this.ws === ws) {
          this.ws = null;
        }
        this.isRunning = false;

        if (this.mediaRecorder && this.mediaRecorder.state !== "inactive") {
          this.mediaRecorder.stop();
        }
        this.mediaRecorder = null;

        if (this.audioStream) {
          this.audioStream.getTracks().forEach((track) => track.stop());
        }
        this.audioStream = null;

        this.nextConnectAttemptAt = Date.now() + this.reconnectDelayMs;
        console.log("WebSocket closed");
      });

      ws.addEventListener("error", (event) => {
        console.error("WebSocket error:", event);
        if (!settled) {
          settled = true;
          reject(new Error("WebSocket connection failed."));
        }
      });
    });
  }
}

const audioStreamer = new AudioStreamer();

function removeOverlay() {
  if (currentOverlay && currentOverlay.parentElement) {
    currentOverlay.remove();
  }
  currentOverlay = null;
  currentVideo = null;
  subtitleLine = "";
  subtitleHistory = [];
  lastSubtitleUpdateTs = 0;
  lastPlaybackTime = null;
  lastPlaybackWallTs = 0;
  lastChunkSentAt = 0;
  lastLatencyMs = null;
  latencyEwmaMs = null;
  if (detachSyncVideoHandlers) {
    detachSyncVideoHandlers();
  }
  detachSyncVideoHandlers = null;
  syncVideo = null;
}

function createOverlay(video) {
  const parent = getOverlayContainer(video);
  if (!parent) {
    return;
  }

  if (getComputedStyle(parent).position === "static") {
    parent.style.position = "relative";
  }

  const overlay = document.createElement("div");
  overlay.id = OVERLAY_ID;
  overlay.className = "usg-subtitle-overlay";
  overlay.textContent = "";
  overlay.style.display = "none";

  parent.appendChild(overlay);
  currentOverlay = overlay;
  currentVideo = video;
  applySubtitleStyles();
}

function setOverlayVisible(visible) {
  if (!currentOverlay) {
    return;
  }

  currentOverlay.style.display = visible ? "" : "none";
}

function clearSubtitlesDisplay() {
  subtitleLine = "";
  subtitleHistory = [];
  lastSubtitleUpdateTs = 0;
  if (currentOverlay) {
    currentOverlay.textContent = "";
  }
  setOverlayVisible(false);
}

function normalizeSubtitleText(text) {
  return String(text || "")
    .replace(/\s+/g, " ")
    .trim();
}

function toWordTokens(text) {
  const normalized = normalizeSubtitleText(text)
    .toLowerCase()
    .replace(/[\u200E\u200F]/g, "")
    .replace(/[\.,!?;:،؟]+/g, "")
    .trim();

  if (!normalized) {
    return [];
  }

  return normalized.split(/\s+/g).filter(Boolean);
}

function normalizeWordForCompare(word) {
  return String(word || "")
    .toLowerCase()
    .replace(/[\u200E\u200F]/g, "")
    .replace(/[\.,!?;:،؟]+/g, "")
    .trim();
}

function jaccardWordSimilarity(a, b) {
  const wordsA = new Set(toWordTokens(a));
  const wordsB = new Set(toWordTokens(b));
  if (wordsA.size === 0 && wordsB.size === 0) {
    return 1;
  }
  if (wordsA.size === 0 || wordsB.size === 0) {
    return 0;
  }

  let intersection = 0;
  for (const word of wordsA) {
    if (wordsB.has(word)) {
      intersection += 1;
    }
  }

  const union = wordsA.size + wordsB.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

function extractExtensionDelta(prevText, nextText) {
  const prevRawWords = normalizeSubtitleText(prevText).split(/\s+/g).filter(Boolean);
  const nextRawWords = normalizeSubtitleText(nextText).split(/\s+/g).filter(Boolean);
  if (prevRawWords.length === 0 || nextRawWords.length === 0) {
    return null;
  }

  if (nextRawWords.length <= prevRawWords.length) {
    return null;
  }

  for (let index = 0; index < prevRawWords.length; index += 1) {
    if (normalizeWordForCompare(prevRawWords[index]) !== normalizeWordForCompare(nextRawWords[index])) {
      return null;
    }
  }

  const deltaWords = nextRawWords.slice(prevRawWords.length);
  const delta = deltaWords.join(" ").trim();
  return delta ? delta : null;
}

function removeWordOverlapSuffixPrefix(prefixText, fragmentText, maxWords = 8) {
  const prefixWords = normalizeSubtitleText(prefixText).split(/\s+/g).filter(Boolean);
  const fragmentWords = normalizeSubtitleText(fragmentText).split(/\s+/g).filter(Boolean);
  if (prefixWords.length === 0 || fragmentWords.length === 0) {
    return normalizeSubtitleText(fragmentText);
  }

  const prefixNorm = prefixWords.map(normalizeWordForCompare).filter(Boolean);
  const fragmentNorm = fragmentWords.map(normalizeWordForCompare).filter(Boolean);
  if (prefixNorm.length === 0 || fragmentNorm.length === 0) {
    return normalizeSubtitleText(fragmentText);
  }

  const search = Math.min(maxWords, prefixNorm.length, fragmentNorm.length);
  let overlap = 0;
  for (let size = search; size >= 1; size -= 1) {
    const prefixSlice = prefixNorm.slice(prefixNorm.length - size);
    const fragmentSlice = fragmentNorm.slice(0, size);
    if (prefixSlice.join("|") === fragmentSlice.join("|")) {
      overlap = size;
      break;
    }
  }

  if (overlap === 0) {
    return normalizeSubtitleText(fragmentText);
  }

  // Map normalized overlap back to raw word count in fragment.
  let rawDropCount = 0;
  let normCount = 0;
  for (const word of fragmentWords) {
    if (normalizeWordForCompare(word)) {
      normCount += 1;
    }
    rawDropCount += 1;
    if (normCount >= overlap) {
      break;
    }
  }

  return fragmentWords.slice(rawDropCount).join(" ").trim();
}

function isSentenceTerminated(text) {
  return /[.!?؟]\s*$/.test(String(text || "").trim());
}

function hexToRgba(hex, opacity) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${opacity / 100})`;
}

function applySubtitleStyles() {
  if (!currentOverlay) return;
  
  currentOverlay.style.fontSize = `${subtitleSettings.fontSize}px`;
  currentOverlay.style.fontFamily = subtitleSettings.fontFamily;
  currentOverlay.style.color = subtitleSettings.fontColor;
  currentOverlay.style.backgroundColor = hexToRgba(subtitleSettings.bgColor, subtitleSettings.bgOpacity);
  currentOverlay.style.whiteSpace = "pre-wrap";
  currentOverlay.style.textAlign = "center";
}

function updateOverlayText(text) {
  if (!currentOverlay) {
    return;
  }

  const cleanText = normalizeSubtitleText(text);
  if (!cleanText) {
    return;
  }

  const now = Date.now();
  setOverlayVisible(true);

  if (!subtitleLine) {
    subtitleLine = cleanText;
    currentOverlay.textContent = subtitleLine;
    lastSubtitleUpdateTs = now;
    return;
  }

  const previousLine = subtitleLine;
  const gapMs = lastSubtitleUpdateTs ? (now - lastSubtitleUpdateTs) : 0;
  const withinCorrectionWindow = gapMs > 0 && gapMs <= SUBTITLE_CORRECTION_GUARD_MS;

  // If the new text is mostly a rewrite of what we already have, prefer it (accuracy).
  const similarity = jaccardWordSimilarity(previousLine, cleanText);
  const incomingWordCount = toWordTokens(cleanText).length;
  const previousWordCount = toWordTokens(previousLine).length;
  const prevNormLower = normalizeSubtitleText(previousLine).toLowerCase();
  const incomingNormLower = normalizeSubtitleText(cleanText).toLowerCase();
  const extendsPrevious = incomingNormLower.startsWith(prevNormLower) && incomingNormLower.length > prevNormLower.length;
  const previousContainsIncoming = prevNormLower.startsWith(incomingNormLower) && prevNormLower.length > incomingNormLower.length;
  const extensionDelta = extractExtensionDelta(previousLine, cleanText);

  // Corrections often come back as the full line again (sometimes very short),
  // so treat high similarity (or prefix-extension) as a rewrite and replace in-place.
  const looksLikeRewrite =
    (incomingWordCount >= 2 && similarity >= 0.72) ||
    (withinCorrectionWindow && incomingWordCount >= 2 && previousWordCount >= 2 && similarity >= 0.62) ||
    extendsPrevious ||
    previousContainsIncoming ||
    extensionDelta !== null;

  const shouldStartNewLine =
    (isSentenceTerminated(previousLine) || gapMs >= SENTENCE_BREAK_GAP_MS) &&
    !looksLikeRewrite;

  if (shouldStartNewLine) {
    subtitleHistory.push(previousLine);
    while (subtitleHistory.length > Math.max(0, MAX_SUBTITLE_LINES - 1)) {
      subtitleHistory.shift();
    }
    subtitleLine = cleanText;
  } else if (looksLikeRewrite) {
    subtitleLine = cleanText;
  } else {
    // Treat as incremental fragment; append, but skip exact repeats.
    if (prevNormLower.endsWith(incomingNormLower)) {
      lastSubtitleUpdateTs = now;
      return;
    }

    // If the incoming chunk already includes the current line (common with partial ASR
    // updates), replace instead of duplicating the line by appending.
    if (incomingNormLower.includes(prevNormLower) && prevNormLower.length >= 8) {
      subtitleLine = cleanText;
      const linesToDisplay = [...subtitleHistory, subtitleLine].filter(Boolean);
      currentOverlay.textContent = linesToDisplay.join("\n");
      lastSubtitleUpdateTs = now;
      return;
    }

    // If it's a clean extension of what we already have, prefer the full updated line.
    if (extensionDelta !== null) {
      subtitleLine = `${previousLine} ${extensionDelta}`.replace(/\s+/g, " ").trim();
      const linesToDisplay = [...subtitleHistory, subtitleLine].filter(Boolean);
      currentOverlay.textContent = linesToDisplay.join("\n");
      lastSubtitleUpdateTs = now;
      return;
    }

    subtitleLine = `${previousLine} ${cleanText}`.replace(/\s+/g, " ").trim();

    const wordCount = subtitleLine.split(/\s+/g).filter(Boolean).length;
    if (wordCount > MAX_LINE_WORDS) {
      subtitleHistory.push(previousLine);
      while (subtitleHistory.length > Math.max(0, MAX_SUBTITLE_LINES - 1)) {
        subtitleHistory.shift();
      }
      subtitleLine = cleanText;
    }
  }

  const linesToDisplay = [...subtitleHistory, subtitleLine].filter(Boolean);
  currentOverlay.textContent = linesToDisplay.join("\n");
  lastSubtitleUpdateTs = now;
}

function handlePlaybackJump(video) {
  const nowSec = performance.now() / 1000;
  if (lastPlaybackTime === null || !isFinite(lastPlaybackTime)) {
    lastPlaybackTime = video.currentTime;
    lastPlaybackWallTs = nowSec;
    return false;
  }

  const wallDelta = Math.max(0, nowSec - lastPlaybackWallTs);
  const expectedDelta = video.paused ? 0 : wallDelta * Math.max(0.1, video.playbackRate || 1);
  const actualDelta = video.currentTime - lastPlaybackTime;

  lastPlaybackTime = video.currentTime;
  lastPlaybackWallTs = nowSec;

  if (Math.abs(actualDelta) >= PLAYBACK_JUMP_THRESHOLD_SEC) {
    return true;
  }

  return Math.abs(actualDelta - expectedDelta) > PLAYBACK_DRIFT_THRESHOLD_SEC;
}

function triggerSyncReset(reason) {
  const now = Date.now();
  if (now - lastResetSignalTs < RESET_DEBOUNCE_MS) {
    return;
  }

  lastResetSignalTs = now;
  subtitleLine = "";
  subtitleHistory = [];
  lastSubtitleUpdateTs = 0;
  if (currentOverlay) {
    currentOverlay.textContent = "";
  }
  setOverlayVisible(false);
  audioStreamer.sendResetSignal(reason);
  audioStreamer.stop();
}

function bindVideoSyncEvents(video) {
  if (syncVideo === video) {
    return;
  }

  if (detachSyncVideoHandlers) {
    detachSyncVideoHandlers();
  }

  const handleSeeking = () => triggerSyncReset("seeking");
  const handleRateChange = () => triggerSyncReset("ratechange");
  video.addEventListener("seeking", handleSeeking);
  video.addEventListener("ratechange", handleRateChange);

  detachSyncVideoHandlers = () => {
    video.removeEventListener("seeking", handleSeeking);
    video.removeEventListener("ratechange", handleRateChange);
  };
  syncVideo = video;
}

function ensureOverlayAndAudio() {
  if (!IS_TOP_FRAME) {
    return;
  }

  if (document.hidden) {
    return;
  }

  if (!subtitlesEnabled) {
    removeOverlay();
    audioStreamer.stop();
    return;
  }

  const video = findBestVideoElement();
  if (!video) {
    noVideoStreak += 1;
    if (noVideoStreak >= 8) {
      removeOverlay();
      audioStreamer.stop();
    }
    return;
  }

  noVideoStreak = 0;

  const isSameOverlayTarget =
    currentVideo === video &&
    currentOverlay &&
    document.body.contains(currentOverlay);

  if (!isSameOverlayTarget) {
    removeOverlay();
    createOverlay(video);
    bindVideoSyncEvents(video);
  } else {
    bindVideoSyncEvents(video);
  }

  if (
    currentOverlay &&
    lastSubtitleUpdateTs &&
    Date.now() - lastSubtitleUpdateTs > SUBTITLE_SILENCE_CLEAR_MS
  ) {
    clearSubtitlesDisplay();
  }

  if (handlePlaybackJump(video)) {
    // Seeking or large playback jump: restart stream so subtitles match current position.
    triggerSyncReset("playback_jump");
  }

  audioStreamer.start(video);
}

function applySubtitlesState(enabled) {
  subtitlesEnabled = Boolean(enabled);
  ensureOverlayAndAudio();
}

if (IS_TOP_FRAME) {
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message && message.type === 'GET_LATENCY') {
      sendResponse({
        latencyMs: latencyEwmaMs != null ? Math.round(latencyEwmaMs) : (lastLatencyMs != null ? Math.round(lastLatencyMs) : null)
      });
      return;
    }

    if (!message || message.type !== "SET_SUBTITLE_STATE") {
      return;
    }

    applySubtitlesState(message.enabled);
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (changes.subtitlesEnabled) {
      applySubtitlesState(changes.subtitlesEnabled.newValue);
    }
    
    // Update settings if changed
    let settingsChanged = false;
    ['fontSize', 'fontFamily', 'fontColor', 'bgOpacity', 'bgColor'].forEach(key => {
      if (changes[key]) {
        subtitleSettings[key] = changes[key].newValue;
        settingsChanged = true;
      }
    });
    
    if (settingsChanged) {
      applySubtitleStyles();
    }
  });

  chrome.storage.local.get({ 
    subtitlesEnabled: false,
    fontSize: 18,
    fontFamily: 'Inter, sans-serif',
    fontColor: '#ffffff',
    bgOpacity: 85,
    bgColor: '#000000'
  }, (settings) => {
    subtitleSettings = {
      fontSize: settings.fontSize,
      fontFamily: settings.fontFamily,
      fontColor: settings.fontColor,
      bgOpacity: settings.bgOpacity,
      bgColor: settings.bgColor
    };
    applySubtitlesState(settings.subtitlesEnabled);
    applySubtitleStyles();
  });

  const observer = new MutationObserver(() => {
    ensureOverlayAndAudio();
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true
  });

  const intervalId = setInterval(() => {
    ensureOverlayAndAudio();
  }, 1000);

  window[RUNTIME_KEY] = {
    stop: () => {
      audioStreamer.stop();
      observer.disconnect();
      clearInterval(intervalId);
    },
    observer,
    intervalId
  };
}
