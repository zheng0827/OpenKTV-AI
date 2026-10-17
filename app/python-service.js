import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const startupTimeoutMs = Number(process.env.KTV_PYTHON_STARTUP_TIMEOUT_MS) || 120000;

function pythonCommand() {
  if (process.env.KTV_PYTHON_EXECUTABLE) return process.env.KTV_PYTHON_EXECUTABLE;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function delay(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

export async function startPythonService() {
  const port = Number(process.env.KTV_PORT) || 5000;
  const healthUrl = `http://127.0.0.1:${port}/health`;
  const logDirectory = process.env.KTV_LOG_DIR || path.join(projectRoot, 'logs');
  fs.mkdirSync(logDirectory, { recursive: true });
  const logStream = fs.createWriteStream(path.join(logDirectory, 'python-service.log'), { flags: 'w', encoding: 'utf8' });
  logStream.on('error', () => console.error('[Python] Could not write service logs.'));
  const childEnv = { ...process.env };
  if (process.platform === 'win32') {
    childEnv.PYTHONUTF8 = '1';
    childEnv.PYTHONIOENCODING = 'utf-8';
  }
  const child = spawn(pythonCommand(), ['-m', 'core.service'], {
    cwd: projectRoot,
    env: childEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    detached: process.platform !== 'win32',
  });
  for (const [label, stream] of [['OUT', child.stdout], ['ERR', child.stderr]]) {
    stream.setEncoding('utf8');
    stream.on('data', text => logStream.write(`[${new Date().toISOString()}][${label}] ${text}`));
  }
  let exitCode = null;
  child.once('exit', code => { exitCode = code ?? 1; });
  child.once('error', error => { exitCode = 1; console.error('[Python] Failed to start processing service:', error.message); });
  child.once('exit', () => logStream.end());

  const deadline = Date.now() + startupTimeoutMs;
  while (Date.now() < deadline) {
    if (exitCode !== null) {
      throw new Error(`Python 處理服務提前結束（exit ${exitCode}）；請確認 Python 環境與 requirements.txt。`);
    }
    try {
      const response = await fetch(healthUrl, { signal: AbortSignal.timeout(1000) });
      if (response.ok) {
        const health = await response.json();
        if (health.ok && health.service === 'python') return child;
      }
    } catch {
      // The service may still be importing its processing dependencies.
    }
    await delay(500);
  }

  await stopPythonService(child);
  throw new Error(`等待 Python 處理服務逾時（${startupTimeoutMs} ms）。請檢查 Python 相依套件與連接埠 ${port}。`);
}

export async function stopPythonService(child) {
  if (!child || child.exitCode !== null || child.killed) return;
  if (process.platform === 'win32' && child.pid) {
    await new Promise(resolve => {
      const killer = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], {
        stdio: 'ignore',
        windowsHide: true,
      });
      killer.once('error', () => resolve());
      killer.once('close', () => resolve());
    });
    return;
  }
  await new Promise(resolve => {
    const timeout = setTimeout(() => {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch {
        child.kill('SIGKILL');
      }
      resolve();
    }, 5000);
    child.once('exit', () => {
      clearTimeout(timeout);
      resolve();
    });
    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch {
      child.kill('SIGTERM');
    }
  });
}
