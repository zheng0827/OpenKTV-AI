import {
  ActionRowBuilder,
  ButtonBuilder,
  ButtonStyle,
  Client,
  EmbedBuilder,
  GatewayIntentBits,
  REST,
  Routes,
  SlashCommandBuilder,
  StringSelectMenuBuilder,
  escapeMarkdown,
} from 'discord.js';
import csv from 'csv-parser';
import { spawn } from 'child_process';
import { execFile as execFileCallback } from 'child_process';
import './environment.js';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { promisify } from 'util';

const execFile = promisify(execFileCallback);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');

const catalogPath = path.resolve(
  projectRoot,
  process.env.KTV_LIBRARY_INDEX_PATH || path.join(process.env.KTV_SONGS_DIR || 'ktv_songs', 'library_index.csv'),
);
const nodePort = Number(process.env.KTV_NODE_PORT) || 3000;
const apiBase = (process.env.KTV_BOT_API_URL || `http://127.0.0.1:${nodePort}`).replace(/\/+$/, '');
const python = process.env.KTV_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const ytDlp = process.env.KTV_YTDLP_PATH
  || (fs.existsSync(path.join(projectRoot, 'yt-dlp.exe')) ? path.join(projectRoot, 'yt-dlp.exe') : 'yt-dlp');
const ingestionFile = path.join(projectRoot, 'script', 'download_songs.py');
const intake = new Set();
const LANGUAGE_CHOICES = [
  { name: '自動判斷', value: 'auto' },
  { name: '中文', value: 'zh' },
  { name: '英文', value: 'en' },
  { name: '日文', value: 'ja' },
  { name: '韓文', value: 'ko' },
];

const commands = [
  new SlashCommandBuilder()
    .setName('songs')
    .setDescription('瀏覽或搜尋 KTV 曲庫')
    .addStringOption(option => option.setName('keyword').setDescription('歌名、歌手或歌詞').setMaxLength(100))
    .addIntegerOption(option => option.setName('page').setDescription('頁數，從 1 開始').setMinValue(1)),
  new SlashCommandBuilder()
    .setName('song')
    .setDescription('查看曲庫歌曲詳細資訊')
    .addStringOption(option => option.setName('keyword').setDescription('歌曲 ID、歌名或歌手').setRequired(true).setMaxLength(100)),
  new SlashCommandBuilder().setName('room').setDescription('查看目前 KTV 歌房播放狀態'),
  new SlashCommandBuilder().setName('queue').setDescription('查看目前 KTV 播放佇列'),
  new SlashCommandBuilder()
    .setName('add')
    .setDescription('以關鍵字、YouTube 影片或播放清單新增歌曲')
    .addStringOption(option => option.setName('query').setDescription('YouTube 關鍵字或網址').setRequired(true).setMaxLength(500))
    .addStringOption(option => option.setName('language').setDescription('篩選 LRCLIB 歌詞語言')
      .addChoices(...LANGUAGE_CHOICES))
    .addStringOption(option => option.setName('separator').setDescription('人聲分離模式')
      .addChoices({ name: 'Demucs', value: 'demucs' }, { name: 'UVR', value: 'uvr' }, { name: 'Hybrid', value: 'hybrid' }))
    .addStringOption(option => option.setName('alignment').setDescription('歌詞對齊模型')
      .addChoices({ name: 'CTC', value: 'ctc' }, { name: 'WhisperX', value: 'whisperx' }, { name: 'Qwen', value: 'qwen' })),
].map(command => command.toJSON());

function display(value, fallback = '（無資料）') {
  const clean = String(value ?? '').trim();
  return clean ? escapeMarkdown(clean).slice(0, 1000) : fallback;
}

function elapsedTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function safeProcessLog(value) {
  return String(value || '沒有額外輸出')
    .replace(/\u001b\[[0-9;]*m/g, '')
    .replaceAll('`', 'ˋ')
    .slice(-1700);
}

async function readCatalog() {
  if (!fs.existsSync(catalogPath)) return [];
  return new Promise((resolve, reject) => {
    const rows = [];
    fs.createReadStream(catalogPath)
      .pipe(csv())
      .on('data', row => rows.push(row))
      .on('end', () => resolve(rows))
      .on('error', reject);
  });
}

function normalizeSearch(value) {
  return String(value || '').normalize('NFKC').toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
}

function findSongs(songs, query) {
  const cleanQuery = String(query || '').trim();
  if (!cleanQuery) return [...songs].sort((a, b) => String(a.title).localeCompare(String(b.title)));
  const normalized = normalizeSearch(cleanQuery);
  return songs.map(song => {
    const title = normalizeSearch(song.title);
    const artist = normalizeSearch(song.artist);
    const lyrics = normalizeSearch(song.lyrics);
    const id = normalizeSearch(song.id);
    const score = id === normalized ? 110 : title === normalized ? 100 : artist === normalized ? 90
      : id.includes(normalized) ? 70
      : title.includes(normalized) ? 60 : artist.includes(normalized) ? 50
        : lyrics.includes(normalized) ? 20 : 0;
    return { song, score };
  }).filter(item => item.score).sort((a, b) => b.score - a.score).map(item => item.song);
}

function songEmbed(song) {
  const embed = new EmbedBuilder()
    .setColor(0x77e8ce)
    .setTitle(display(song.title, '未命名歌曲'))
    .setDescription(`歌手：${display(song.artist)}\n曲庫 ID：\`${display(song.id)}\``);
  const fields = [
    ['專輯', song.album], ['發行年份', song.release_year], ['音源時長', song.duration_seconds ? elapsedTime(song.duration_seconds) : ''],
    ['歌手性別', song.artist_gender], ['語言', song.language], ['曲風', song.genre], ['分離模式', song.separator_mode],
    ['分離模型', song.separator_model], ['歌詞對齊模型', song.alignment_model],
    ['影片檔名', song.video_filename], ['伴奏檔名', song.instrumental_filename], ['歌詞檔名', song.lyrics_filename],
    ['人聲檔名', song.vocals_filename], ['去混響人聲檔名', song.dereverbed_vocals_filename],
    ['對齊結果檔名', song.alignment_results_filename], ['處理狀態', song.processing_status],
    ['建立時間', song.created_at], ['原始 YouTube 標題', song.youtube_title], ['來源 URL', song.source_url],
  ];
  for (const [name, value] of fields) {
    if (value) embed.addFields({ name, value: display(value, '（無資料）').slice(0, 1024), inline: true });
  }
  if (song.lyrics) embed.addFields({ name: '歌詞', value: display(song.lyrics).slice(0, 1024) });
  return embed;
}

function catalogEmbed(songs, page, query) {
  const pageSize = 10;
  const start = (page - 1) * pageSize;
  const items = songs.slice(start, start + pageSize);
  const description = items.length
    ? items.map((song, index) => `**${start + index + 1}. ${display(song.title, '未命名歌曲')}** — ${display(song.artist)}\n\`${display(song.id).slice(0, 14)}\` · ${song.language || '語言未標示'} · ${song.duration_seconds ? elapsedTime(song.duration_seconds) : '時長未知'}`).join('\n')
    : '此頁沒有歌曲。';
  return new EmbedBuilder()
    .setColor(0x77e8ce)
    .setTitle(query ? `曲庫搜尋：${display(query)}` : 'KTV 歌曲曲庫')
    .setDescription(description.slice(0, 4000))
    .setFooter({ text: `共 ${songs.length} 首 · 第 ${page} / ${Math.max(1, Math.ceil(songs.length / pageSize))} 頁` });
}

function ytVideoId(url) {
  try {
    const parsed = new URL(url);
    if (parsed.hostname === 'youtu.be' || parsed.hostname.endsWith('.youtu.be')) return parsed.pathname.split('/').filter(Boolean)[0] || '';
    if (parsed.hostname === 'youtube.com' || parsed.hostname.endsWith('.youtube.com')) {
      return parsed.searchParams.get('v') || parsed.pathname.match(/\/(?:shorts|live|embed)\/([^/]+)/)?.[1] || '';
    }
  } catch {}
  return '';
}

function canonicalYouTubeUrl(entry) {
  const candidate = entry.webpage_url || entry.original_url || entry.url || '';
  if (/^https?:\/\//i.test(candidate)) return candidate;
  return entry.id ? `https://www.youtube.com/watch?v=${encodeURIComponent(entry.id)}` : '';
}

function cleanVideoTitle(value) {
  return String(value || '').replace(/\s*[\[(](?:official|lyrics?|music video|mv|hd|4k)[^\])]*[\])]/ig, '')
    .replace(/\s*[-|]\s*(?:official|lyrics?|music video|mv|hd|4k).*$/i, '').trim();
}

function isCatalogDuplicate(entry, songs) {
  const id = ytVideoId(entry.youtube_url);
  const titleParts = cleanVideoTitle(entry.title).split(/\s+-\s+/);
  const candidateTitle = normalizeSearch(titleParts.length > 1 ? titleParts.slice(1).join(' - ') : titleParts[0]);
  const candidateArtist = normalizeSearch(titleParts.length > 1 ? titleParts[0] : '');
  return songs.some(song => {
    if (id && ytVideoId(song.source_url) === id) return true;
    if (song.source_url && song.source_url === entry.youtube_url) return true;
    const catalogTitle = normalizeSearch(song.title);
    const catalogArtist = normalizeSearch(song.artist);
    return candidateTitle && catalogTitle && candidateTitle === catalogTitle
      && (!candidateArtist || !catalogArtist || candidateArtist === catalogArtist);
  });
}

async function resolveYouTubeCandidates(query) {
  const isUrl = /^https?:\/\//i.test(query);
  if (isUrl) {
    const parsed = new URL(query);
    if (!['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be'].includes(parsed.hostname)
      || parsed.username || parsed.password || !['', '80', '443'].includes(parsed.port)) {
      throw new Error('只接受 YouTube 影片或播放清單網址。');
    }
  }
  const target = isUrl ? query : `ytsearch25:${query}`;
  const { stdout } = await execFile(ytDlp, [
    '--no-config', '--dump-single-json', '--flat-playlist', '--playlist-end', '250',
    '--skip-download', '--no-warnings', '--no-call-home', target,
  ], { cwd: projectRoot, timeout: 120_000, maxBuffer: 8 * 1024 * 1024, windowsHide: true });
  const payload = JSON.parse(stdout);
  const entries = Array.isArray(payload.entries) ? payload.entries : [payload];
  return entries.filter(Boolean).map(entry => ({
    title: String(entry.title || '').trim(),
    youtube_url: canonicalYouTubeUrl(entry),
    duration: Number(entry.duration) || 0,
  })).filter(entry => entry.title && entry.youtube_url).slice(0, 250);
}

function languageMatches(item, requested) {
  if (!requested || requested === 'auto') return true;
  const rawLanguage = String(item.language || '').toLowerCase();
  const declared = ({
    chinese: 'zh', zho: 'zh', mandarin: 'zh', cantonese: 'zh',
    english: 'en', eng: 'en', japanese: 'ja', jpn: 'ja', korean: 'ko', kor: 'ko',
  })[rawLanguage] || rawLanguage;
  const text = `${item.trackName || ''} ${item.plainLyrics || ''} ${item.syncedLyrics || ''}`;
  if (declared) {
    if (requested === 'zh' && declared.startsWith('zh')) return true;
    if (declared.startsWith(requested)) return true;
    if (['zh', 'en', 'ja', 'ko'].includes(declared.slice(0, 2)) && declared !== 'unknown') return false;
  }
  if (requested === 'ja') return /[\u3040-\u30ff]/u.test(text);
  if (requested === 'ko') return /[\uac00-\ud7af]/u.test(text);
  if (requested === 'zh') return /[\u3400-\u9fff]/u.test(text) && !/[\u3040-\u30ff\uac00-\ud7af]/u.test(text);
  if (requested === 'en') return /[A-Za-z]/u.test(text) && !/[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]/u.test(text);
  return false;
}

function trackAndArtist(title) {
  const clean = cleanVideoTitle(title);
  const parts = clean.split(/\s+-\s+/);
  if (parts.length > 1) return { artist: parts[0].trim(), track: parts.slice(1).join(' - ').trim() };
  return { artist: '', track: clean };
}

async function searchLyrics(entry, language) {
  const { artist, track } = trackAndArtist(entry.title);
  if (!track) return [];
  const queries = [artist ? { track_name: track, artist_name: artist } : { track_name: track }];
  if (artist) queries.push({ track_name: track });
  const matches = new Map();
  for (const query of queries) {
    const params = new URLSearchParams(query);
    const response = await fetch(`https://lrclib.net/api/search?${params}`, {
      headers: { 'User-Agent': 'OpenKTV-AI-DiscordBot/1.0' },
      signal: AbortSignal.timeout(12_000),
    });
    if (!response.ok) throw new Error(`LRCLIB 回應 ${response.status}`);
    const rows = await response.json();
    for (const item of Array.isArray(rows) ? rows : []) {
      if (item && languageMatches(item, language)) matches.set(String(item.id || `${item.trackName}-${item.artistName}`), item);
    }
    if (matches.size >= 23) break;
  }
  return [...matches.values()].slice(0, 23);
}

function selectMenu(customId, candidates, page, selected) {
  const size = 25;
  const entries = candidates.slice(page * size, (page + 1) * size);
  const menu = new StringSelectMenuBuilder()
    .setCustomId(customId)
    .setPlaceholder(`第 ${page + 1} 頁：預設全選，可取消不下載歌曲`)
    .setMinValues(0)
    .setMaxValues(Math.max(1, entries.length))
    .addOptions(entries.map((entry, offset) => ({
      label: `${page * size + offset + 1}. ${cleanVideoTitle(entry.title) || entry.title}`.slice(0, 100),
      description: entry.youtube_url.slice(0, 100),
      value: String(page * size + offset),
      default: selected.has(page * size + offset),
    })));
  const row = new ActionRowBuilder().addComponents(menu);
  const pageCount = Math.ceil(candidates.length / size);
  const buttons = new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId('picker:prev').setLabel('上一頁').setStyle(ButtonStyle.Secondary).setDisabled(page === 0),
    new ButtonBuilder().setCustomId('picker:next').setLabel('下一頁').setStyle(ButtonStyle.Secondary).setDisabled(page >= pageCount - 1),
    new ButtonBuilder().setCustomId('picker:confirm').setLabel(`確認選取 (${selected.size})`).setStyle(ButtonStyle.Success),
  );
  return { components: [row, buttons] };
}

async function choosePlaylistSongs(interaction, message, candidates) {
  const selected = new Set(candidates.map((_, index) => index));
  let page = 0;
  await message.edit({
    content: `找到 ${candidates.length} 首，已先過濾曲庫中已有的歌曲。播放清單預設全選；可逐頁取消不需要下載的項目。`,
    allowedMentions: { parse: [] },
    ...selectMenu('playlist-picker', candidates, page, selected),
  });
  const collector = message.createMessageComponentCollector({
    time: 15 * 60 * 1000,
    filter: component => component.user.id === interaction.user.id,
  });
  return new Promise(resolve => {
    collector.on('collect', async component => {
      if (component.customId === 'playlist-picker') {
        const start = page * 25;
        for (let i = start; i < Math.min(start + 25, candidates.length); i++) selected.delete(i);
        component.values.forEach(value => selected.add(Number(value)));
      } else if (component.customId === 'picker:prev') {
        page = Math.max(0, page - 1);
      } else if (component.customId === 'picker:next') {
        page = Math.min(Math.ceil(candidates.length / 25) - 1, page + 1);
      } else if (component.customId === 'picker:confirm') {
        await component.deferUpdate();
        collector.stop('confirmed');
        return;
      }
      if (!component.deferred) await component.deferUpdate();
      await message.edit({
        content: `已選 ${selected.size} / ${candidates.length} 首。播放清單預設全選；可逐頁取消不需要下載的項目。`,
        allowedMentions: { parse: [] },
        ...selectMenu('playlist-picker', candidates, page, selected),
      });
    });
    collector.on('end', (_collected, reason) => {
      resolve(reason === 'confirmed' ? [...selected].sort((a, b) => a - b).map(index => candidates[index]) : null);
    });
  });
}

async function chooseLyricsForSongs(interaction, message, songs, language) {
  const selected = [];
  for (let index = 0; index < songs.length; index++) {
    const song = songs[index];
    if (index) await new Promise(resolve => setTimeout(resolve, 500));
    let results = [];
    try {
      results = await searchLyrics(song, language);
    } catch {}
    if (!results.length) {
      selected.push({ ...song, lrclib_url: '' });
      continue;
    }
    const options = [{
      label: '不指定歌詞（處理時自動搜尋）',
      description: '讓歌曲處理流程自行尋找歌詞',
      value: 'auto',
      default: true,
    }, {
      label: '這首及之後的歌曲都自動搜尋',
      description: '不再逐首詢問歌詞連結',
      value: 'auto_remaining',
    }, ...results.slice(0, 23).map((item, resultIndex) => ({
      label: `${item.trackName || '未命名歌曲'} — ${item.artistName || '未知歌手'}`.slice(0, 100),
      description: `${item.albumName || '未提供專輯'} · ${item.duration ? elapsedTime(item.duration) : '時長未知'}`.slice(0, 100),
      value: String(resultIndex),
    }))];
    const menu = new StringSelectMenuBuilder()
      .setCustomId(`lyrics:${index}`)
      .setPlaceholder(`第 ${index + 1}/${songs.length} 首：選擇正確歌詞或自動搜尋`)
      .setMinValues(1)
      .setMaxValues(1)
      .addOptions(options);
    const detail = results.length
      ? `LRCLIB 找到 ${results.length} 筆${language !== 'auto' ? `（已套用${LANGUAGE_CHOICES.find(item => item.value === language)?.name || ''}語言篩選）` : ''}。請為這首歌選擇歌詞連結。`
      : `沒有找到符合${language === 'auto' ? '' : `「${LANGUAGE_CHOICES.find(item => item.value === language)?.name}」`}條件的 LRCLIB 結果，將由處理流程自動搜尋。`;
    await message.edit({
      content: `歌詞選擇 ${index + 1}/${songs.length}：${display(song.title)}\n${detail}`,
      allowedMentions: { parse: [] },
      embeds: [],
      components: [new ActionRowBuilder().addComponents(menu)],
    });
    let choice = 'auto';
    let timedOut = false;
    try {
      const component = await message.awaitMessageComponent({
        time: 10 * 60 * 1000,
        filter: value => value.user.id === interaction.user.id && value.customId === `lyrics:${index}`,
      });
      choice = component.values[0];
      await component.deferUpdate();
    } catch {
      choice = 'auto';
      timedOut = true;
    }
    const result = choice === 'auto' || choice === 'auto_remaining' ? null : results[Number(choice)];
    selected.push({
      ...song,
      lrclib_url: result?.id ? `https://lrclib.net/api/get/${encodeURIComponent(result.id)}` : '',
    });
    if (choice === 'auto_remaining' || timedOut) {
      selected.push(...songs.slice(index + 1).map(item => ({ ...item, lrclib_url: '' })));
      break;
    }
  }
  return selected;
}

function csvCell(value) {
  const text = String(value ?? '');
  return `"${text.replaceAll('"', '""')}"`;
}

function createInputCsv(jobs) {
  const fields = ['youtube_url', 'youtube_title', 'title_is_auto', 'lrclib_url', 'separator_mode', 'alignment_model'];
  return `${fields.join(',')}\n${jobs.map(job => fields.map(field => csvCell(
    field === 'youtube_title' ? job.title : field === 'title_is_auto' ? 'true' : job[field],
  )).join(',')).join('\n')}\n`;
}

function runDownloader(inputPath, statusMessage) {
  return new Promise(resolve => {
    const child = spawn(python, [ingestionFile, '--input', inputPath], {
      cwd: projectRoot,
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    let log = '';
    let pending = '';
    let updateTimer = null;
    const append = chunk => {
      pending += chunk.toString('utf8');
      const lines = pending.split(/\r?\n/);
      pending = lines.pop() || '';
      log = `${log}${lines.join('\n')}\n`.slice(-3500);
      if (!updateTimer) {
        updateTimer = setTimeout(() => {
          updateTimer = null;
          statusMessage.edit({
            content: `歌曲處理中：\n\`\`\`\n${safeProcessLog(pending + '\n' + log)}\n\`\`\``,
            allowedMentions: { parse: [] },
          }).catch(() => {});
        }, 5000);
      }
    };
    child.stdout.on('data', append);
    child.stderr.on('data', append);
    child.once('error', error => resolve({ code: 1, log: `${log}\n${error.message}` }));
    child.once('close', code => {
      if (updateTimer) clearTimeout(updateTimer);
      resolve({ code: code ?? 1, log: `${log}\n${pending}`.trim() });
    });
  });
}

async function roomSnapshot() {
  const token = process.env.KTV_BOT_API_TOKEN || '';
  if (!token) throw new Error('尚未設定 KTV_BOT_API_TOKEN。');
  const response = await fetch(`${apiBase}/api/bot/room`, {
    headers: { Authorization: 'Bearer ' + token },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Node KTV 服務回應 ${response.status}，請確認伺服器與 KTV_BOT_API_TOKEN 設定。`);
  return response.json();
}

async function handleSongs(interaction) {
  const catalog = await readCatalog();
  const query = interaction.options.getString('keyword') || '';
  const rows = findSongs(catalog, query);
  const pages = Math.max(1, Math.ceil(rows.length / 10));
  const page = Math.min(interaction.options.getInteger('page') || 1, pages);
  await interaction.reply({ embeds: [catalogEmbed(rows, page, query)], ephemeral: true });
}

async function handleSong(interaction) {
  const rows = findSongs(await readCatalog(), interaction.options.getString('keyword'));
  if (!rows.length) return interaction.reply({ content: '曲庫中找不到符合的歌曲。', ephemeral: true });
  if (rows.length === 1 || rows[0].id === interaction.options.getString('keyword')) {
    return interaction.reply({ embeds: [songEmbed(rows[0])], ephemeral: true });
  }
  const choices = rows.slice(0, 25);
  const menu = new StringSelectMenuBuilder().setCustomId('song-detail').setPlaceholder('選擇要查看的歌曲')
    .addOptions(choices.map((song, index) => ({
      label: `${song.title || '未命名歌曲'} — ${song.artist || '未知歌手'}`.slice(0, 100),
      value: String(index),
    })));
  const message = await interaction.reply({
    content: `找到 ${rows.length} 首歌曲，請選擇：`,
    components: [new ActionRowBuilder().addComponents(menu)],
    ephemeral: true,
    fetchReply: true,
  });
  try {
    const chosen = await message.awaitMessageComponent({
      time: 60_000,
      filter: component => component.user.id === interaction.user.id && component.customId === 'song-detail',
    });
    await chosen.update({ content: '', embeds: [songEmbed(choices[Number(chosen.values[0])])], components: [] });
  } catch {
    await interaction.editReply({ content: '選擇已逾時。', embeds: [], components: [] });
  }
}

async function handleRoom(interaction) {
  const data = await roomSnapshot();
  const state = data.state || {};
  const song = data.currentSong || data.queue?.[0];
  const embed = new EmbedBuilder().setColor(0x77e8ce).setTitle('KTV 歌房狀態')
    .addFields(
      { name: '目前播放', value: song ? `${display(song.title)} — ${display(song.artist)}` : '尚未播放歌曲' },
      { name: '播放狀態', value: state.isPlaying ? '播放中' : '已暫停／待機', inline: true },
      { name: '播放位置', value: elapsedTime(state.position), inline: true },
      { name: '音軌', value: state.audioMode === 'accompaniment' ? '伴奏' : '原唱', inline: true },
      { name: '佇列歌曲', value: String(data.queue?.length || 0), inline: true },
      { name: '曲庫歌曲', value: String(data.songCount || 0), inline: true },
    );
  await interaction.reply({ embeds: [embed], ephemeral: true });
}

async function handleQueue(interaction) {
  const data = await roomSnapshot();
  const queue = data.queue || [];
  const description = queue.length
    ? queue.slice(0, 25).map((song, index) => `${index ? `${index}.` : '▶'} **${display(song.title)}** — ${display(song.artist)}`).join('\n')
    : '播放佇列目前是空的。';
  await interaction.reply({
    embeds: [new EmbedBuilder().setColor(0x77e8ce).setTitle(`KTV 播放佇列 (${queue.length})`).setDescription(description.slice(0, 4000))],
    ephemeral: true,
  });
}

async function handleAdd(interaction) {
  if (intake.size) return interaction.reply({ content: '目前已有歌曲處理工作執行中，請稍後再試。', ephemeral: true });
  if (!interaction.guildId || !interaction.channel?.send) {
    return interaction.reply({ content: '新增歌曲功能目前僅支援伺服器文字頻道。', ephemeral: true });
  }
  intake.add(interaction.user.id);
  let tempDirectory;
  let workflowMessage;
  try {
    await interaction.deferReply();
    workflowMessage = await interaction.fetchReply();
    const query = interaction.options.getString('query', true).trim();
    await workflowMessage.edit({
      content: `正在搜尋 YouTube：${display(query)}`,
      allowedMentions: { parse: [] },
    });
    const allCandidates = await resolveYouTubeCandidates(query);
    const catalog = await readCatalog();
    const candidates = allCandidates.filter(candidate => !isCatalogDuplicate(candidate, catalog));
    if (!candidates.length) {
      await workflowMessage.edit({ content: '沒有找到可新增的歌曲；搜尋結果可能都已存在於曲庫。', allowedMentions: { parse: [] } });
      return;
    }
    const chosen = await choosePlaylistSongs(interaction, workflowMessage, candidates);
    if (!chosen) {
      await workflowMessage.edit({ content: '操作逾時，已取消新增歌曲。', components: [], allowedMentions: { parse: [] } });
      return;
    }
    if (!chosen.length) {
      await workflowMessage.edit({ content: '沒有選取歌曲，已取消新增。', components: [], allowedMentions: { parse: [] } });
      return;
    }
    const language = interaction.options.getString('language') || 'auto';
    const withLyrics = await chooseLyricsForSongs(interaction, workflowMessage, chosen, language);
    const jobs = withLyrics.map(song => ({
      ...song,
      separator_mode: interaction.options.getString('separator') || '',
      alignment_model: interaction.options.getString('alignment') || '',
    }));
    tempDirectory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'openktv-discord-'));
    const inputPath = path.join(tempDirectory, 'songs.csv');
    await fs.promises.writeFile(inputPath, createInputCsv(jobs), { encoding: 'utf8', flag: 'wx' });
    await workflowMessage.edit({
      content: `已接收 ${jobs.length} 首歌曲，正在啟動 Python 歌曲處理流程。`,
      components: [],
      allowedMentions: { parse: [] },
    });
    const result = await runDownloader(inputPath, workflowMessage);
    await workflowMessage.edit({
      content: `${result.code === 0 ? '✅ 處理完成' : '⚠️ 處理結束，部分歌曲失敗'}\n\`\`\`\n${safeProcessLog(result.log)}\n\`\`\``,
      allowedMentions: { parse: [] },
    });
  } catch (error) {
    const message = String(error?.message || error).slice(0, 1500);
    if (workflowMessage) await workflowMessage.edit({
      content: `無法新增歌曲：${message}`,
      allowedMentions: { parse: [] },
    }).catch(() => {});
    else await interaction.editReply(`無法新增歌曲：${message}`).catch(() => {});
  } finally {
    intake.delete(interaction.user.id);
    if (tempDirectory) await fs.promises.rm(tempDirectory, { recursive: true, force: true }).catch(() => {});
  }
}

async function registerCommands(clientId, token, guildId) {
  const rest = new REST({ version: '10' }).setToken(token);
  const route = guildId
    ? Routes.applicationGuildCommands(clientId, guildId)
    : Routes.applicationCommands(clientId);
  await rest.put(route, { body: commands });
}

const token = process.env.DISCORD_BOT_TOKEN || '';
const clientId = process.env.DISCORD_CLIENT_ID || '';
if (!token || !clientId || !process.env.KTV_BOT_API_TOKEN) {
  console.error('請設定 DISCORD_BOT_TOKEN、DISCORD_CLIENT_ID 與 KTV_BOT_API_TOKEN。');
  process.exit(1);
}

const client = new Client({ intents: [GatewayIntentBits.Guilds] });
client.once('ready', () => console.log(`[Discord] 已登入為 ${client.user.tag}`));
client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;
  try {
    if (interaction.commandName === 'songs') return await handleSongs(interaction);
    if (interaction.commandName === 'song') return await handleSong(interaction);
    if (interaction.commandName === 'room') return await handleRoom(interaction);
    if (interaction.commandName === 'queue') return await handleQueue(interaction);
    if (interaction.commandName === 'add') return await handleAdd(interaction);
  } catch (error) {
    const message = `指令執行失敗：${String(error?.message || error).slice(0, 1000)}`;
    if (interaction.deferred || interaction.replied) {
      await interaction.followUp({ content: message, ephemeral: true }).catch(() => {});
    } else {
      await interaction.reply({ content: message, ephemeral: true }).catch(() => {});
    }
  }
});

await registerCommands(clientId, token, process.env.DISCORD_GUILD_ID || '');
await client.login(token);

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    client.destroy();
    process.exit(0);
  });
}
