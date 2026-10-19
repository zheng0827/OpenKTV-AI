import assert from 'node:assert/strict';
import test from 'node:test';
import { allowsPageAccess } from '../page-access.js';

test('allows remote-control pages for network clients but blocks local interfaces', () => {
  assert.equal(allowsPageAccess('/remote', '192.168.1.42'), true);
  assert.equal(allowsPageAccess('/remote.html', '192.168.1.42'), true);
  assert.equal(allowsPageAccess('/operator', '192.168.1.42'), false);
  assert.equal(allowsPageAccess('/console.html', '192.168.1.42'), false);
  assert.equal(allowsPageAccess('/player.html', '192.168.1.42'), false);
  assert.equal(allowsPageAccess('/', '192.168.1.42'), false);
});

test('allows local clients to access the operator and player pages', () => {
  for (const address of ['127.0.0.1', '::1', '::ffff:127.0.0.1']) {
    assert.equal(allowsPageAccess('/operator', address), true);
    assert.equal(allowsPageAccess('/player.html', address), true);
  }
});

test('blocks URL-encoded local page paths from network clients', () => {
  assert.equal(allowsPageAccess('/%70layer.html?fullscreen=1', '10.0.0.5'), false);
  assert.equal(allowsPageAccess('/console%2Ehtml', '10.0.0.5'), false);
});
