import './environment.js';
import express from 'express';
import { createServer } from 'http';
import { Server } from 'socket.io';
import { rateLimit } from 'express-rate-limit';
import fs from 'fs';
import os from 'os';
import { timingSafeEqual } from 'crypto';
import { allowsPageAccess, isLocalAddress } from './page-access.js';
import { createMicRelay } from './mic-relay.js';
import { startHttpsProxy } from './https-proxy.js';
import csv from 'csv-parser';
import MiniSearch from 'minisearch';
import QRCode from 'qrcode';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');
const songsDir = path.resolve(projectRoot, process.env.KTV_SONGS_DIR || path.join(projectRoot, 'ktv_songs'));
const catalogPath = path.resolve(projectRoot, process.env.KTV_LIBRARY_INDEX_PATH || path.join(songsDir, 'library_index.csv'));
const legacyCatalogPath = path.join(__dirname, 'songs.csv');

function mediaPath(filename) {
  if (!filename || path.basename(filename) !== filename
    || !['.mp4', '.m4a', '.wav', '.lrc'].includes(path.extname(filename).toLowerCase())) return null;
  try {
    const resolved = path.resolve(songsDir, filename);
    if (!resolved.startsWith(`${songsDir}${path.sep}`) || !fs.existsSync(resolved)) return null;
    const realSongsDir = fs.realpathSync(songsDir);
    const realMediaPath = fs.realpathSync(resolved);
    return realMediaPath.startsWith(`${realSongsDir}${path.sep}`) ? realMediaPath : null;
  } catch {
    return null;
  }
}

function mediaUrl(filename) {
  return `/media/${encodeURIComponent(filename)}`;
}

function normalizeSong(row) {
  const filename = row.video_filename || row.path?.replace(/^\/songs\//, '') || row.song || '';
  const song = {
    ...row,
    id: String(row.id || ''),
    title: row.title || row.song_title || row.name || '',
    artist: row.artist || row.singer || '',
    language: row.language || '',
    char_count: Number(row.char_count) || (row.title || row.song_title || '').length,
    pinyin_abbr: (row.pinyin_abbr || '').toUpperCase(),
    zhuyin_abbr: row.zhuyin_abbr || '',
    video_filename: filename,
    instrumental_filename: row.instrumental_filename || row.instrumental || '',
    lyrics_filename: row.lyrics_filename || row.lyrics_lrc || '',
  };
  if (!song.instrumental_filename && filename.toLowerCase().endsWith('.mp4')) {
    song.instrumental_filename = `${filename.slice(0, -4)}.instrumental.m4a`;
  }
  if (!song.lyrics_filename && filename.toLowerCase().endsWith('.mp4')) {
    song.lyrics_filename = `${filename.slice(0, -4)}.lrc`;
  }
  song.path = filename ? mediaUrl(filename) : '';
  song.instrumental_path = song.instrumental_filename ? mediaUrl(song.instrumental_filename) : '';
  return song;
}

async function readCatalog(filePath) {
  if (!fs.existsSync(filePath)) return [];
  return new Promise((resolve, reject) => {
    const rows = [];
    fs.createReadStream(filePath)
      .pipe(csv())
      .on('data', row => rows.push(normalizeSong(row)))
      .on('end', () => resolve(rows))
      .on('error', reject);
  });
}

function createSearchIndex(songs) {
  const index = new MiniSearch({
    fields: ['title', 'artist', 'lyrics', 'language', 'pinyin_abbr', 'zhuyin_abbr'],
    storeFields: ['id', 'title', 'artist', 'lyrics', 'language', 'char_count', 'path', 'instrumental_path',
      'video_filename', 'instrumental_filename', 'lyrics_filename'],
    searchOptions: { prefix: true, fuzzy: 0.2, boost: { title: 4, artist: 3, lyrics: 1 } },
  });
  index.addAll(songs);
  return index;
}

function createInverseIndex(songs) {
  const index = new Map();
  for (const song of songs) {
    const fields = [song.title, song.artist, song.lyrics, song.pinyin_abbr, song.zhuyin_abbr, song.language];
    const tokens = new Set();
    for (const field of fields) {
      const value = String(field || '').toUpperCase();
      for (const token of value.match(/[\p{L}\p{N}]+/gu) || []) tokens.add(token);
      for (const char of [...value]) {
        if (/^[\p{L}\p{N}]$/u.test(char)) tokens.add(char);
      }
    }
    for (const token of tokens) {
      if (!index.has(token)) index.set(token, new Set());
      index.get(token).add(song.id);
    }
  }
  return index;
}

function inverseSearch(songs, inverseIndex, query) {
  const clean = String(query || '').trim().toUpperCase();
  if (!clean) return songs;
  const terms = clean.match(/[\p{L}\p{N}]+/gu) || [...clean].filter(ch => /^[\p{L}\p{N}]$/u.test(ch));
  if (!terms.length) return [];
  let candidates = null;
  for (const term of terms) {
    const ids = new Set();
    for (const [token, values] of inverseIndex.entries()) {
      if (token.startsWith(term) || token.includes(term)) for (const id of values) ids.add(id);
    }
    candidates = candidates === null ? ids : new Set([...candidates].filter(id => ids.has(id)));
  }
  const byId = new Map(songs.map(song => [song.id, song]));
  return [...(candidates || [])].map(id => byId.get(id)).filter(Boolean);
}

function findSongs(songs, miniSearch, inverseIndex, query, language, charCount, mode = 'keyword', sort = 'relevance') {
  let results = query ? inverseSearch(songs, inverseIndex, query) : songs;
  if (query && results.length < 5) {
    const fuzzy = miniSearch.search(query.toUpperCase())
      .map(result => songs.find(song => song.id === result.id))
      .filter(Boolean);
    results = [...new Map([...results, ...fuzzy].map(song => [song.id, song])).values()];
  }
  const fieldForMode = { zhuyin: 'zhuyin_abbr', pinyin: 'pinyin_abbr', lyrics: 'lyrics', artist: 'artist' }[mode];
  if (query && fieldForMode) {
    const cleanQuery = query.trim().toUpperCase();
    results = results.filter(song => String(song[fieldForMode] || '').toUpperCase().includes(cleanQuery));
  }
  if (language) results = results.filter(song => song.language === language);
  if (charCount) results = results.filter(song => song.char_count === Number(charCount));
  if (sort === 'title') results = [...results].sort((a, b) => a.title.localeCompare(b.title));
  else if (sort === 'artist') results = [...results].sort((a, b) => a.artist.localeCompare(b.artist));
  else if (sort === 'duration') results = [...results].sort((a, b) => (a.duration_seconds || 0) - (b.duration_seconds || 0));
  return results;
}

function parseRange(range, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(range || '');
  if (!match) return null;
  let start;
  let end;
  if (!match[1]) {
    const suffixLength = Number(match[2]);
    if (!suffixLength) return null;
    start = Math.max(0, size - suffixLength);
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : size - 1;
  }
  if (start >= size || end < start) return null;
  return { start, end: Math.min(end, size - 1) };
}

export function startServer(port = Number(process.env.KTV_NODE_PORT) || 3000) {
  return new Promise(async (resolve, reject) => {
    try {
      let songs = await readCatalog(catalogPath);
      if (!fs.existsSync(catalogPath) && catalogPath !== legacyCatalogPath) {
        songs = await readCatalog(legacyCatalogPath);
      }
      let miniSearch = createSearchIndex(songs);
      let inverseIndex = createInverseIndex(songs);
      const app = express();
      const httpServer = createServer(app);
      const io = new Server(httpServer, { cors: { origin: "*", methods: ["GET", "POST"] } });
      const micRelay = createMicRelay(io);
      let advanceQueue = () => {};
      app.use(express.json({ limit: '32kb' }));
      const mediaRequestLimiter = rateLimit({
        windowMs: 60 * 1000,
        limit: 600,
        standardHeaders: 'draft-8',
        legacyHeaders: false,
      });
      const botApiLimiter = rateLimit({
        windowMs: 60 * 1000,
        limit: 60,
        standardHeaders: 'draft-8',
        legacyHeaders: false,
      });
      const pageRequestLimiter = rateLimit({
        windowMs: 60 * 1000,
        limit: 120,
        standardHeaders: 'draft-8',
        legacyHeaders: false,
      });

      const publicDirectory = path.join(__dirname, 'public');
      app.use((req, res, next) => {
        const proxyClientAddress = isLocalAddress(req.socket.remoteAddress)
          ? req.get('x-ktv-proxy-client-ip')
          : null;
        if (!allowsPageAccess(req.originalUrl, proxyClientAddress || req.socket.remoteAddress)) {
          return res.redirect('/remote');
        }
        return next();
      });
      app.use(express.static(publicDirectory));
      app.get(['/', '/home'], pageRequestLimiter, (_req, res) => res.sendFile(path.join(publicDirectory, 'home.html')));
      app.get('/player', (_req, res) => res.redirect('/player.html'));
      app.get(['/remote', '/queue'], (_req, res) => res.redirect('/remote.html'));
      app.get('/operator', pageRequestLimiter, (_req, res) => res.sendFile(path.join(publicDirectory, 'console.html')));
      app.get(['/console', '/ktv'], pageRequestLimiter, (_req, res) => res.sendFile(path.join(publicDirectory, 'console.html')));
      app.get('/api/room', (_req, res) => res.json({
        songCount: songs.length,
        clientCount: io.engine.clientsCount,
        isPlaying: playerState.isPlaying,
        currentSong: currentSong(),
        queueCount: queue.length,
      }));
      app.get('/api/webrtc-config', (_req, res) => {
        const iceServers = String(process.env.KTV_STUN_URLS || 'stun:stun.l.google.com:19302')
          .split(',')
          .map(url => url.trim())
          .filter(Boolean)
          .map(urls => ({ urls }));
        const turnUrls = String(process.env.KTV_TURN_URLS || '').split(',').map(url => url.trim()).filter(Boolean);
        if (turnUrls.length && process.env.KTV_TURN_USERNAME && process.env.KTV_TURN_CREDENTIAL) {
          iceServers.push({
            urls: turnUrls,
            username: process.env.KTV_TURN_USERNAME,
            credential: process.env.KTV_TURN_CREDENTIAL,
          });
        }
        res.setHeader('Cache-Control', 'no-store');
        return res.json({ iceServers });
      });
      app.get('/api/services', pageRequestLimiter, async (_req, res) => {
        const pythonPort = Number(process.env.KTV_PORT) || 5000;
        try {
          const response = await fetch(`http://127.0.0.1:${pythonPort}/health`, {
            signal: AbortSignal.timeout(1500),
          });
          const health = response.ok ? await response.json() : null;
          return res.json({
            node: { ok: true },
            python: { ok: Boolean(health?.ok && health.service === 'python') },
            pythonPort,
          });
        } catch {
          return res.status(503).json({ node: { ok: true }, python: { ok: false }, pythonPort });
        }
      });
      app.get('/api/qr', pageRequestLimiter, async (req, res) => {
        let target;
        const isHttpsProxyRequest = isLocalAddress(req.socket.remoteAddress)
          && Boolean(req.get('x-ktv-proxy-client-ip'));
        const httpsConfigured = Boolean(process.env.KTV_TLS_CERT_PATH && process.env.KTV_TLS_KEY_PATH);
        try {
          target = new URL(`${isHttpsProxyRequest ? 'https' : req.protocol}://${req.get('host')}/remote`);
        } catch {
          return res.status(400).json({ error: '無效的服務網址' });
        }
        if (['localhost', '127.0.0.1', '::1'].includes(target.hostname)) {
          const addresses = Object.values(os.networkInterfaces()).flatMap(items => items || []);
          const lanAddress = addresses.find(item => (item.family === 'IPv4' || item.family === 4) && !item.internal);
          if (lanAddress) target.hostname = lanAddress.address;
        }
        if (httpsConfigured && !isHttpsProxyRequest) {
          target.protocol = 'https:';
          target.port = String(Number(process.env.KTV_HTTPS_PORT) || 3443);
        }
        try {
          const url = target.toString();
          const dataUrl = await QRCode.toDataURL(url, { margin: 2, width: 440, errorCorrectionLevel: 'M' });
          return res.json({ dataUrl, url });
        } catch {
          return res.status(503).json({ error: '無法產生遙控器 QR Code' });
        }
      });
      app.get('/api/resolve_yt_title', pageRequestLimiter, async (req, res) => {
        const pythonPort = Number(process.env.KTV_PORT) || 5000;
        try {
          const target = new URL(`http://127.0.0.1:${pythonPort}/api/resolve_yt_title`);
          target.searchParams.set('url', String(req.query.url || ''));
          const response = await fetch(target, { signal: AbortSignal.timeout(30000) });
          const payload = await response.json();
          return res.status(response.status).json(payload);
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });
      app.patch('/api/catalog/:songId', pageRequestLimiter, async (req, res) => {
        try {
          const response = await fetch(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/${encodeURIComponent(req.params.songId)}`, {
            method: 'PATCH',
            headers: {
              authorization: req.get('authorization') || '',
              'content-type': 'application/json',
            },
            body: JSON.stringify(req.body || {}),
            signal: AbortSignal.timeout(5000),
          });
          return res.status(response.status).json(await response.json());
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });
      app.delete('/api/catalog/:songId', pageRequestLimiter, async (req, res) => {
        try {
          const response = await fetch(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/${encodeURIComponent(req.params.songId)}`, {
            method: 'DELETE',
            headers: { authorization: req.get('authorization') || '' },
            signal: AbortSignal.timeout(5000),
          });
          return res.status(response.status).json(await response.json());
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });
      app.get('/api/songs', pageRequestLimiter, (req, res) => {
        const query = String(req.query.q || '').trim();
        const language = String(req.query.language || '');
        const charCount = Number(req.query.charCount) || 0;
        const offset = Math.max(0, Number.parseInt(req.query.offset, 10) || 0);
        const limit = Math.max(1, Math.min(100, Number.parseInt(req.query.limit, 10) || 50));
        const sort = String(req.query.sort || 'relevance');
        const mode = String(req.query.mode || 'keyword');
        const results = findSongs(songs, miniSearch, inverseIndex, query, language, charCount, mode, sort);
        return res.json({ items: results.slice(offset, offset + limit), total: results.length, offset, limit });
      });
      app.get('/api/bot/room', botApiLimiter, (req, res) => {
        const expected = process.env.KTV_BOT_API_TOKEN || '';
        const supplied = (req.get('authorization') || '').replace(/^Bearer\s+/i, '');
        const expectedBuffer = Buffer.from(expected);
        const suppliedBuffer = Buffer.from(supplied);
        const authorized = expectedBuffer.length > 0 && suppliedBuffer.length === expectedBuffer.length
          && timingSafeEqual(suppliedBuffer, expectedBuffer);
        if (!authorized) return res.sendStatus(401);
        return res.json({ songCount: songs.length, ...snapshot() });
      });
      app.get('/media/:filename', mediaRequestLimiter, (req, res) => {
        const fullPath = mediaPath(req.params.filename);
        if (!fullPath || !fs.existsSync(fullPath) || !fs.statSync(fullPath).isFile()) {
          return res.sendStatus(404);
        }
        const size = fs.statSync(fullPath).size;
        const mimeTypes = {
          '.mp4': 'video/mp4',
          '.m4a': 'audio/mp4',
          '.wav': 'audio/wav',
          '.lrc': 'text/plain; charset=utf-8',
        };
        res.setHeader('Content-Type', mimeTypes[path.extname(fullPath).toLowerCase()] || 'application/octet-stream');
        const rangeHeader = req.headers.range;
        res.setHeader('Accept-Ranges', 'bytes');
        if (!rangeHeader) {
          res.setHeader('Content-Length', size);
          if (req.method === 'HEAD') return res.end();
          return fs.createReadStream(fullPath).pipe(res);
        }
        const range = parseRange(rangeHeader, size);
        if (!range) {
          res.setHeader('Content-Range', `bytes */${size}`);
          return res.sendStatus(416);
        }
        res.status(206);
        res.setHeader('Content-Range', `bytes ${range.start}-${range.end}/${size}`);
        res.setHeader('Content-Length', range.end - range.start + 1);
        if (req.method === 'HEAD') return res.end();
        return fs.createReadStream(fullPath, range).pipe(res);
      });

      const queue = [];
      const history = [];
      const playerState = {
        isPlaying: false,
        audioMode: 'original',
        musicVol: 0.8,
        position: 0,
        startedAt: null,
        serverTime: Date.now(),
      };

      const currentPosition = () => playerState.position + (
        playerState.isPlaying && playerState.startedAt !== null
          ? Math.max(0, (Date.now() - playerState.startedAt) / 1000)
          : 0
      );
      const currentSong = () => queue[0] || null;
      const canPlayInstrumental = song => {
        try {
          const fullPath = mediaPath(song?.instrumental_filename || '');
          const stat = fullPath && fs.statSync(fullPath);
          return Boolean(stat?.isFile() && stat.size > 0);
        } catch {
          return false;
        }
      };
      const snapshot = () => ({
        queue: [...queue],
        history: [...history],
        currentSong: currentSong(),
        state: {
          isPlaying: playerState.isPlaying,
          audioMode: playerState.audioMode,
          musicVol: playerState.musicVol,
          position: currentPosition(),
          serverTime: Date.now(),
        },
      });
      const emitState = () => {
        const payload = snapshot();
        io.emit('sync_state', { queue: payload.queue, history: payload.history, state: payload.state });
        io.emit('queue_updated', payload.queue);
        io.emit('playback_snapshot', payload);
      };
      const startCurrent = () => {
        playerState.position = 0;
        if (!currentSong()) playerState.isPlaying = false;
        else if (queue.length === 1) playerState.isPlaying = true;
        if (currentSong() && playerState.audioMode === 'accompaniment' && !canPlayInstrumental(currentSong())) {
          playerState.audioMode = 'original';
          io.emit('playback_error', { message: '新歌曲沒有有效伴奏檔，已切回原唱模式。' });
        }
        playerState.startedAt = playerState.isPlaying ? Date.now() : null;
      };
      const findSong = song => songs.find(item => item.id === String(song?.id));
      advanceQueue = () => {
        if (queue.length) {
          history.unshift(queue.shift());
          history.splice(100);
        }
        startCurrent();
        emitState();
      };

      app.get('/health', (_req, res) => res.json({ ok: true, songs: songs.length }));

      io.on('connection', socket => {
        micRelay.attach(socket);
        const payload = snapshot();
        socket.emit('sync_state', { queue: payload.queue, history: payload.history, state: payload.state });
        socket.emit('playback_snapshot', payload);
        socket.on('time_ping', clientSentAt => {
          socket.emit('time_pong', { clientSentAt: Number(clientSentAt), serverTime: Date.now() });
        });
        socket.on('search_songs', ({ query = '', language = '', charCount = 0, offset = 0, limit = 50, paginated = false, requestId, mode = 'keyword', sort = 'relevance' } = {}) => {
          const cleanQuery = String(query).trim();
          const results = findSongs(songs, miniSearch, inverseIndex, cleanQuery, language, charCount, mode, sort);
          if (paginated) {
            const safeOffset = Math.max(0, Number.parseInt(offset, 10) || 0);
            const safeLimit = Math.max(1, Math.min(100, Number.parseInt(limit, 10) || 50));
            socket.emit('search_page_results', {
              items: results.slice(safeOffset, safeOffset + safeLimit),
              total: results.length,
              offset: safeOffset,
              limit: safeLimit,
              requestId,
            });
          } else {
            socket.emit('search_results', results.slice(0, 50));
          }
        });
        socket.on('add_to_queue', payloadSong => {
          const song = findSong(payloadSong);
          if (!song || !song.video_filename) return;
          queue.push({
            ...song,
            requestedBy: String(payloadSong?.requestedBy || '').trim().slice(0, 24),
            queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          });
          if (queue.length === 1) startCurrent();
          emitState();
        });
        socket.on('insert_to_top', payloadSong => {
          const song = findSong(payloadSong);
          if (!song || !song.video_filename) return;
          const item = {
            ...song,
            requestedBy: String(payloadSong?.requestedBy || '').trim().slice(0, 24),
            queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          };
          queue.splice(queue.length ? 1 : 0, 0, item);
          if (queue.length === 1) startCurrent();
          emitState();
        });
        socket.on('remove_from_queue', queueId => {
          const index = queue.findIndex(item => item.queueId === queueId);
          if (index < 0) return;
          const wasCurrent = index === 0;
          const [removed] = queue.splice(index, 1);
          if (wasCurrent) {
            history.unshift(removed);
            history.splice(100);
            startCurrent();
          }
          emitState();
        });
        socket.on('move_to_top', queueId => {
          const index = queue.findIndex(item => item.queueId === queueId);
          if (index <= 1) return;
          const [item] = queue.splice(index, 1);
          queue.splice(1, 0, item);
          emitState();
        });
        socket.on('shuffle_queue', () => {
          for (let index = queue.length - 1; index > 1; index--) {
            const target = 1 + Math.floor(Math.random() * index);
            [queue[index], queue[target]] = [queue[target], queue[index]];
          }
          emitState();
        });
        socket.on('clear_queue', () => {
          queue.splice(1);
          emitState();
        });
        socket.on('reorder_queue', ids => {
          if (!Array.isArray(ids)) return;
          const byId = new Map(queue.map(item => [item.queueId, item]));
          const reordered = ids.map(item => byId.get(typeof item === 'string' ? item : item?.queueId)).filter(Boolean);
          queue.splice(0, queue.length, ...reordered, ...queue.filter(item => !reordered.includes(item)));
          emitState();
        });
        socket.on('next_song', () => {
          advanceQueue();
        });
        socket.on('replay_current', () => {
          playerState.position = 0;
          playerState.startedAt = playerState.isPlaying ? Date.now() : null;
          emitState();
        });
        socket.on('seek', value => {
          const position = Number(value?.position ?? value);
          if (!Number.isFinite(position) || position < 0) return;
          const duration = Number(currentSong()?.duration_seconds);
          playerState.position = duration > 0 ? Math.min(position, duration) : position;
          playerState.startedAt = playerState.isPlaying ? Date.now() : null;
          emitState();
        });
        socket.on('player_command', command => {
          if (!command || typeof command !== 'object') return;
          if (command.isPlaying !== undefined) {
            const position = currentPosition();
            playerState.position = position;
            playerState.isPlaying = Boolean(command.isPlaying);
            playerState.startedAt = playerState.isPlaying ? Date.now() : null;
          }
          if (command.audioMode === 'original') playerState.audioMode = 'original';
          if (command.audioMode === 'accompaniment') {
            const song = currentSong();
            if (!canPlayInstrumental(song)) {
              io.emit('playback_error', { message: '伴奏檔缺失或無效，已保留原唱模式。' });
              command = { ...command, audioMode: undefined };
            } else playerState.audioMode = 'accompaniment';
          }
          for (const key of ['musicVol']) {
            if (command[key] !== undefined && Number.isFinite(Number(command[key]))) {
              playerState[key] = Math.max(0, Math.min(1, Number(command[key])));
            }
          }
          playerState.serverTime = Date.now();
          io.emit('execute_command', command);
          emitState();
        });
        socket.on('report_score', scorePayload => socket.broadcast.emit('score_updated', scorePayload));
        socket.on('crowd_interaction', payload => {
          if (!payload || !['applause', 'cheer', 'message'].includes(payload.type)) return;
          const text = String(payload.text || '').trim().slice(0, 100);
          if (payload.type === 'message' && !text) return;
          io.emit('crowd_interaction', {
            type: payload.type,
            text,
            user: String(payload.user || '來賓').trim().slice(0, 24),
            serverTime: Date.now(),
          });
        });
      });

      setInterval(async () => {
        try {
          const stat = fs.existsSync(catalogPath) ? fs.statSync(catalogPath) : null;
          if (stat && stat.mtimeMs !== catalogMtime) {
            songs = await readCatalog(catalogPath);
            miniSearch = createSearchIndex(songs);
            inverseIndex = createInverseIndex(songs);
            catalogMtime = stat.mtimeMs;
          }
        } catch (error) {
          console.error('[Database] Catalog reload failed:', error.message);
        }
        if (currentSong()) emitState();
      }, 1000).unref();

      let catalogMtime = fs.existsSync(catalogPath) ? fs.statSync(catalogPath).mtimeMs : 0;
      httpServer.once('error', reject);
      httpServer.listen(port, '0.0.0.0', () => {
        const httpsPort = Number(process.env.KTV_HTTPS_PORT) || 3443;
        startHttpsProxy({
          port: httpsPort,
          targetPort: port,
          certPath: process.env.KTV_TLS_CERT_PATH,
          keyPath: process.env.KTV_TLS_KEY_PATH,
        }).then(httpsProxy => {
          console.log(`[Database] Loaded ${songs.length} songs.`);
          console.log(`[Server] KTV service listening on port ${port}.`);
          if (httpsProxy) console.log(`[Server] HTTPS remote available on port ${httpsPort}.`);
          resolve({
            close: async () => {
              await httpsProxy?.close();
              await new Promise(resolveClose => io.close(resolveClose));
            },
          });
        }).catch(error => {
          io.close(() => reject(error));
        });
      });
    } catch (error) {
      reject(error);
    }
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  startServer().catch(error => {
    console.error('[Server] Startup failed:', error.message);
    process.exitCode = 1;
  });
}
