import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { MpvPlayer, resolveMpvExecutable } from '../mpv-player.js';

const executableName = process.platform === 'win32' ? 'mpv.exe' : 'mpv';

test('resolves an MPV executable from PATH', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'openktv-mpv-path-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const executable = path.join(directory, executableName);
  fs.writeFileSync(executable, '');

  assert.equal(resolveMpvExecutable({ env: { PATH: directory } }), executable);
});

test('finds an MPV executable in the packaged resources directory', t => {
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'openktv-mpv-resources-'));
  t.after(() => fs.rmSync(resourcesPath, { recursive: true, force: true }));
  const bundledPath = path.join(resourcesPath, 'mpv');
  fs.mkdirSync(bundledPath);
  const executable = path.join(bundledPath, executableName);
  fs.writeFileSync(executable, '');

  assert.equal(resolveMpvExecutable({ env: { PATH: '' }, resourcesPath }), executable);
});

test('reports actionable guidance when a configured MPV path is missing', () => {
  assert.throws(
    () => resolveMpvExecutable({ env: { PATH: '', KTV_MPV_EXECUTABLE: '/missing/path/mpv.exe' } }),
    /MPV was not found.*KTV_MPV_EXECUTABLE/,
  );
});

test('MPV startup failure remains a handled status instead of a child-process error', async () => {
  const previousExecutable = process.env.KTV_MPV_EXECUTABLE;
  process.env.KTV_MPV_EXECUTABLE = path.join(os.tmpdir(), `missing-mpv-${process.pid}`);
  const player = new MpvPlayer();
  try {
    await assert.rejects(player.open(null, {}, () => null), /MPV was not found/);
    assert.equal(player.getStatus().status, 'unavailable');
    assert.match(player.getStatus().error, /Install MPV/);
  } finally {
    if (previousExecutable === undefined) delete process.env.KTV_MPV_EXECUTABLE;
    else process.env.KTV_MPV_EXECUTABLE = previousExecutable;
  }
});
