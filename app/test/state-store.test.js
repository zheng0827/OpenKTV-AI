import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { loadPlaybackState, persistPlaybackState } from '../state-store.js';

function withTempFile(fn) {
  const filePath = path.join(os.tmpdir(), `ktv-state-${Date.now()}-${Math.random().toString(36).slice(2)}.json`);
  try {
    fn(filePath);
  } finally {
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
  }
}

test('persists and restores playback state with song lookup', () => {
  withTempFile(filePath => {
    persistPlaybackState(filePath, {
      queue: [{ id: '1', requestedBy: 'A', queueId: 'q1' }],
      history: [{ id: '2', requestedBy: 'B', queueId: 'h1' }],
      playerState: { isPlaying: true, audioMode: 'original', musicVol: 0.7, position: 12.3 },
    });
    const songs = [
      { id: '1', title: 'Song 1', artist: 'Singer', video_filename: 's1.mp4' },
      { id: '2', title: 'Song 2', artist: 'Singer', video_filename: 's2.mp4' },
    ];
    const restored = loadPlaybackState(filePath, songs);
    assert.equal(restored.queue.length, 1);
    assert.equal(restored.history.length, 1);
    assert.equal(restored.queue[0].title, 'Song 1');
    assert.equal(restored.playerState.isPlaying, true);
  });
});

test('drops queue items that no longer exist in catalog', () => {
  withTempFile(filePath => {
    persistPlaybackState(filePath, {
      queue: [{ id: 'missing', requestedBy: 'A', queueId: 'q1' }],
      history: [],
      playerState: { isPlaying: true, audioMode: 'original', musicVol: 0.7, position: 12.3 },
    });
    const restored = loadPlaybackState(filePath, []);
    assert.equal(restored.queue.length, 0);
    assert.equal(restored.playerState.isPlaying, false);
  });
});
