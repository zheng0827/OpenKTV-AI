function isLocalAddress(address = '') {
  const normalized = address.toLowerCase().replace(/^::ffff:/, '');
  return normalized === '::1' || normalized === '127.0.0.1' || normalized.startsWith('127.');
}

export function allowsPageAccess(url, remoteAddress) {
  if (isLocalAddress(remoteAddress)) return true;
  try {
    const pathname = decodeURIComponent(new URL(url, 'http://localhost').pathname).toLowerCase();
    const localOnlyPaths = new Set(['/operator', '/console', '/ktv', '/player', '/player.html', '/console.html']);
    return !localOnlyPaths.has(pathname) && !['/', '/home', '/home.html'].includes(pathname);
  } catch {
    return false;
  }
}
