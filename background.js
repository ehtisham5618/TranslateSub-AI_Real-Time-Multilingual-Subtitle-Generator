// Force the extension to default to OFF on browser startup or installation.
chrome.runtime.onStartup.addListener(() => {
  chrome.storage.local.set({ subtitlesEnabled: false });
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ subtitlesEnabled: false });
});
