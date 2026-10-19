import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function resolveCertificatePath(value) {
  return path.resolve(projectRoot, value);
}

function proxyHeaders(request) {
  const headers = { ...request.headers };
  delete headers['x-ktv-proxy-client-ip'];
  headers['x-ktv-proxy-client-ip'] = request.socket.remoteAddress || '';
  return headers;
}

export function startHttpsProxy({ port, targetPort, certPath, keyPath }) {
  if (!certPath && !keyPath) return Promise.resolve(null);
  if (!certPath || !keyPath) {
    return Promise.reject(new Error('KTV_TLS_CERT_PATH and KTV_TLS_KEY_PATH must both be configured.'));
  }

  let credentials;
  try {
    credentials = {
      cert: fs.readFileSync(resolveCertificatePath(certPath)),
      key: fs.readFileSync(resolveCertificatePath(keyPath)),
    };
  } catch (error) {
    return Promise.reject(new Error(`Could not read HTTPS certificate/key: ${error.message}`));
  }

  const server = https.createServer(credentials, (request, response) => {
    const upstream = http.request({
      hostname: '127.0.0.1',
      port: targetPort,
      method: request.method,
      path: request.url,
      headers: proxyHeaders(request),
    }, upstreamResponse => {
      response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.statusMessage, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    });
    upstream.on('error', error => {
      if (!response.headersSent) response.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
      response.end(`KTV service unavailable: ${error.message}`);
    });
    request.pipe(upstream);
  });

  server.on('upgrade', (request, clientSocket, head) => {
    const upstreamSocket = net.connect(targetPort, '127.0.0.1', () => {
      const rawHeaders = [];
      for (let index = 0; index < request.rawHeaders.length; index += 2) {
        if (request.rawHeaders[index].toLowerCase() === 'x-ktv-proxy-client-ip') continue;
        rawHeaders.push(`${request.rawHeaders[index]}: ${request.rawHeaders[index + 1]}`);
      }
      rawHeaders.push(`x-ktv-proxy-client-ip: ${request.socket.remoteAddress || ''}`);
      upstreamSocket.write(`${request.method} ${request.url} HTTP/${request.httpVersion}\r\n${rawHeaders.join('\r\n')}\r\n\r\n`);
      if (head.length) upstreamSocket.write(head);
      clientSocket.pipe(upstreamSocket);
      upstreamSocket.pipe(clientSocket);
    });
    upstreamSocket.on('error', () => {
      if (!clientSocket.destroyed) clientSocket.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n');
    });
    clientSocket.on('error', () => upstreamSocket.destroy());
  });

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '0.0.0.0', () => {
      server.removeListener('error', reject);
      resolve({
        close: () => new Promise(resolveClose => server.close(resolveClose)),
        port,
      });
    });
  });
}
