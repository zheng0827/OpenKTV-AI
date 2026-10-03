import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';
import { startServer } from '../server.js';

let server;
let baseUrl;
let mediaDir;
let oldSongsDir;
let oldLibraryPath;

before(async () => {
  mediaDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openktv-media-'));
  oldSongsDir = process.env.KTV_SONGS_DIR;
  oldLibraryPath = process.env.KTV_LIBRARY_CSV;
  process.env.KTV_SONGS_DIR = mediaDir;
  process.env.KTV_LIBRARY_CSV = path.join(mediaDir, 'library.csv');
  fs.writeFileSync(path.join(mediaDir, 'sample.mp4'), '0123456789');
  fs.writeFileSync(path.join(mediaDir, 'sample.instrumental.m4a'), 'audio');
  fs.writeFileSync(path.join(mediaDir, 'library.csv'),
    'id,artist_name,song_name,album,duration_seconds,video_filename,accompaniment_filename,lyrics_filename\n'
    + 'test-id,Artist,Song,Album,120,sample.mp4,sample.instrumental.m4a,\n');
  server = await startServer(0);
  baseUrl = `http://127.0.0.1:${server.httpServer.address().port}`;
});

after(async () => {
  if (server) await new Promise((resolve) => server.io.close(resolve));
  if (oldSongsDir === undefined) delete process.env.KTV_SONGS_DIR;
  else process.env.KTV_SONGS_DIR = oldSongsDir;
  if (oldLibraryPath === undefined) delete process.env.KTV_LIBRARY_CSV;
  else process.env.KTV_LIBRARY_CSV = oldLibraryPath;
  fs.rmSync(mediaDir, { recursive: true, force: true });
});

test('health, room QR, and library media range requests work', async () => {
  const health = await fetch(`${baseUrl}/health`).then((response) => response.json());
  assert.equal(health.status, 'ok');
  assert.equal(health.librarySongs, 1);

  const roomResponse = await fetch(`${baseUrl}/api/rooms`, { method: 'POST' });
  assert.equal(roomResponse.status, 201);
  const room = await roomResponse.json();
  assert.match(room.adminToken, /^[a-f0-9]{64}$/);

  const qr = await fetch(`${baseUrl}/api/rooms/${room.roomId}/qr`).then((response) => response.json());
  assert.match(qr.dataUrl, /^data:image\/png;base64,/);
  assert.match(qr.joinUrl, new RegExp(room.roomId));

  const media = await fetch(`${baseUrl}/media/sample.mp4`, { headers: { Range: 'bytes=0-3' } });
  assert.equal(media.status, 206);
  assert.equal(await media.text(), '0123');
  assert.equal((await fetch(`${baseUrl}/media/unknown.mp4`)).status, 404);
});

test('standalone download page is routed separately from the player', async () => {
  const downloadPage = await fetch(`${baseUrl}/download`).then((response) => response.text());
  const playerPage = await fetch(`${baseUrl}/player`).then((response) => response.text());
  assert.match(downloadPage, /新增一首好歌/);
  assert.match(downloadPage, /localStorage/);
  assert.doesNotMatch(playerPage, /youtubeUrl|separatorBackend|alignmentBackend/);
});

test('authorized processing proxy forwards separation and alignment options', async () => {
  const room = await fetch(`${baseUrl}/api/rooms`, { method: 'POST' }).then((response) => response.json());
  const previousToken = process.env.KTV_PROCESSING_API_TOKEN;
  const previousUrl = process.env.KTV_PROCESSING_API_URL;
  const originalFetch = globalThis.fetch;
  process.env.KTV_PROCESSING_API_TOKEN = 'test-service-token-long-enough-to-pass-validation';
  process.env.KTV_PROCESSING_API_URL = 'http://processing.test/api/jobs';
  let forwardedPayload;
  globalThis.fetch = async (_url, options) => {
    forwardedPayload = JSON.parse(options.body);
    return { status: 202, json: async () => ({ job_id: 'a'.repeat(32), status: 'queued' }) };
  };
  try {
    const response = await originalFetch(`${baseUrl}/api/rooms/${room.roomId}/jobs`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + room.adminToken,
      },
      body: JSON.stringify({
        url: 'https://youtu.be/abc123',
        title: 'Manual song',
        singer: 'Manual artist',
        options: { separator_backend: 'hybrid', alignment_backend: 'whisperx' },
      }),
    });
    assert.equal(response.status, 202);
    assert.deepEqual(forwardedPayload.options, {
      separator_backend: 'hybrid',
      alignment_backend: 'whisperx',
    });
    assert.equal(forwardedPayload.title, 'Manual song');
    assert.equal(forwardedPayload.singer, 'Manual artist');
  } finally {
    globalThis.fetch = originalFetch;
    if (previousToken === undefined) delete process.env.KTV_PROCESSING_API_TOKEN;
    else process.env.KTV_PROCESSING_API_TOKEN = previousToken;
    if (previousUrl === undefined) delete process.env.KTV_PROCESSING_API_URL;
    else process.env.KTV_PROCESSING_API_URL = previousUrl;
  }
});

test('health endpoint applies a per-client request limit', async () => {
  let rateLimited = false;
  for (let index = 0; index < 65 && !rateLimited; index += 1) {
    const response = await fetch(`${baseUrl}/health`);
    rateLimited = response.status === 429;
  }
  assert.equal(rateLimited, true);
});
