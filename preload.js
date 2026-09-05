const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  startEngine: () => ipcRenderer.invoke('start-engine'),
  search: (keyword) => ipcRenderer.invoke('search', keyword),
  searchMany: (keywords) => ipcRenderer.invoke('search-many', keywords),
  saveHit: (hit) => ipcRenderer.invoke('save-hit', hit),
  sendDiscord: (keyword, hits) => ipcRenderer.invoke('send-discord', { keyword, hits }),
  indexStatus: () => ipcRenderer.invoke('index-status'),
  clearCache: () => ipcRenderer.invoke('clear-cache'),
  reindex: () => ipcRenderer.invoke('reindex'),
  openFolder: () => ipcRenderer.invoke('open-folder'),
  onSearchEvent: (callback) =>
    ipcRenderer.on('search-event', (_event, data) => callback(data)),
  loadConfig: () => ipcRenderer.invoke('load-config'),
  saveConfig: (config) => ipcRenderer.invoke('save-config', config),
  minimizeWindow: () => ipcRenderer.invoke('minimize-window'),
  maximizeWindow: () => ipcRenderer.invoke('maximize-window'),
  closeWindow: () => ipcRenderer.invoke('close-window'),
});
