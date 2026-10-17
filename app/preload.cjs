const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ktvDesktop', {
  openPlayer: () => ipcRenderer.invoke('player:open'),
  closePlayer: () => ipcRenderer.invoke('player:close'),
});
