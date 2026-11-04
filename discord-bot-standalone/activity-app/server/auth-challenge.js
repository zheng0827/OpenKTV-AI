import crypto from 'crypto';

export function randomToken(bytes = 24) {
  return crypto.randomBytes(bytes).toString('base64url');
}

export function createChallengeStore(ttlMs) {
  const store = new Map();

  function cleanup() {
    const now = Date.now();
    for (const [id, entry] of store.entries()) {
      if (entry.expiresAt <= now || entry.used) store.delete(id);
    }
  }

  function issue(redirectUri) {
    cleanup();
    const challengeId = randomToken(18);
    const state = randomToken(24);
    store.set(challengeId, {
      state,
      redirectUri: String(redirectUri),
      expiresAt: Date.now() + ttlMs,
      used: false,
    });
    return { challengeId, state, expiresInMs: ttlMs };
  }

  function consume({ challengeId, state, redirectUri }) {
    cleanup();
    const challenge = store.get(String(challengeId));
    if (!challenge || challenge.used || challenge.expiresAt <= Date.now()) return { ok: false, reason: 'invalid_or_expired_challenge' };
    if (challenge.state !== String(state) || challenge.redirectUri !== String(redirectUri)) return { ok: false, reason: 'challenge_mismatch' };
    challenge.used = true;
    store.delete(String(challengeId));
    return { ok: true };
  }

  return { issue, consume, cleanup, _size: () => store.size };
}

export function isAllowedRedirectOrigin(redirectUri, allowedOrigins, requestOrigin) {
  let parsed;
  try {
    parsed = new URL(String(redirectUri || ''));
  } catch {
    return false;
  }
  if (!allowedOrigins || allowedOrigins.size === 0) return parsed.origin === requestOrigin;
  return allowedOrigins.has(parsed.origin);
}
