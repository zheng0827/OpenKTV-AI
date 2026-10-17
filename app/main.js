import { app, BrowserWindow, ipcMain, screen } from 'electron';
import { spawnSync } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import { startServer } from './server.js';
import { startPythonService, stopPythonService } from './python-service.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverPort = Number(process.env.KTV_NODE_PORT) || 3000;
if (process.platform === 'win32' && process.stdout.isTTY) {
  spawnSync('cmd.exe', ['/d', '/s', '/c', 'chcp 65001 > nul'], {
    stdio: 'ignore',
    windowsHide: true,
  });
}
let mainWindow = null;
let playerWindow = null;
let pythonProcess = null;
let serverService = null;
let isQuitting = false;

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
    return;
  }
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
  mainWindow.loadURL(`http://127.0.0.1:${serverPort}/operator`);

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
  const playerDisplay = displays.find(display => display.id !== screen.getPrimaryDisplay().id);
  playerWindow = new BrowserWindow({
    ...(playerDisplay ? { x: playerDisplay.bounds.x, y: playerDisplay.bounds.y } : {}),
    width: playerDisplay?.bounds.width || 1280,
    height: playerDisplay?.bounds.height || 720,
    backgroundColor: '#000000',
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
    title: 'OpenKTV Player',
  });
  playerWindow.loadURL(`http://127.0.0.1:${serverPort}/player`);
  if (playerDisplay) playerWindow.setFullScreen(true);
  playerWindow.on('closed', () => {
    playerWindow = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('player:closed');
    }
  });
}

ipcMain.handle('player:open', async () => {
  openPlayerWindow();
  return { ok: true };
});

app.whenReady().then(async () => {
  try {
    process.env.KTV_LOG_DIR = path.join(app.getPath('userData'), 'logs');
    pythonProcess = await startPythonService();
    serverService = await startServer(serverPort);
    createWindow();
  } catch (error) {
    console.error('[OpenKTV] Startup failed:', error.message);
    app.quit();
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('before-quit', event => {
  if (isQuitting) return;
  isQuitting = true;
  if (pythonProcess || serverService) {
    event.preventDefault();
    Promise.allSettled([
      serverService?.close(),
      pythonProcess ? stopPythonService(pythonProcess) : Promise.resolve(),
    ]).finally(() => {
      pythonProcess = null;
      serverService = null;
      app.quit();
    });
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});