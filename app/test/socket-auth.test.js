import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateSocketPermission } from '../socket-auth.js';

test('denies role outside allowlist', () => {
  const result = evaluateSocketPermission({
    role: 'remote',
    allowedRoles: ['admin', 'player'],
    requireToken: false,
    hasValidToken: false,
  });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'forbidden_role');
});

test('denies non-player control without token when required', () => {
  const result = evaluateSocketPermission({
    role: 'admin',
    allowedRoles: ['admin'],
    requireToken: true,
    hasValidToken: false,
  });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'invalid_control_token');
});

test('allows player control without token compatibility path', () => {
  const result = evaluateSocketPermission({
    role: 'player',
    allowedRoles: ['admin', 'player'],
    requireToken: true,
    hasValidToken: false,
  });
  assert.equal(result.ok, true);
});
