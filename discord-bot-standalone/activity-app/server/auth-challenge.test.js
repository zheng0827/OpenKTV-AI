import test from 'node:test';
import assert from 'node:assert/strict';
import { createChallengeStore, isAllowedRedirectOrigin } from './auth-challenge.js';

test('challenge store issues and consumes once', () => {
  const store = createChallengeStore(60_000);
  const challenge = store.issue('https://example.com');
  assert.ok(challenge.challengeId);
  assert.ok(challenge.state);

  const first = store.consume({
    challengeId: challenge.challengeId,
    state: challenge.state,
    redirectUri: 'https://example.com',
  });
  assert.equal(first.ok, true);

  const second = store.consume({
    challengeId: challenge.challengeId,
    state: challenge.state,
    redirectUri: 'https://example.com',
  });
  assert.equal(second.ok, false);
  assert.equal(second.reason, 'invalid_or_expired_challenge');
});

test('challenge store rejects mismatched state', () => {
  const store = createChallengeStore(60_000);
  const challenge = store.issue('https://example.com');
  const result = store.consume({
    challengeId: challenge.challengeId,
    state: 'bad-state',
    redirectUri: 'https://example.com',
  });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'challenge_mismatch');
});

test('redirect origin validation', () => {
  const allowlist = new Set(['https://openktv.example.com']);
  assert.equal(isAllowedRedirectOrigin('https://openktv.example.com/path', allowlist, 'https://x'), true);
  assert.equal(isAllowedRedirectOrigin('https://evil.example.com/path', allowlist, 'https://x'), false);
  assert.equal(isAllowedRedirectOrigin('https://same-origin.example/path', new Set(), 'https://same-origin.example'), true);
  assert.equal(isAllowedRedirectOrigin('not-a-url', new Set(), 'https://same-origin.example'), false);
});
