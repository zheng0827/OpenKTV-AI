import { app, BrowserWindow, ipcMain } from 'electron';
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
let pythonProcess = null;
let playerService = null;
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

ipcMain.handle('player:open', async () => playerService?.openCurrent());
ipcMain.handle('player:close', async () => playerService?.close());

app.whenReady().then(async () => {
  try {
    process.env.KTV_LOG_DIR = path.join(app.getPath('userData'), 'logs');
    pythonProcess = await startPythonService();
    playerService = await startServer(serverPort);
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
  if (pythonProcess || playerService) {
    event.preventDefault();
    Promise.allSettled([
      playerService?.close(),
      pythonProcess ? stopPythonService(pythonProcess) : Promise.resolve(),
    ]).finally(() => {
      pythonProcess = null;
      playerService = null;
      app.quit();
    });
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});