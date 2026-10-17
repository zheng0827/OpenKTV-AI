import { spawn } from 'child_process';
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
  const child = spawn(pythonCommand(), ['-m', 'core.service'], {
    cwd: projectRoot,
    env: process.env,
    stdio: 'inherit',
    windowsHide: true,
  });
  let exitCode = null;
  child.once('exit', code => { exitCode = code ?? 1; });
  child.once('error', error => { exitCode = 1; console.error('[Python] 無法啟動處理服務：', error.message); });

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

  child.kill();
  throw new Error(`等待 Python 處理服務逾時（${startupTimeoutMs} ms）。請檢查 Python 相依套件與連接埠 ${port}。`);
}

export async function stopPythonService(child) {
  if (!child || child.exitCode !== null || child.killed) return;
  await new Promise(resolve => {
    const timeout = setTimeout(() => {
      child.kill('SIGKILL');
      resolve();
    }, 5000);
    child.once('exit', () => {
      clearTimeout(timeout);
      resolve();
    });
    child.kill();
  });
}
