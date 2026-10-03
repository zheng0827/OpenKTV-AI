import { app, BrowserWindow } from 'electron';
import path from 'path';
import { fileURLToPath } from 'url';
import { startServer } from './server.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverPort = Number(process.env.KTV_NODE_PORT) || 3000;
let mainWindow = null;

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1920,
    height: 1080,
    backgroundColor: '#050505',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      webSecurity: false,
      allowRunningInsecureContent: true
    },
    autoHideMenuBar: true,
    title: 'KTV Enterprise Host System'
  });

  mainWindow.maximize();

  // 載入本地 Express 提供的電視主螢幕播放介面
  await mainWindow.loadURL(`http://localhost:${serverPort}/player.html`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  // 先於背景啟動 Express 與 WebSocket 伺服器
  await startServer(serverPort);
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});