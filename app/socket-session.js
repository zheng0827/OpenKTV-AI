import { createHash, createHmac, timingSafeEqual } from 'crypto';

const DEFAULT_TTL_SECONDS = 60 * 60;

function base64urlEncode(value) {
  return Buffer.from(value).toString('base64url');
}

function base64urlDecode(value) {
  return Buffer.from(String(value || ''), 'base64url').toString('utf8');
}

function normalizeRole(role) {
  const normalized = String(role || '').trim().toLowerCase();
  return ['admin', 'player', 'remote', 'viewer'].includes(normalized) ? normalized : 'viewer';
}

function fingerprint(clientIp, userAgent) {
  return createHash('sha256')
    .update(`${String(clientIp || '')}|${String(userAgent || '')}`)
    .digest('hex');
}

function safeEqual(left, right) {
  const a = Buffer.from(String(left || ''));
  const b = Buffer.from(String(right || ''));
  return a.length === b.length && timingSafeEqual(a, b);
}

export function createSocketSessionManager({ secret, ttlSeconds = DEFAULT_TTL_SECONDS } = {}) {
  const sessionSecret = String(secret || '').trim();
  if (!sessionSecret) throw new Error('socket session secret is required');
  const ttlMs = Math.max(30, Number(ttlSeconds) || DEFAULT_TTL_SECONDS) * 1000;

  const sign = payloadText => createHmac('sha256', sessionSecret).update(payloadText).digest('base64url');

  return {
    issue({ role, clientIp, userAgent, now = Date.now() } = {}) {
      const payload = {
        role: normalizeRole(role),
        iat: now,
        exp: now + ttlMs,
        fp: fingerprint(clientIp, userAgent),
      };
      const payloadText = JSON.stringify(payload);
      return `${base64urlEncode(payloadText)}.${sign(payloadText)}`;
    },
    verify(token, { clientIp, userAgent, now = Date.now() } = {}) {
      const [encodedPayload, signature] = String(token || '').split('.');
      if (!encodedPayload || !signature) return { ok: false, reason: 'missing_token' };
      let payload;
      try {
        payload = JSON.parse(base64urlDecode(encodedPayload));
      } catch {
        return { ok: false, reason: 'malformed_token' };
      }
      const payloadText = JSON.stringify(payload);
      if (!safeEqual(signature, sign(payloadText))) return { ok: false, reason: 'invalid_signature' };
      if (!payload.exp || now > Number(payload.exp)) return { ok: false, reason: 'token_expired' };
      if (!safeEqual(payload.fp, fingerprint(clientIp, userAgent))) return { ok: false, reason: 'fingerprint_mismatch' };
      return {
        ok: true,
        role: normalizeRole(payload.role),
        expiresAt: Number(payload.exp),
        issuedAt: Number(payload.iat || 0),
      };
    },
  };
}

export function resolveGrantedRole({ requestedRole, isLocalClient = false, hasValidControlToken = false } = {}) {
  if (hasValidControlToken) return 'admin';
  const role = normalizeRole(requestedRole);
  if (role === 'remote') return 'remote';
  if (role === 'viewer') return 'viewer';
  if (isLocalClient && (role === 'admin' || role === 'player')) return role;
  return isLocalClient ? 'player' : 'viewer';
}
