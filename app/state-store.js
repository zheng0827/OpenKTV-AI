import fs from 'fs';
import path from 'path';

function ensureDirectory(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function clampMusicVol(value, fallback = 0.8) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(0, Math.min(1, number));
}

function sanitizeState(state, songsById) {
  const queue = [];
  const history = [];
  const sourceQueue = Array.isArray(state?.queue) ? state.queue : [];
  const sourceHistory = Array.isArray(state?.history) ? state.history : [];

  for (const [index, item] of sourceQueue.entries()) {
    const song = songsById.get(String(item?.id || ''));
    if (!song || !song.video_filename) continue;
    queue.push({
      ...song,
      requestedBy: String(item?.requestedBy || '').trim().slice(0, 24),
      queueId: String(item?.queueId || `${song.id}_restored_${index}`),
    });
  }

  for (const item of sourceHistory) {
    const song = songsById.get(String(item?.id || ''));
    if (!song || !song.video_filename) continue;
    history.push({
      ...song,
      requestedBy: String(item?.requestedBy || '').trim().slice(0, 24),
      queueId: String(item?.queueId || ''),
    });
    if (history.length >= 100) break;
  }

  const persisted = state?.playerState || {};
  const playerState = {
    isPlaying: Boolean(persisted.isPlaying) && queue.length > 0,
    audioMode: persisted.audioMode === 'accompaniment' || persisted.audioMode === 'original'
      ? persisted.audioMode
      : 'original',
    musicVol: clampMusicVol(persisted.musicVol),
    position: Math.max(0, Number(persisted.position) || 0),
  };

  return { queue, history, playerState };
}

export function loadPlaybackState(filePath, songs = []) {
  try {
    if (!fs.existsSync(filePath)) return null;
    const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    const songsById = new Map((songs || []).map(song => [String(song.id || ''), song]));
    return sanitizeState(parsed, songsById);
  } catch {
    return null;
  }
}

export function persistPlaybackState(filePath, state) {
  ensureDirectory(filePath);
  const payload = {
    version: 1,
    updatedAt: new Date().toISOString(),
    queue: state.queue,
    history: state.history,
    playerState: state.playerState,
  };
  fs.writeFileSync(filePath, JSON.stringify(payload, null, 2), 'utf8');
}
