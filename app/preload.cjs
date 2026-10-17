const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ktvDesktop', {
  openPlayer: () => ipcRenderer.invoke('player:open'),
  onPlayerClosed: callback => ipcRenderer.on('player:closed', callback),
});
