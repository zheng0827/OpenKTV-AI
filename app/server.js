import express from 'express';
import { createServer } from 'http';
import { Server } from 'socket.io';
import { rateLimit } from 'express-rate-limit';
import fs from 'fs';
import csv from 'csv-parser';
import MiniSearch from 'minisearch';
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
    fields: ['title', 'artist', 'language', 'pinyin_abbr', 'zhuyin_abbr'],
    storeFields: ['id', 'title', 'artist', 'language', 'char_count', 'path', 'instrumental_path',
      'video_filename', 'instrumental_filename', 'lyrics_filename'],
    searchOptions: { prefix: true, fuzzy: 0.2 },
  });
  index.addAll(songs);
  return index;
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
      const app = express();
      const httpServer = createServer(app);
      const io = new Server(httpServer, { cors: { origin: "*", methods: ["GET", "POST"] } });
      const mediaRequestLimiter = rateLimit({
        windowMs: 60 * 1000,
        limit: 600,
        standardHeaders: 'draft-8',
        legacyHeaders: false,
      });

      app.use(express.static(path.join(__dirname, 'public')));
      app.get('/player', (_req, res) => res.redirect('/player.html'));
      app.get(['/remote', '/queue'], (_req, res) => res.redirect('/remote.html'));
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
      const playerState = {
        isPlaying: false,
        audioMode: 'original',
        musicVol: 0.8,
        micVol: 1,
        reverbVol: 0.35,
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
        currentSong: currentSong(),
        state: {
          isPlaying: playerState.isPlaying,
          audioMode: playerState.audioMode,
          musicVol: playerState.musicVol,
          micVol: playerState.micVol,
          reverbVol: playerState.reverbVol,
          position: currentPosition(),
          serverTime: Date.now(),
        },
      });
      const emitState = () => {
        const payload = snapshot();
        io.emit('sync_state', { queue: payload.queue, state: payload.state });
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

      app.get('/health', (_req, res) => res.json({ ok: true, songs: songs.length }));

      io.on('connection', socket => {
        const payload = snapshot();
        socket.emit('sync_state', { queue: payload.queue, state: payload.state });
        socket.emit('playback_snapshot', payload);
        socket.on('time_ping', clientSentAt => {
          socket.emit('time_pong', { clientSentAt: Number(clientSentAt), serverTime: Date.now() });
        });
        socket.on('search_songs', ({ query = '', language = '', charCount = 0 } = {}) => {
          const cleanQuery = String(query).trim();
          let results = cleanQuery
            ? miniSearch.search(cleanQuery.toUpperCase()).map(result => songs.find(song => song.id === result.id)).filter(Boolean)
            : songs;
          if (language) results = results.filter(song => song.language === language);
          if (charCount) results = results.filter(song => song.char_count === Number(charCount));
          socket.emit('search_results', results.slice(0, 50));
        });
        socket.on('add_to_queue', payloadSong => {
          const song = findSong(payloadSong);
          if (!song || !song.video_filename) return;
          queue.push({ ...song, queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}` });
          if (queue.length === 1) startCurrent();
          emitState();
        });
        socket.on('insert_to_top', payloadSong => {
          const song = findSong(payloadSong);
          if (!song || !song.video_filename) return;
          const item = { ...song, queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}` };
          queue.splice(queue.length ? 1 : 0, 0, item);
          if (queue.length === 1) startCurrent();
          emitState();
        });
        socket.on('remove_from_queue', queueId => {
          const index = queue.findIndex(item => item.queueId === queueId);
          if (index < 0) return;
          const wasCurrent = index === 0;
          queue.splice(index, 1);
          if (wasCurrent) startCurrent();
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
          if (queue.length) queue.shift();
          startCurrent();
          emitState();
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
          for (const key of ['musicVol', 'micVol', 'reverbVol']) {
            if (command[key] !== undefined && Number.isFinite(Number(command[key]))) {
              const max = key === 'musicVol' ? 1.5 : key === 'micVol' ? 2.5 : 1.2;
              playerState[key] = Math.max(0, Math.min(max, Number(command[key])));
            }
          }
          playerState.serverTime = Date.now();
          io.emit('execute_command', command);
          emitState();
        });
        socket.on('report_score', scorePayload => socket.broadcast.emit('score_updated', scorePayload));
      });

      setInterval(async () => {
        try {
          const stat = fs.existsSync(catalogPath) ? fs.statSync(catalogPath) : null;
          if (stat && stat.mtimeMs !== catalogMtime) {
            songs = await readCatalog(catalogPath);
            miniSearch = createSearchIndex(songs);
            catalogMtime = stat.mtimeMs;
          }
        } catch (error) {
          console.error('[Database] 重新載入正式曲庫失敗：', error.message);
        }
        if (currentSong()) emitState();
      }, 1000).unref();

      let catalogMtime = fs.existsSync(catalogPath) ? fs.statSync(catalogPath).mtimeMs : 0;
      httpServer.once('error', reject);
      httpServer.listen(port, '0.0.0.0', () => {
        console.log(`[Database] 已載入 ${songs.length} 首歌曲；正式曲庫：${catalogPath}`);
        console.log(`[Server] KTV 核心服務器啟動成功：http://localhost:${port}`);
        resolve();
      });
    } catch (error) {
      reject(error);
    }
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  startServer().catch(error => {
    console.error('[Server] 啟動失敗：', error);
    process.exitCode = 1;
  });
}
