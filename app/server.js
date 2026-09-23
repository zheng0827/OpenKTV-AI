import express from 'express';
import { createServer } from 'http';
import { Server } from 'socket.io';
import fs from 'fs';
import csv from 'csv-parser';
import MiniSearch from 'minisearch';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export function startServer(port = 3000) {
  return new Promise((resolve) => {
    const app = express();
    const httpServer = createServer(app);
    const io = new Server(httpServer, {
      cors: { origin: "*", methods: ["GET", "POST"] }
    });

    app.use(express.static(path.join(__dirname, 'public')));

    // 建立高效能記憶體搜尋索引引擎
    const miniSearch = new MiniSearch({
      fields: ['title', 'artist', 'language', 'pinyin_abbr', 'zhuyin_abbr'],
      storeFields: ['id', 'title', 'artist', 'language', 'char_count', 'path'],
      searchOptions: { prefix: true, fuzzy: 0.2 }
    });

    const songs = [];
    const csvPath = path.join(__dirname, 'songs.csv');

    if (fs.existsSync(csvPath)) {
      fs.createReadStream(csvPath)
        .pipe(csv())
        .on('data', (row) => {
          songs.push({
            ...row,
            id: String(row.id),
            char_count: Number(row.char_count) || row.title?.length || 0,
            pinyin_abbr: (row.pinyin_abbr || '').toUpperCase(),
            zhuyin_abbr: row.zhuyin_abbr || ''
          });
        })
        .on('end', () => {
          miniSearch.addAll(songs);
          console.log(`[Database] 成功載入並完成 ${songs.length} 首歌的記憶體搜尋索引。`);
        });
    } else {
      console.warn(`[Database] 找不到 songs.csv，請確認資料庫檔案是否存在。`);
    }

    // 共享佇列與全域控制狀態 (含音樂音量、麥克風增益與混響)
    let playQueue = [];
    let playerState = {
      isPlaying: true,
      audioMode: 'original', // 'original' (原唱) 或 'accompaniment' (伴奏)
      musicVol: 0.8,
      micVol: 1.2,
      reverbVol: 0.45,
      currentScore: 85,
      currentPitch: 0
    };

    io.on('connection', (socket) => {
      // 連線初次全同步
      socket.emit('sync_state', { queue: playQueue, state: playerState });

      // 歌曲即時檢索 (支援文字、拼音首字母、注音、語系過濾)
      socket.on('search_songs', ({ query = '', language = '', charCount = 0 }) => {
        let results = [];
        const cleanQuery = query.trim();

        if (!cleanQuery) {
          results = songs.filter(s => {
            if (language && s.language !== language) return false;
            if (charCount && s.char_count !== charCount) return false;
            return true;
          }).slice(0, 50);
        } else {
          const searchRes = miniSearch.search(cleanQuery.toUpperCase());
          results = searchRes.map(r => songs.find(s => s.id === r.id)).filter(Boolean);
          if (language) results = results.filter(s => s.language === language);
          if (charCount) results = results.filter(s => s.char_count === charCount);
        }

        socket.emit('search_results', results);
      });

      // 點歌：追加至隊列尾端
      socket.on('add_to_queue', (song) => {
        const item = { ...song, queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).substring(2, 6)}` };
        playQueue.push(item);
        io.emit('queue_updated', playQueue);
      });

      // 插播：置於當前播放歌曲的下一首（Index 1）
      socket.on('insert_to_top', (song) => {
        const item = { ...song, queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).substring(2, 6)}` };
        if (playQueue.length > 0) {
          playQueue.splice(1, 0, item);
        } else {
          playQueue.push(item);
        }
        io.emit('queue_updated', playQueue);
      });

      // 移除佇列中的特定歌曲
      socket.on('remove_from_queue', (queueId) => {
        playQueue = playQueue.filter(s => s.queueId !== queueId);
        io.emit('queue_updated', playQueue);
      });

      // 重新排序佇列
      socket.on('reorder_queue', (newQueue) => {
        playQueue = newQueue;
        io.emit('queue_updated', playQueue);
      });

      // 切歌
      socket.on('next_song', () => {
        if (playQueue.length > 0) {
          playQueue.shift();
          io.emit('queue_updated', playQueue);
        }
      });

      // 重唱當前歌曲
      socket.on('replay_current', () => {
        io.emit('command_replay');
      });

      // 播放器即時控制指令 (暫停/播放/音軌/音量/混音)
      socket.on('player_command', (cmd) => {
        Object.assign(playerState, cmd);
        io.emit('execute_command', cmd);
      });

      // 主機端即時廣播音準與評分給遙控端
      socket.on('report_score', (scorePayload) => {
        playerState.currentScore = scorePayload.score;
        playerState.currentPitch = scorePayload.pitch;
        socket.broadcast.emit('score_updated', scorePayload);
      });
    });

    httpServer.listen(port, '0.0.0.0', () => {
      console.log(`[Server] KTV 核心服務器啟動成功：http://localhost:${port}`);
      resolve();
    });
  });
}

// 支援單獨以 node server.js 運行
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  startServer(3000);
}