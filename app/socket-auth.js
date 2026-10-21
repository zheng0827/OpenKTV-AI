export function evaluateSocketPermission({ role = 'viewer', allowedRoles = [], requireToken = false, hasValidToken = false } = {}) {
  if (allowedRoles.length && !allowedRoles.includes(role)) return { ok: false, reason: 'forbidden_role' };
  if (requireToken && role !== 'player' && !hasValidToken) return { ok: false, reason: 'invalid_control_token' };
  return { ok: true, reason: '' };
}
