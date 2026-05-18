// DOM Elements
const mainView = document.getElementById('main-view');
const settingsView = document.getElementById('settings-view');
const navMain = document.getElementById('nav-main');
const navSettings = document.getElementById('nav-settings');

const toggleBtn = document.getElementById('toggle-btn');
const iconContainer = document.getElementById('btn-icon-container');
const btnText = document.getElementById('btn-text');
const statusDot = document.getElementById('status-dot');
const statusLabel = document.getElementById('status-label');
const latencyVal = document.getElementById('latency-val');

// Settings Elements
const fontSizeSlider = document.getElementById('font-size-slider');
const fontSizeVal = document.getElementById('font-size-val');
const fontFamilySelect = document.getElementById('font-family-select');
const fontColorPicker = document.getElementById('font-color-picker');
const fontColorVal = document.getElementById('font-color-val');
const opacitySlider = document.getElementById('opacity-slider');
const opacityVal = document.getElementById('opacity-val');
const bgColorPicker = document.getElementById('bg-color-picker');
const bgColorVal = document.getElementById('bg-color-val');
const previewArea = document.getElementById('subtitle-preview');
const backBtn = document.getElementById('back-btn');

// State
let isRunning = false;
let currentSettings = {
    fontSize: 18,
    fontFamily: 'Inter, sans-serif',
    fontColor: '#ffffff',
    bgOpacity: 85,
    bgColor: '#000000'
};

const ICONS = {
    play: '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-play"><polygon points="6 3 20 12 6 21 6 3"/></svg>',
    stop: '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-square"><rect width="18" height="18" x="3" y="3" rx="2"/></svg>'
};

function hexToRgba(hex, opacity) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${opacity / 100})`;
}

function updatePreview() {
    if (!previewArea) return;
    previewArea.style.fontSize = `${currentSettings.fontSize}px`;
    previewArea.style.fontFamily = currentSettings.fontFamily;
    previewArea.style.color = currentSettings.fontColor;
    previewArea.style.backgroundColor = hexToRgba(currentSettings.bgColor, currentSettings.bgOpacity);
}

// Navigation Logic
function switchView(viewName) {
    if (viewName === 'main') {
        mainView.classList.remove('hidden');
        settingsView.classList.add('hidden');
        navMain.classList.add('active');
        navSettings.classList.remove('active');
    } else {
        mainView.classList.add('hidden');
        settingsView.classList.remove('hidden');
        navMain.classList.remove('active');
        navSettings.classList.add('active');
        updatePreview();
    }
}

navMain.addEventListener('click', () => switchView('main'));
navSettings.addEventListener('click', () => switchView('settings'));
backBtn.addEventListener('click', () => switchView('main'));

// Update UI based on running state
function updateRunningState(enabled) {
    isRunning = enabled;
    if (isRunning) {
        toggleBtn.classList.add('active');
        btnText.textContent = 'Stop Subtitles';
        iconContainer.innerHTML = ICONS.stop;
        statusDot.classList.add('active');
        statusLabel.textContent = 'Live';
        latencyVal.textContent = '—';
        requestLatencyFromActiveTab();
    } else {
        toggleBtn.classList.remove('active');
        btnText.textContent = 'Start Subtitles';
        iconContainer.innerHTML = ICONS.play;
        statusDot.classList.remove('active');
        statusLabel.textContent = 'Idle';
        latencyVal.textContent = '—';
    }
}

function formatLatency(ms) {
    if (ms == null || !isFinite(ms) || ms <= 0) {
        return '—';
    }
    return `${(ms / 1000).toFixed(1)}s`;
}

function setLatency(ms) {
    latencyVal.textContent = formatLatency(ms);
}

function requestLatencyFromActiveTab() {
    if (!isRunning) {
        return;
    }

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs && tabs[0];
        if (!tab || !tab.id || !tab.url || tab.url.startsWith('chrome://')) {
            return;
        }

        chrome.tabs.sendMessage(tab.id, { type: 'GET_LATENCY' }, (response) => {
            if (chrome.runtime.lastError) {
                return;
            }
            if (response && typeof response.latencyMs === 'number') {
                setLatency(response.latencyMs);
            }
        });
    });
}

chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== 'USG_LATENCY_UPDATE') {
        return;
    }
    if (!isRunning) {
        return;
    }
    if (typeof message.latencyMs === 'number') {
        setLatency(message.latencyMs);
    }
});

// Toggle logic
toggleBtn.addEventListener('click', () => {
    isRunning = !isRunning;
    chrome.storage.local.set({ subtitlesEnabled: isRunning }, () => {
        updateRunningState(isRunning);
        notifyTabs(isRunning);
    });
});

// Notify active tab about state change
function notifyTabs(enabled) {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs[0] && tabs[0].id && tabs[0].url && !tabs[0].url.startsWith('chrome://')) {
            chrome.tabs.sendMessage(tabs[0].id, {
                type: 'SET_SUBTITLE_STATE',
                enabled: enabled
            }, () => {
                if (chrome.runtime.lastError) {
                    // Ignore errors if content script not loaded
                }
            });
        }
    });
}

// Load initial state and settings
chrome.storage.local.get({
    subtitlesEnabled: false,
    fontSize: 18,
    fontFamily: 'Inter, sans-serif',
    fontColor: '#ffffff',
    bgOpacity: 85,
    bgColor: '#000000'
}, (settings) => {
    currentSettings = settings;
    updateRunningState(settings.subtitlesEnabled);
    
    // Apply settings to UI
    fontSizeSlider.value = settings.fontSize;
    fontSizeVal.textContent = `${settings.fontSize}px`;
    fontFamilySelect.value = settings.fontFamily;
    fontColorPicker.value = settings.fontColor;
    fontColorVal.textContent = settings.fontColor;
    opacitySlider.value = settings.bgOpacity;
    opacityVal.textContent = `${settings.bgOpacity}%`;
    bgColorPicker.value = settings.bgColor;
    bgColorVal.textContent = settings.bgColor;
    
    updatePreview();
});

// Settings change listeners
fontSizeSlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value);
    fontSizeVal.textContent = `${val}px`;
    currentSettings.fontSize = val;
    chrome.storage.local.set({ fontSize: val });
    updatePreview();
});

fontFamilySelect.addEventListener('change', (e) => {
    currentSettings.fontFamily = e.target.value;
    chrome.storage.local.set({ fontFamily: e.target.value });
    updatePreview();
});

fontColorPicker.addEventListener('input', (e) => {
    const val = e.target.value;
    fontColorVal.textContent = val;
    currentSettings.fontColor = val;
    chrome.storage.local.set({ fontColor: val });
    updatePreview();
});

opacitySlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value);
    opacityVal.textContent = `${val}%`;
    currentSettings.bgOpacity = val;
    chrome.storage.local.set({ bgOpacity: val });
    updatePreview();
});

bgColorPicker.addEventListener('input', (e) => {
    const val = e.target.value;
    bgColorVal.textContent = val;
    currentSettings.bgColor = val;
    chrome.storage.local.set({ bgColor: val });
    updatePreview();
});
