import test from 'node:test';
import assert from 'node:assert/strict';
import { createSocketSessionManager, resolveGrantedRole } from '../socket-session.js';

test('issues and verifies signed session for same client fingerprint', () => {
  const manager = createSocketSessionManager({ secret: 'test-secret', ttlSeconds: 300 });
  const token = manager.issue({ role: 'admin', clientIp: '127.0.0.1', userAgent: 'jest-agent', now: 1000 });
  const result = manager.verify(token, { clientIp: '127.0.0.1', userAgent: 'jest-agent', now: 2000 });
  assert.equal(result.ok, true);
  assert.equal(result.role, 'admin');
});

test('rejects token replay from different client fingerprint', () => {
  const manager = createSocketSessionManager({ secret: 'test-secret', ttlSeconds: 300 });
  const token = manager.issue({ role: 'remote', clientIp: '10.0.0.1', userAgent: 'ua-a', now: 1000 });
  const result = manager.verify(token, { clientIp: '10.0.0.2', userAgent: 'ua-a', now: 2000 });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'fingerprint_mismatch');
});

test('grants admin role when control token is valid', () => {
  const role = resolveGrantedRole({ requestedRole: 'viewer', isLocalClient: false, hasValidControlToken: true });
  assert.equal(role, 'admin');
});

test('downgrades non-local admin request without valid control token', () => {
  const role = resolveGrantedRole({ requestedRole: 'admin', isLocalClient: false, hasValidControlToken: false });
  assert.equal(role, 'viewer');
});
