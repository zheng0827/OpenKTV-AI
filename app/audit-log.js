import fs from 'fs';
import path from 'path';

export function createAuditLogger(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  return ({ action, result = 'allowed', reason = '', socket = null, details = {} } = {}) => {
    const entry = {
      ts: new Date().toISOString(),
      action: String(action || 'unknown'),
      result,
      reason: String(reason || ''),
      role: socket?.data?.role || 'viewer',
      socketId: socket?.id || '',
      clientIp: socket?.data?.clientIp || socket?.handshake?.address || '',
      userAgent: socket?.handshake?.headers?.['user-agent'] || '',
      details,
    };
    fs.appendFile(filePath, `${JSON.stringify(entry)}\n`, error => {
      if (error) console.error('[Audit] write failed:', error.message);
    });
  };
}
