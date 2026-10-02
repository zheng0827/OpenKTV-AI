import express from 'express';
import { createServer } from 'node:http';
import { Server } from 'socket.io';
import fs from 'node:fs';
import path from 'node:path';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import csv from 'csv-parser';
import MiniSearch from 'minisearch';
import QRCode from 'qrcode';
import YAML from 'yaml';

const appDir = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(appDir, '..');

function loadConfig() {
  const configPath = process.env.KTV_CONFIG_PATH || path.join(rootDir, 'config.yaml');
  return YAML.parse(fs.readFileSync(configPath, 'utf8'));
}

function safeEqual(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string') return false;
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

function idFor(row) {
  return String(row.id || row.spotify_track_id || row.video_filename || row.song || '');
}

function publicSong(song) {
  return {
    id: song.id,
    song_name: song.song_name || '',
    artist_name: song.artist_name || '',
    album: song.album || '',
    duration_seconds: Number(song.duration_seconds) || 0,
    video_filename: song.video_filename,
    accompaniment_filename: song.accompaniment_filename || '',
    lyrics_filename: song.lyrics_filename || '',
  };
}

function createRoom(id) {
  return {
    id,
    adminToken: randomBytes(32).toString('hex'),
    members: new Map(),
    queue: [],
    played: [],
    current: null,
    playing: false,
    position: 0,
    startedAt: null,
    audioMode: 'instrumental',
    effects: { volume: 1, micVolume: 1, reverb: 0.25 },
    emptyTimer: null,
  };
}

function positionOf(room) {
  return room.playing && room.startedAt
    ? room.position + Math.max(0, (Date.now() - room.startedAt) / 1000)
    : room.position;
}

function serialiseRoom(room) {
  return {
    roomId: room.id,
    queue: room.queue,
    played: room.played.slice(-20),
    current: room.current,
    isPlaying: room.playing,
    position: positionOf(room),
    serverTime: Date.now(),
    audioMode: room.audioMode,
    effects: room.effects,
    memberCount: room.members.size,
  };
}

function makeRoomId(rooms) {
  let id;
  do {
    id = randomBytes(6).toString('hex').toUpperCase();
  } while (rooms.has(id));
  return id;
}

export async function startServer(port) {
  const config = loadConfig();
  const mediaDir = path.resolve(rootDir, process.env.KTV_SONGS_DIR || config.paths.songs_directory);
  const csvPath = path.resolve(rootDir, process.env.KTV_LIBRARY_CSV || config.paths.library_csv);
  fs.mkdirSync(mediaDir, { recursive: true });
  const publicDir = path.join(appDir, 'public');
  const configuredOrigins = (process.env.KTV_CORS_ORIGINS || '').split(',').map((value) => value.trim()).filter(Boolean);
  const allowedOrigins = configuredOrigins.length ? configuredOrigins : (config.socket_io?.cors_origins || []);
  const emptyGraceMs = Math.max(0, Number(config.player?.rooms?.empty_room_grace_seconds ?? 30)) * 1000;
  const rooms = new Map();
  const roomCreationAttempts = new Map();
  let songs = [];
  let searchIndex = new MiniSearch({
    fields: ['song_name', 'artist_name', 'album', 'lyrics_plain_text'],
    storeFields: ['id', 'song_name', 'artist_name', 'album', 'duration_seconds', 'video_filename', 'accompaniment_filename', 'lyrics_filename'],
    searchOptions: { prefix: true, fuzzy: 0.15 },
  });

  async function reloadLibrary() {
    const loaded = [];
    if (fs.existsSync(csvPath)) {
      await new Promise((resolve, reject) => {
        fs.createReadStream(csvPath)
          .pipe(csv())
          .on('data', (row) => {
            const id = idFor(row);
            if (id && row.video_filename) loaded.push({ ...row, id });
          })
          .on('end', resolve)
          .on('error', reject);
      });
    }
    const index = new MiniSearch({
      fields: ['song_name', 'artist_name', 'album', 'lyrics_plain_text'],
      storeFields: ['id', 'song_name', 'artist_name', 'album', 'duration_seconds', 'video_filename', 'accompaniment_filename', 'lyrics_filename'],
      searchOptions: { prefix: true, fuzzy: 0.15 },
    });
    index.addAll(loaded);
    songs = loaded;
    searchIndex = index;
    return songs.length;
  }

  await reloadLibrary();
  const app = express();
  const httpServer = createServer(app);
  const io = new Server(httpServer, {
    cors: { origin: allowedOrigins, methods: ['GET', 'POST'] },
    maxHttpBufferSize: 1e6,
    connectionStateRecovery: { maxDisconnectionDuration: 120_000 },
  });

  app.disable('x-powered-by');
  app.use(express.json({ limit: '32kb' }));
  app.use(express.static(publicDir, { index: false, dotfiles: 'deny' }));
  app.get('/health', (_req, res) => {
    const stats = fs.statfsSync(mediaDir);
    res.json({
      status: 'ok',
      librarySongs: songs.length,
      rooms: rooms.size,
      sockets: [...rooms.values()].reduce((total, room) => total + room.members.size, 0),
      uptimeSeconds: Math.round(process.uptime()),
      memoryBytes: process.memoryUsage().rss,
      cpuMicros: process.cpuUsage(),
      loadAverage: os.loadavg(),
      diskFreeBytes: stats.bavail * stats.bsize,
    });
  });
  app.get('/api/songs', (_req, res) => res.json(songs));
  app.get('/api/search', (req, res) => {
    const query = typeof req.query.q === 'string' ? req.query.q.trim().slice(0, 100) : '';
    const limit = Math.max(1, Math.min(50, Number.parseInt(req.query.limit, 10) || 30));
    const results = query ? searchIndex.search(query).slice(0, limit) : songs.slice(0, limit);
    res.json(results);
  });

  app.post('/api/library/reload', async (req, res) => {
    const expected = process.env.KTV_LIBRARY_RELOAD_TOKEN;
    if (!expected || !safeEqual(req.get('authorization')?.replace(/^Bearer\s+/i, ''), expected)) {
      return res.status(401).json({ error: 'unauthorized' });
    }
    try {
      res.json({ count: await reloadLibrary() });
      io.emit('library_updated');
    } catch (error) {
      res.status(500).json({ error: 'library_reload_failed' });
    }
  });

  app.post('/api/rooms', (req, res) => {
    const now = Date.now();
    const attempts = (roomCreationAttempts.get(req.ip) || []).filter((time) => now - time < 60_000);
    if (attempts.length >= 5) return res.status(429).json({ error: 'room_creation_rate_limited' });
    attempts.push(now);
    roomCreationAttempts.set(req.ip, attempts);
    if (rooms.size >= 100) return res.status(503).json({ error: 'room_capacity_reached' });
    const roomId = makeRoomId(rooms);
    const room = createRoom(roomId);
    rooms.set(roomId, room);
    room.emptyTimer = setTimeout(() => {
      if (room.members.size === 0) rooms.delete(room.id);
    }, emptyGraceMs);
    room.emptyTimer.unref?.();
    res.status(201).json({ roomId, adminToken: room.adminToken, playerUrl: `/player?room=${roomId}`, remoteUrl: `/remote?room=${roomId}` });
  });

  app.post('/api/rooms/:roomId/jobs', async (req, res) => {
    const room = rooms.get(String(req.params.roomId).toUpperCase());
    const supplied = req.get('authorization')?.replace(/^Bearer\s+/i, '');
    if (!room || !safeEqual(supplied, room.adminToken)) return res.status(403).json({ error: 'forbidden' });
    const url = typeof req.body?.url === 'string' ? req.body.url.trim() : '';
    if (url.length > 2048 || !/^https:\/\/(www\.)?(youtube\.com|youtu\.be)\//i.test(url)) {
      return res.status(400).json({ error: 'invalid_youtube_url' });
    }
    const processingUrl = process.env.KTV_PROCESSING_API_URL || 'http://127.0.0.1:5000/api/jobs';
    const processingToken = process.env.KTV_PROCESSING_API_TOKEN;
    if (!processingToken || processingToken.length < 32 || processingToken.startsWith('replace-')) {
      return res.status(503).json({ error: 'processing_service_not_configured' });
    }
    try {
      const response = await fetch(processingUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + processingToken },
        body: JSON.stringify({
          url,
          title: typeof req.body?.title === 'string' ? req.body.title.slice(0, 300) : '',
          singer: typeof req.body?.singer === 'string' ? req.body.singer.slice(0, 200) : '',
        }),
        signal: AbortSignal.timeout(10_000),
      });
      const result = await response.json();
      res.status(response.status).json(result);
    } catch (_error) {
      res.status(502).json({ error: 'processing_service_unavailable' });
    }
  });

  app.get('/api/rooms/:roomId/jobs/:jobId', async (req, res) => {
    const room = rooms.get(String(req.params.roomId).toUpperCase());
    const supplied = req.get('authorization')?.replace(/^Bearer\s+/i, '');
    if (!room || !safeEqual(supplied, room.adminToken)) return res.status(403).json({ error: 'forbidden' });
    const processingUrl = process.env.KTV_PROCESSING_API_URL || 'http://127.0.0.1:5000/api/jobs';
    const processingToken = process.env.KTV_PROCESSING_API_TOKEN;
    if (!processingToken || processingToken.length < 32 || processingToken.startsWith('replace-')
      || !/^[a-f0-9]{32}$/.test(req.params.jobId)) return res.status(400).json({ error: 'invalid_job_request' });
    try {
      const response = await fetch(`${processingUrl}/${req.params.jobId}`, {
        headers: { Authorization: 'Bearer ' + processingToken },
        signal: AbortSignal.timeout(10_000),
      });
      res.status(response.status).json(await response.json());
    } catch (_error) {
      res.status(502).json({ error: 'processing_service_unavailable' });
    }
  });

  app.get('/api/rooms/:roomId/qr', async (req, res) => {
    const room = rooms.get(String(req.params.roomId).toUpperCase());
    if (!room) return res.status(404).json({ error: 'room_not_found' });
    const configuredBase = process.env.PUBLIC_BASE_URL;
    const base = configuredBase || `${req.protocol}://${req.get('host')}`;
    try {
      const dataUrl = await QRCode.toDataURL(`${base}/remote?room=${encodeURIComponent(room.id)}`, { margin: 1, width: 280 });
      res.json({ roomId: room.id, joinUrl: `${base}/remote?room=${room.id}`, dataUrl });
    } catch (_error) {
      res.status(500).json({ error: 'qr_generation_failed' });
    }
  });

  const allowedMedia = () => new Set(songs.flatMap((song) => [
    song.video_filename,
    song.accompaniment_filename,
    song.instrumental_filename,
    song.lyrics_filename,
    song.lyrics_lrc,
  ]).filter(Boolean));
  app.get('/media/:filename', (req, res) => {
    const filename = req.params.filename;
    if (path.basename(filename) !== filename || !allowedMedia().has(filename)) {
      return res.status(404).end();
    }
    let filePath;
    try {
      const realMediaDir = fs.realpathSync(mediaDir);
      filePath = fs.realpathSync(path.resolve(mediaDir, filename));
      if (!filePath.startsWith(`${realMediaDir}${path.sep}`)) return res.status(404).end();
    } catch (_error) {
      return res.status(404).end();
    }
    res.setHeader('Cache-Control', 'private, max-age=60');
    res.setHeader('Accept-Ranges', 'bytes');
    res.sendFile(filePath, { acceptRanges: true }, (error) => {
      if (error && !res.headersSent) res.status(error.statusCode || 500).end();
    });
  });

  app.get('/', (_req, res) => res.sendFile(path.join(publicDir, 'index.html')));
  app.get('/player', (_req, res) => res.sendFile(path.join(publicDir, 'player.html')));
  app.get('/remote', (_req, res) => res.sendFile(path.join(publicDir, 'remote.html')));

  io.use((socket, next) => {
    const roomId = String(socket.handshake.auth?.roomId || '').toUpperCase();
    const role = socket.handshake.auth?.role;
    const room = rooms.get(roomId);
    if (!room || !['player', 'remote', 'admin'].includes(role)) return next(new Error('room_or_role_invalid'));
    if (['admin', 'player'].includes(role) && !safeEqual(socket.handshake.auth?.token, room.adminToken)) {
      return next(new Error('admin_auth_failed'));
    }
    socket.data.roomId = roomId;
    socket.data.role = role;
    next();
  });

  function publish(room) {
    io.to(`room:${room.id}`).emit('room_state', serialiseRoom(room));
  }

  function startNext(room) {
    const next = room.queue.shift() || null;
    if (room.current) room.played.push(room.current);
    room.current = next;
    room.position = 0;
    room.playing = Boolean(next);
    room.startedAt = next ? Date.now() : null;
  }

  io.on('connection', (socket) => {
    const room = rooms.get(socket.data.roomId);
    if (!room) return socket.disconnect(true);
    clearTimeout(room.emptyTimer);
    room.members.set(socket.id, socket.data.role);
    socket.join(`room:${room.id}`);
    socket.emit('room_state', serialiseRoom(room));

    let recentEvents = [];
    const remoteActions = new Set(config.socket_io?.remote_permissions?.playback_actions || []);
    const canControl = (action) => socket.data.role === 'admin'
      || (socket.data.role === 'remote' && remoteActions.has(action));
    const rateAllowed = () => {
      const now = Date.now();
      recentEvents = recentEvents.filter((time) => now - time < 10_000);
      if (recentEvents.length >= 40) return false;
      recentEvents.push(now);
      return true;
    };
    const emitError = (code) => socket.emit('operation_error', { code });
    const knownEvents = new Set([
      'search_songs', 'add_to_queue', 'queue_insert_next', 'queue_remove',
      'reorder_queue', 'control', 'seek', 'change_track', 'set_effects', 'song_ended',
    ]);
    socket.onAny((event) => {
      if (!knownEvents.has(event)) emitError('unknown_event');
    });
    const onEvent = (name, handler) => socket.on(name, (payload) => {
      if (!rateAllowed()) return emitError('rate_limited');
      try {
        handler(payload);
      } catch (_error) {
        emitError('invalid_payload');
      }
    });

    onEvent('search_songs', (payload) => {
      if (!payload || typeof payload !== 'object') return emitError('invalid_payload');
      const query = typeof payload.query === 'string' ? payload.query.trim().slice(0, 100) : '';
      const results = query ? searchIndex.search(query).slice(0, 40) : songs.slice(0, 40);
      socket.emit('search_results', results);
    });

    onEvent('add_to_queue', (payload) => {
      if (!['remote', 'admin', 'player'].includes(socket.data.role)) return emitError('forbidden');
      const songId = String(payload?.songId || '').slice(0, 160);
      const song = songs.find((entry) => entry.id === songId);
      if (!song) return emitError('song_not_found');
      room.queue.push({ queueId: randomBytes(10).toString('hex'), ...publicSong(song) });
      if (!room.current) startNext(room);
      publish(room);
    });

    onEvent('queue_insert_next', (payload) => {
      if (socket.data.role !== 'admin' && !(socket.data.role === 'remote' && config.socket_io?.remote_permissions?.insert_next)) return emitError('forbidden');
      const songId = String(payload?.songId || '').slice(0, 160);
      const song = songs.find((entry) => entry.id === songId);
      if (!song) return emitError('song_not_found');
      room.queue.splice(room.current ? 0 : room.queue.length, 0, { queueId: randomBytes(10).toString('hex'), ...publicSong(song) });
      if (!room.current) startNext(room);
      publish(room);
    });

    onEvent('queue_remove', (payload) => {
      if (socket.data.role !== 'admin' && !(socket.data.role === 'remote' && config.socket_io?.remote_permissions?.remove_queue)) return emitError('forbidden');
      const queueId = String(payload?.queueId || '');
      room.queue = room.queue.filter((entry) => entry.queueId !== queueId);
      publish(room);
    });

    onEvent('reorder_queue', (payload) => {
      if ((socket.data.role !== 'admin' && !(socket.data.role === 'remote' && config.socket_io?.remote_permissions?.reorder_queue))
        || !Array.isArray(payload?.queueIds) || payload.queueIds.length !== room.queue.length) {
        return emitError('forbidden_or_invalid_queue');
      }
      const byId = new Map(room.queue.map((entry) => [entry.queueId, entry]));
      if (payload.queueIds.some((id) => typeof id !== 'string' || !byId.has(id)) || new Set(payload.queueIds).size !== room.queue.length) {
        return emitError('invalid_queue_order');
      }
      room.queue = payload.queueIds.map((id) => byId.get(id));
      publish(room);
    });

    onEvent('control', (payload) => {
      if (!canControl(payload?.action) || !room.current || !['play', 'pause', 'next', 'replay', 'stop'].includes(payload?.action)) {
        return emitError('forbidden_or_invalid_action');
      }
      if (payload.action === 'next') startNext(room);
      else if (payload.action === 'replay') {
        room.position = 0;
        room.playing = true;
        room.startedAt = Date.now();
      } else if (payload.action === 'stop') {
        room.position = 0;
        room.playing = false;
        room.startedAt = null;
      } else {
        if (payload.action === 'pause' && room.playing) room.position = positionOf(room);
        room.playing = payload.action === 'play';
        room.startedAt = room.playing ? Date.now() : null;
      }
      publish(room);
    });

    onEvent('seek', (payload) => {
      if (!canControl('seek') || !room.current || !Number.isFinite(payload?.position)) return emitError('forbidden_or_invalid_position');
      const duration = Math.max(0, Number(room.current.duration_seconds) || 24 * 60 * 60);
      room.position = Math.max(0, Math.min(duration, payload.position));
      room.startedAt = room.playing ? Date.now() : null;
      publish(room);
    });

    onEvent('change_track', (payload) => {
      if (!canControl('change_track') || !['original', 'instrumental'].includes(payload?.mode)) return emitError('forbidden_or_invalid_mode');
      if (payload.mode === 'original') return emitError('original_audio_disabled');
      room.audioMode = 'instrumental';
      publish(room);
    });

    onEvent('set_effects', (payload) => {
      if (!canControl('set_effects')) return emitError('forbidden');
      if (!payload || typeof payload !== 'object') return emitError('invalid_payload');
      for (const [key, min, max] of [['volume', 0, 1], ['micVolume', 0, 1], ['reverb', 0, 0.8]]) {
        if (payload[key] !== undefined) {
          if (!Number.isFinite(payload[key]) || payload[key] < min || payload[key] > max) return emitError('invalid_effect_value');
          room.effects[key] = payload[key];
        }
      }
      publish(room);
    });

    onEvent('song_ended', (payload) => {
      if (!['admin', 'player'].includes(socket.data.role)) return emitError('forbidden');
      if (!room.current || payload?.songId !== room.current.id) return;
      startNext(room);
      publish(room);
    });

    socket.on('disconnect', () => {
      room.members.delete(socket.id);
      publish(room);
      if (room.members.size === 0 && config.player?.rooms?.close_when_empty !== false) {
        room.emptyTimer = setTimeout(() => {
          if (room.members.size === 0) rooms.delete(room.id);
        }, emptyGraceMs);
        room.emptyTimer.unref?.();
      }
    });
  });

  const syncInterval = setInterval(() => {
    for (const room of rooms.values()) if (room.members.size) publish(room);
  }, Math.max(250, Number(config.player?.playback?.snapshot_interval_seconds || 1) * 1000));
  syncInterval.unref?.();
  let observedCsvMtime = fs.existsSync(csvPath) ? fs.statSync(csvPath).mtimeMs : 0;
  const libraryWatcher = setInterval(async () => {
    try {
      const nextMtime = fs.existsSync(csvPath) ? fs.statSync(csvPath).mtimeMs : 0;
      if (nextMtime !== observedCsvMtime) {
        observedCsvMtime = nextMtime;
        const count = await reloadLibrary();
        io.emit('library_updated', { count });
      }
    } catch (error) {
      console.error('[node-playback] library reload failed:', error.message);
    }
  }, 3000);
  libraryWatcher.unref?.();

  const listenPort = port ?? Number(process.env.KTV_NODE_PORT || config.services.node.port);
  return new Promise((resolve, reject) => {
    httpServer.once('error', reject);
    httpServer.listen(listenPort, process.env.KTV_NODE_HOST || config.services.node.host, () => {
      console.log(`[node-playback] listening on ${listenPort}; ${songs.length} library entries`);
      resolve({ app, io, httpServer, rooms, reloadLibrary });
    });
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  startServer().catch((error) => {
    console.error('[node-playback] startup failed:', error);
    process.exitCode = 1;
  });
}
