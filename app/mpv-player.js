import { spawn } from 'child_process';
import { createConnection } from 'net';
import fs from 'fs';
import os from 'os';
import path from 'path';

export function resolveMpvExecutable({
  platform = process.platform,
  env = process.env,
  resourcesPath = process.resourcesPath,
} = {}) {
  const windows = platform === 'win32';
  const binaryName = windows ? 'mpv.exe' : 'mpv';
  const pathApi = windows ? path.win32 : path;
  const candidates = [];
  const configured = env.KTV_MPV_EXECUTABLE?.trim();
  if (configured) {
    candidates.push(pathApi.isAbsolute(configured)
      ? configured
      : pathApi.dirname(configured) !== '.'
        ? pathApi.resolve(configured)
        : configured);
  } else {
    if (resourcesPath) {
      candidates.push(pathApi.join(resourcesPath, 'mpv', binaryName));
      candidates.push(pathApi.join(resourcesPath, 'app.asar.unpacked', 'bin', binaryName));
    }
    if (windows) {
      for (const root of [env.ProgramFiles, env['ProgramFiles(x86)'], env.LOCALAPPDATA, env.Scoop]) {
        if (!root) continue;
        candidates.push(pathApi.join(root, 'mpv', binaryName));
        candidates.push(pathApi.join(root, 'Programs', 'mpv', binaryName));
        candidates.push(pathApi.join(root, 'Scoop', 'apps', 'mpv', 'current', binaryName));
      }
      if (env.ChocolateyInstall) {
        candidates.push(pathApi.join(env.ChocolateyInstall, 'bin', binaryName));
      }
      candidates.push('C:\\mpv\\mpv.exe');
    }
    candidates.push(binaryName);
  }

  const searchPaths = (env.PATH || env.Path || '').split(pathApi.delimiter);
  for (const candidate of candidates) {
    if (pathApi.isAbsolute(candidate) || candidate.includes(pathApi.sep)) {
      if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return pathApi.resolve(candidate);
      continue;
    }
    for (const directory of searchPaths) {
      if (!directory) continue;
      const fullPath = pathApi.join(directory, candidate);
      if (fs.existsSync(fullPath) && fs.statSync(fullPath).isFile()) return fullPath;
    }
  }
  if (configured) {
    throw new Error(`MPV was not found at "${configured}". Install MPV or set KTV_MPV_EXECUTABLE to the full path of mpv.exe.`);
  }
  throw new Error(`MPV is not installed or not on PATH. Install MPV, restart OpenKTV, or set KTV_MPV_EXECUTABLE to the full path of ${binaryName}.`);
}

export class MpvPlayer {
  constructor({ onEnded = () => {} } = {}) {
    this.onEnded = onEnded;
    this.child = null;
    this.socket = null;
    this.pending = new Map();
    this.requestId = 0;
    this.queueId = null;
    this.audioMode = null;
    this.externalAudioTrackId = null;
    this.isPlaying = null;
    this.musicVolume = null;
    this.lastPositionSync = 0;
    this.status = 'stopped';
    this.error = '';
    this.disabled = false;
    this.startPromise = null;
    this.syncQueue = Promise.resolve();
    this.ipcPath = process.platform === 'win32'
      ? `\\\\.\\pipe\\openktv-mpv-${process.pid}`
      : path.join(os.tmpdir(), `openktv-mpv-${process.pid}.sock`);
  }

  getStatus() {
    return { available: this.status === 'ready', status: this.status, error: this.error };
  }

  ensureStarted() {
    if (this.socket && !this.socket.destroyed) return;
    if (this.startPromise) return this.startPromise;
    this.startPromise = this.startProcess().finally(() => { this.startPromise = null; });
    return this.startPromise;
  }

  async startProcess() {
    if (this.child && this.child.exitCode === null && !this.child.killed) {
      await this.connect();
      return;
    }
    this.status = 'starting';
    this.error = '';
    const displayArgs = process.env.KTV_MPV_FULLSCREEN === '1'
      ? ['--fullscreen', `--screen=${Number(process.env.KTV_MPV_SCREEN) || 0}`]
      : ['--autofit=70%x70%', '--geometry=+20+20'];
    let executable;
    try {
      executable = resolveMpvExecutable();
    } catch (error) {
      this.status = 'unavailable';
      this.error = error.message;
      throw error;
    }
    const child = spawn(executable, [
      '--idle=yes',
      '--force-window=yes',
      '--keep-open=always',
      '--volume-max=150',
      ...displayArgs,
      `--input-ipc-server=${this.ipcPath}`,
    ], { stdio: 'ignore', windowsHide: false });
    this.child = child;
    child.once('error', error => {
      this.status = 'unavailable';
      this.error = error.code === 'ENOENT'
        ? 'MPV executable was not found. Install MPV or set KTV_MPV_EXECUTABLE to its full path.'
        : `Could not start MPV: ${error.message}`;
      if (this.child === child) this.child = null;
    });
    child.once('exit', code => {
      this.status = 'stopped';
      this.socket?.destroy();
      this.socket = null;
      if (this.child === child) this.child = null;
      if (code) this.error = `MPV exited with code ${code}`;
    });
    await this.connect();
  }

  async connect() {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      if (!this.child || this.child.exitCode !== null || this.status === 'unavailable') {
        throw new Error(this.error || 'MPV is unavailable. Install mpv or set KTV_MPV_EXECUTABLE.');
      }
      try {
        await new Promise((resolve, reject) => {
          const socket = createConnection(this.ipcPath);
          const onError = error => {
            socket.destroy();
            reject(error);
          };
          socket.once('error', onError);
          socket.once('connect', () => {
            socket.removeListener('error', onError);
            this.socket = socket;
            socket.setEncoding('utf8');
            let pendingText = '';
            socket.on('data', chunk => {
              pendingText += chunk;
              const lines = pendingText.split('\n');
              pendingText = lines.pop();
              for (const line of lines) {
                try {
                  const message = JSON.parse(line);
                  if (message.request_id) {
                    this.pending.get(message.request_id)?.(message);
                    this.pending.delete(message.request_id);
                  }
                  if (message.event === 'end-file' && message.reason === 'eof') this.onEnded();
                } catch {
                  continue;
                }
              }
            });
            socket.on('close', () => {
              this.socket = null;
              this.status = 'stopped';
            });
            socket.on('error', error => {
              this.status = 'unavailable';
              this.error = error.code === 'ENOENT'
                ? 'MPV executable was not found. Install MPV or set KTV_MPV_EXECUTABLE to its full path.'
                : `Could not start MPV: ${error.message}`;
              for (const resolve of this.pending.values()) resolve({ error: error.message });
              this.pending.clear();
            });
            resolve();
          });
        });
        this.status = 'ready';
        await this.command(['set_property', 'pause', true]);
        return;
      } catch {
        await new Promise(resolve => setTimeout(resolve, 200));
      }
    }
    this.status = 'unavailable';
    throw new Error('Could not connect to the MPV playback process.');
  }

  command(command) {
    if (!this.socket || this.socket.destroyed) return Promise.reject(new Error('MPV is not connected.'));
    const requestId = ++this.requestId;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error('MPV command timed out.'));
      }, 5000);
      this.pending.set(requestId, response => {
        clearTimeout(timeout);
        if (response.error !== 'success') reject(new Error(`MPV command failed: ${response.error}`));
        else resolve(response);
      });
      this.socket.write(`${JSON.stringify({ command, request_id: requestId })}\n`, error => {
        if (!error) return;
        clearTimeout(timeout);
        this.pending.delete(requestId);
        reject(error);
      });
    });
  }

  async load(song, state, resolveMedia) {
    await this.ensureStarted();
    const videoPath = resolveMedia(song?.video_filename);
    if (!videoPath) throw new Error('The current song video file is missing or invalid.');
    await this.command(['loadfile', videoPath, 'replace']);
    this.queueId = song.queueId;
    this.audioMode = null;
    this.externalAudioTrackId = null;
    this.isPlaying = null;
    this.musicVolume = null;
    await this.applyState(song, state, resolveMedia);
    const target = Number(state.position || 0) + (state.isPlaying
      ? Math.max(0, (Date.now() - Number(state.serverTime || Date.now())) / 1000)
      : 0);
    if (target > 0) await this.seek(target);
  }

  async applyState(song, state, resolveMedia) {
    if (!this.socket || this.socket.destroyed) return;
    if (state.audioMode !== this.audioMode) {
      if (state.audioMode === 'accompaniment') {
        const audioPath = resolveMedia(song?.instrumental_filename);
        if (!audioPath) throw new Error('The instrumental audio file is missing or invalid.');
        const tracks = await this.command(['get_property', 'track-list']);
        let track = tracks.data?.find(item => item.type === 'audio' && item.external
          && (item['external-filename'] === audioPath || item.id === this.externalAudioTrackId));
        if (!track) {
          await this.command(['audio-add', audioPath, 'select']);
          const updatedTracks = await this.command(['get_property', 'track-list']);
          track = updatedTracks.data?.find(item => item.type === 'audio' && item.external
            && (item['external-filename'] === audioPath || item.selected));
        }
        if (!track) throw new Error('MPV could not load the instrumental audio track.');
        this.externalAudioTrackId = track.id;
        await this.command(['set_property', 'aid', track.id]);
      } else {
        await this.command(['set_property', 'aid', 'auto']);
        this.externalAudioTrackId = null;
      }
      this.audioMode = state.audioMode;
    }
    if (state.isPlaying !== this.isPlaying) {
      await this.command(['set_property', 'pause', !state.isPlaying]);
      this.isPlaying = state.isPlaying;
    }
    const volume = Math.max(0, Math.min(150, Number(state.musicVol || 0) * 100));
    if (volume !== this.musicVolume) {
      await this.command(['set_property', 'volume', volume]);
      this.musicVolume = volume;
    }
    if (state.isPlaying && Date.now() - this.lastPositionSync >= 4000) {
      this.lastPositionSync = Date.now();
      const current = await this.command(['get_property', 'time-pos']);
      const target = Number(state.position || 0)
        + Math.max(0, (Date.now() - Number(state.serverTime || Date.now())) / 1000);
      if (Number.isFinite(current.data) && Math.abs(current.data - target) > 0.75) {
        await this.seek(target);
      }
    }
  }

  async sync(song, state, resolveMedia) {
    const operation = async () => {
      if (this.disabled) return;
      if (!song) {
        if (this.queueId && this.socket && !this.socket.destroyed) await this.command(['stop']);
        this.queueId = null;
        return;
      }
      if (song.queueId !== this.queueId) {
        await this.load(song, state, resolveMedia);
        return;
      }
      await this.applyState(song, state, resolveMedia);
    };
    this.syncQueue = this.syncQueue.catch(() => {}).then(operation);
    return this.syncQueue;
  }

  async seek(position) {
    if (this.socket && !this.socket.destroyed) await this.command(['seek', Number(position), 'absolute']);
  }

  async open(song, state, resolveMedia) {
    this.disabled = false;
    await this.ensureStarted();
    if (song && song.queueId !== this.queueId) await this.sync(song, state, resolveMedia);
  }

  async close() {
    this.disabled = true;
    if (!this.socket || this.socket.destroyed) return;
    try {
      await this.command(['quit']);
    } finally {
      this.socket.destroy();
      this.socket = null;
      this.child = null;
      this.queueId = null;
      this.audioMode = null;
      this.externalAudioTrackId = null;
      this.isPlaying = null;
      this.musicVolume = null;
      this.lastPositionSync = 0;
      this.status = 'stopped';
      this.syncQueue = Promise.resolve();
    }
  }
}
