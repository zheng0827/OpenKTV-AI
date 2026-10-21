import '../../environment.js';
import express from 'express';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(__dirname, '..');
const distDir = path.join(appRoot, 'dist');
const port = Number(process.env.DISCORD_ACTIVITY_PORT) || 4173;

const apiBase = (process.env.KTV_BOT_API_URL || `http://127.0.0.1:${Number(process.env.KTV_NODE_PORT) || 3000}`).replace(/\/+$/, '');
const botApiToken = process.env.KTV_BOT_API_TOKEN || '';
const activityClientId = process.env.DISCORD_ACTIVITY_CLIENT_ID || process.env.DISCORD_CLIENT_ID || '';
const activityClientSecret = process.env.DISCORD_ACTIVITY_CLIENT_SECRET || '';

const app = express();
app.use(express.json({ limit: '256kb' }));

async function fetchKtv(pathname, options = {}) {
  const headers = {
    ...(options.headers || {}),
  };
  if (botApiToken) headers.Authorization = 'Bearer ' + botApiToken;
  const response = await fetch(`${apiBase}${pathname}`, { ...options, headers, signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error(`KTV API ${pathname} 回應 ${response.status}`);
  return response.json();
}

app.get('/api/activity/config', (_req, res) => {
  res.json({
    clientId: activityClientId,
  });
});

app.post('/api/discord/token', async (req, res) => {
  if (!activityClientId || !activityClientSecret) {
    return res.status(500).json({ error: 'Missing DISCORD_ACTIVITY_CLIENT_ID / DISCORD_ACTIVITY_CLIENT_SECRET' });
  }
  const { code, codeVerifier, redirectUri } = req.body || {};
  if (!code || !codeVerifier || !redirectUri) {
    return res.status(400).json({ error: 'code, codeVerifier, redirectUri are required' });
  }
  try {
    const body = new URLSearchParams({
      client_id: activityClientId,
      client_secret: activityClientSecret,
      grant_type: 'authorization_code',
      code: String(code),
      code_verifier: String(codeVerifier),
      redirect_uri: String(redirectUri),
    });
    const tokenRes = await fetch('https://discord.com/api/v10/oauth2/token', {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body,
      signal: AbortSignal.timeout(10000),
    });
    const payload = await tokenRes.json();
    if (!tokenRes.ok) {
      return res.status(tokenRes.status).json({ error: payload.error || 'oauth token exchange failed', detail: payload });
    }
    return res.json(payload);
  } catch (error) {
    return res.status(500).json({ error: String(error?.message || error) });
  }
});

app.get('/api/ktv/room', async (_req, res) => {
  if (!botApiToken) return res.status(500).json({ error: 'Missing KTV_BOT_API_TOKEN' });
  try {
    return res.json(await fetchKtv('/api/bot/room'));
  } catch (error) {
    return res.status(502).json({ error: String(error?.message || error) });
  }
});

app.get('/api/ktv/songs', async (req, res) => {
  const params = new URLSearchParams();
  if (req.query.q) params.set('q', String(req.query.q));
  if (req.query.limit) params.set('limit', String(req.query.limit));
  if (req.query.offset) params.set('offset', String(req.query.offset));
  const suffix = params.toString() ? `?${params}` : '';
  try {
    return res.json(await fetchKtv(`/api/songs${suffix}`, { headers: {} }));
  } catch (error) {
    return res.status(502).json({ error: String(error?.message || error) });
  }
});

if (fs.existsSync(distDir)) {
  app.use(express.static(distDir));
  app.get('*', (_req, res) => res.sendFile(path.join(distDir, 'index.html')));
} else {
  app.get('*', (_req, res) => {
    res.status(503).send('Activity app dist not found. Please run `npm run build` in discord-bot-standalone/activity-app first.');
  });
}

app.listen(port, () => {
  console.log(`[Activity] server listening on http://127.0.0.1:${port}`);
});
