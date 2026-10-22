export function applySecurityHeaders(req, res, next) {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
  res.setHeader('Permissions-Policy', 'camera=(), geolocation=(), microphone=(self)');
  res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
  res.setHeader('Cross-Origin-Opener-Policy', 'same-origin');

  const forwardedProto = String(req.get('x-forwarded-proto') || '').toLowerCase();
  const isSecure = req.secure || forwardedProto === 'https';
  if (isSecure) {
    res.setHeader('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
  }
  next();
}
