import { app, BrowserWindow } from 'electron';
import path from 'path';
import { fileURLToPath } from 'url';
import { startServer } from './server.js';
import { startPythonService, stopPythonService } from './python-service.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverPort = Number(process.env.KTV_NODE_PORT) || 3000;
let mainWindow = null;
let pythonProcess = null;
let isQuitting = false;

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1920,
    height: 1080,
    backgroundColor: '#050505',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true
    },
    autoHideMenuBar: true,
    title: 'KTV Enterprise Host System'
  });

  mainWindow.maximize();

  // 載入本地 Express 提供的電視主螢幕播放介面
  await mainWindow.loadURL(`http://localhost:${serverPort}/console`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  try {
    pythonProcess = await startPythonService();
    await startServer(serverPort);
    await createWindow();
  } catch (error) {
    console.error('[OpenKTV] 啟動失敗：', error.message);
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