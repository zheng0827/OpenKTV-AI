import './style.css';
import { DiscordSDK } from '@discord/embedded-app-sdk';

const app = document.querySelector('#app');

function esc(value) {
  return String(value ?? '').replace(/[&<>"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[char]);
}

function elapsed(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
}

function createState() {
  return {
    sdkReady: false,
    authReady: false,
    context: {},
    room: null,
    songs: [],
    error: '',
  };
}

const state = createState();

function render() {
  const queue = state.room?.queue || [];
  const currentSong = state.room?.currentSong || queue[0];
  app.innerHTML = `
    <main>
      <header>
        <h1>OpenKTV Discord Activity</h1>
        <p>真正 Discord 語音房內嵌 Activity 版本（Embedded App）。</p>
      </header>
      <section class="status">
        <div><strong>SDK：</strong>${state.sdkReady ? '已連線' : '未連線'}</div>
        <div><strong>授權：</strong>${state.authReady ? '完成' : '未完成'}</div>
        <div><strong>Guild：</strong>${esc(state.context.guildId || 'N/A')}</div>
        <div><strong>Voice Channel：</strong>${esc(state.context.channelId || 'N/A')}</div>
      </section>
      <section class="actions">
        <button id="refresh">重新整理房間狀態</button>
      </section>
      ${state.error ? `<section class="error">${esc(state.error)}</section>` : ''}
      <section class="card">
        <h2>目前播放</h2>
        <p>${currentSong ? `${esc(currentSong.title || '未命名')} — ${esc(currentSong.artist || '未知歌手')}` : '尚未播放歌曲'}</p>
        <ul>
          <li>播放狀態：${state.room?.state?.isPlaying ? '播放中' : '待機/暫停'}</li>
          <li>播放位置：${elapsed(state.room?.state?.position)}</li>
          <li>佇列數量：${queue.length}</li>
          <li>曲庫數量：${state.room?.songCount || 0}</li>
        </ul>
      </section>
      <section class="card">
        <h2>待播清單（前 10 首）</h2>
        <ol>
          ${queue.slice(0, 10).map(song => `<li>${esc(song.title || '未命名')} — ${esc(song.artist || '未知歌手')}</li>`).join('') || '<li>目前無歌曲</li>'}
        </ol>
      </section>
      <section class="card">
        <h2>曲庫快覽（前 20 首）</h2>
        <ol>
          ${state.songs.slice(0, 20).map(song => `<li>${esc(song.title || '未命名')} — ${esc(song.artist || '未知歌手')}</li>`).join('') || '<li>未取得曲庫資料</li>'}
        </ol>
      </section>
    </main>
  `;
  document.querySelector('#refresh')?.addEventListener('click', () => syncRoom());
}

async function sha256base64url(input) {
  const encoded = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest('SHA-256', encoded);
  return btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function randomString(length = 64) {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~';
  const data = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(data, n => chars[n % chars.length]).join('');
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${url} 回應 ${response.status}`);
  return response.json();
}

async function syncRoom() {
  try {
    const [room, songs] = await Promise.all([
      fetchJson('/api/ktv/room'),
      fetchJson('/api/ktv/songs?limit=20'),
    ]);
    state.room = room;
    state.songs = songs.items || [];
    state.error = '';
  } catch (error) {
    state.error = `同步失敗：${error.message || error}`;
  }
  render();
}

async function initDiscordAuth(config) {
  if (!config.clientId) {
    state.error = '尚未設定 DISCORD_ACTIVITY_CLIENT_ID';
    render();
    return;
  }
  const sdk = new DiscordSDK(config.clientId);
  await sdk.ready();
  state.sdkReady = true;
  state.context = {
    guildId: sdk.guildId || '',
    channelId: sdk.channelId || '',
    instanceId: sdk.instanceId || '',
  };
  render();

  const codeVerifier = randomString(96);
  const codeChallenge = await sha256base64url(codeVerifier);
  const challenge = await fetchJson('/api/discord/auth-challenge', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ redirectUri: window.location.origin }),
  });
  const stateParam = String(challenge.state || '');
  if (!stateParam || !challenge.challengeId) throw new Error('無法建立 Activity OAuth challenge');
  sessionStorage.setItem('ktvActivityChallengeId', String(challenge.challengeId));
  sessionStorage.setItem('ktvActivityState', stateParam);
  sessionStorage.setItem('ktvActivityCodeVerifier', codeVerifier);

  const auth = await sdk.commands.authorize({
    client_id: config.clientId,
    response_type: 'code',
    state: stateParam,
    prompt: 'none',
    scope: ['identify', 'guilds', 'applications.commands'],
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
    redirect_uri: window.location.origin,
  });
  if (!auth?.code) throw new Error('Discord OAuth 未回傳授權碼');
  if (!auth?.state || auth.state !== sessionStorage.getItem('ktvActivityState')) {
    throw new Error('Discord OAuth state 驗證失敗');
  }

  const tokenPayload = await fetchJson('/api/discord/token', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      code: auth.code,
      codeVerifier: sessionStorage.getItem('ktvActivityCodeVerifier') || codeVerifier,
      redirectUri: window.location.origin,
      challengeId: sessionStorage.getItem('ktvActivityChallengeId') || challenge.challengeId,
      state: auth.state,
    }),
  });
  sessionStorage.removeItem('ktvActivityChallengeId');
  sessionStorage.removeItem('ktvActivityState');
  sessionStorage.removeItem('ktvActivityCodeVerifier');

  await sdk.commands.authenticate({ access_token: tokenPayload.access_token });
  state.authReady = true;
  state.error = '';
  render();
}

async function bootstrap() {
  render();
  try {
    const config = await fetchJson('/api/activity/config');
    await initDiscordAuth(config);
  } catch (error) {
    state.error = `Activity 初始化失敗：${error.message || error}`;
    render();
  }
  await syncRoom();
}

bootstrap();
