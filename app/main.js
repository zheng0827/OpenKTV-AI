import { app, BrowserWindow, ipcMain, screen } from 'electron';
import path from 'path';
import { fileURLToPath } from 'url';
import { startServer } from './server.js';
import { startPythonService, stopPythonService } from './python-service.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverPort = Number(process.env.KTV_NODE_PORT) || 3000;
let mainWindow = null;
let playerWindow = null;
let pythonProcess = null;
let isQuitting = false;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 950,
    minWidth: 980,
    minHeight: 640,
    backgroundColor: '#10131b',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs')
    },
    autoHideMenuBar: true,
    title: 'OpenKTV Master'
  });
  mainWindow.loadURL(`http://localhost:${serverPort}/operator`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function openPlayerWindow() {
  if (playerWindow && !playerWindow.isDestroyed()) {
    playerWindow.show();
    playerWindow.focus();
    return;
  }
  const displays = screen.getAllDisplays();
  const display = displays.find(item => item.id !== screen.getPrimaryDisplay().id) || screen.getPrimaryDisplay();
  playerWindow = new BrowserWindow({
    x: display.bounds.x,
    y: display.bounds.y,
    width: display.bounds.width,
    height: display.bounds.height,
    backgroundColor: '#000000',
    fullscreen: true,
    frame: false,
    autoHideMenuBar: true,
    title: 'OpenKTV Player',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs')
    }
  });
  playerWindow.loadURL(`http://localhost:${serverPort}/player`);
  playerWindow.on('closed', () => { playerWindow = null; });
}

function closePlayerWindow() {
  if (playerWindow && !playerWindow.isDestroyed()) playerWindow.close();
}

ipcMain.handle('player:open', openPlayerWindow);
ipcMain.handle('player:close', closePlayerWindow);
ipcMain.handle('player:capture', async () => {
  if (!playerWindow || playerWindow.isDestroyed()) return null;
  const image = await playerWindow.webContents.capturePage();
  return image.toDataURL();
});

app.whenReady().then(async () => {
  try {
    pythonProcess = await startPythonService();
    await startServer(serverPort);
    createWindow();
    if (screen.getAllDisplays().length > 1) openPlayerWindow();
  } catch (error) {
    console.error('[OpenKTV] 啟動失敗：', error.message);
    app.quit();
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
  screen.on('display-added', () => {
    if (!playerWindow) openPlayerWindow();
  });
});

app.on('before-quit', event => {
  if (isQuitting) return;
  isQuitting = true;
  if (pythonProcess) {
    event.preventDefault();
    stopPythonService(pythonProcess).finally(() => {
      pythonProcess = null;
      app.quit();
    });
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});