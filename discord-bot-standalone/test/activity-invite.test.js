import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyActivityInviteError } from '../lib/activity-invite.js';

test('classifies permission errors', () => {
  assert.equal(classifyActivityInviteError(403, 50013), '機器人缺少「建立邀請」權限，請在該語音頻道補齊權限後重試。');
});

test('classifies channel missing', () => {
  assert.equal(classifyActivityInviteError(404, 10003), '找不到語音頻道，請確認你仍在同一個伺服器語音房。');
});

test('classifies fallback errors', () => {
  assert.equal(classifyActivityInviteError(500, 0, 'boom'), '建立 Activity 邀請失敗（500）：boom');
});
