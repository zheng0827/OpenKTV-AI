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
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import ytdl from 'ytdl-core';
import yts from 'yt-search';
import './environment.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');
const python = process.env.KTV_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const ingestionFile = path.join(projectRoot, 'script', 'download_songs.py');
const nodePort = Number(process.env.KTV_NODE_PORT) || 3000;
const apiBase = (process.env.KTV_BOT_API_URL || `http://127.0.0.1:${nodePort}`).replace(/\/+$/, '');
const catalogPath = path.resolve(
  projectRoot,
  process.env.KTV_LIBRARY_INDEX_PATH || path.join(process.env.KTV_SONGS_DIR || 'ktv_songs', 'library_index.csv'),
);
const prefix = (process.env.DISCORD_BOT_PREFIX || '!ktv').trim();
const activityBase = (process.env.KTV_ACTIVITY_BASE_URL || `${apiBase}/ktv`).replace(/\/+$/, '');
const micBase = (process.env.KTV_MIC_REMOTE_URL || `${apiBase}/remote`).replace(/\/+$/, '');
const embeddedActivityAppId = process.env.DISCORD_ACTIVITY_APPLICATION_ID || process.env.DISCORD_CLIENT_ID || '';
const intake = new Set();

const commands = [
  new SlashCommandBuilder().setName('songs').setDescription('瀏覽曲庫').addStringOption(o => o.setName('keyword').setDescription('歌名/歌手/歌詞')).addIntegerOption(o => o.setName('page').setDescription('頁數').setMinValue(1)),
  new SlashCommandBuilder().setName('song').setDescription('歌曲詳細資訊').addStringOption(o => o.setName('keyword').setDescription('ID/歌名/歌手').setRequired(true)),
  new SlashCommandBuilder().setName('room').setDescription('歌房狀態'),
  new SlashCommandBuilder().setName('queue').setDescription('播放佇列'),
  new SlashCommandBuilder().setName('activity').setDescription('建立 KTV activity 入口').addStringOption(o => o.setName('mode').setDescription('開啟模式').addChoices(
    { name: '播放頁', value: 'ktv' },
    { name: '低延遲 MIC', value: 'mic' },
  )),
  new SlashCommandBuilder().setName('activitylaunch').setDescription('在目前語音房建立 Discord 內嵌 Activity 邀請'),
  new SlashCommandBuilder().setName('add').setDescription('用關鍵字/YouTube 新增歌曲').addStringOption(o => o.setName('query').setDescription('關鍵字或 YouTube 網址').setRequired(true)),
].map(c => c.toJSON());

function display(value, fallback = '（無資料）') {
  const clean = String(value ?? '').trim();
  return clean ? escapeMarkdown(clean).slice(0, 1000) : fallback;
}

function elapsedTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function normalize(value) {
  return String(value || '').normalize('NFKC').toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
}

async function readCatalog() {
  if (!fs.existsSync(catalogPath)) return [];
  return new Promise((resolve, reject) => {
    const rows = [];
    fs.createReadStream(catalogPath).pipe(csv())
      .on('data', row => rows.push(row))
      .on('end', () => resolve(rows))
      .on('error', reject);
  });
}

function findSongs(songs, query) {
  const raw = String(query || '').trim();
  if (!raw) return [...songs].sort((a, b) => String(a.title).localeCompare(String(b.title)));
  const needle = normalize(raw);
  return songs.map(song => {
    const title = normalize(song.title);
    const artist = normalize(song.artist);
    const lyrics = normalize(song.lyrics);
    const id = normalize(song.id);
    const score = id === needle ? 110 : title === needle ? 100 : artist === needle ? 90
      : id.includes(needle) ? 70 : title.includes(needle) ? 60 : artist.includes(needle) ? 50 : lyrics.includes(needle) ? 20 : 0;
    return { song, score };
  }).filter(v => v.score).sort((a, b) => b.score - a.score).map(v => v.song);
}

function songEmbed(song) {
  const embed = new EmbedBuilder().setColor(0x77e8ce)
    .setTitle(display(song.title, '未命名歌曲'))
    .setDescription(`歌手：${display(song.artist)}\n曲庫 ID：\`${display(song.id)}\``);
  const fields = [
    ['專輯', song.album], ['年份', song.release_year], ['時長', song.duration_seconds ? elapsedTime(song.duration_seconds) : ''],
    ['語言', song.language], ['曲風', song.genre], ['處理狀態', song.processing_status], ['建立時間', song.created_at], ['來源 URL', song.source_url],
  ];
  for (const [name, value] of fields) if (value) embed.addFields({ name, value: display(value, '（無資料）').slice(0, 1024), inline: true });
  if (song.lyrics) embed.addFields({ name: '歌詞', value: display(song.lyrics).slice(0, 1024) });
  return embed;
}

function catalogEmbed(songs, page, query) {
  const pageSize = 10;
  const start = (page - 1) * pageSize;
  const items = songs.slice(start, start + pageSize);
  const text = items.length
    ? items.map((song, i) => `**${start + i + 1}. ${display(song.title, '未命名歌曲')}** — ${display(song.artist)}\n\`${display(song.id).slice(0, 14)}\` · ${song.language || '語言未標示'} · ${song.duration_seconds ? elapsedTime(song.duration_seconds) : '時長未知'}`).join('\n')
    : '此頁沒有歌曲。';
  return new EmbedBuilder().setColor(0x77e8ce)
    .setTitle(query ? `曲庫搜尋：${display(query)}` : 'KTV 曲庫')
    .setDescription(text.slice(0, 4000))
    .setFooter({ text: `共 ${songs.length} 首 · 第 ${page}/${Math.max(1, Math.ceil(songs.length / pageSize))} 頁` });
}

function ytVideoId(url) {
  try {
    const parsed = new URL(url);
    if (parsed.hostname === 'youtu.be' || parsed.hostname.endsWith('.youtu.be')) return parsed.pathname.split('/').filter(Boolean)[0] || '';
    if (parsed.hostname === 'youtube.com' || parsed.hostname.endsWith('.youtube.com')) return parsed.searchParams.get('v') || '';
  } catch {}
  return '';
}

function cleanVideoTitle(value) {
  return String(value || '').replace(/\s*[\[(](?:official|lyrics?|music video|mv|hd|4k)[^\])]*[\])]/ig, '')
    .replace(/\s*[-|]\s*(?:official|lyrics?|music video|mv|hd|4k).*$/i, '').trim();
}

function isCatalogDuplicate(entry, songs) {
  const id = ytVideoId(entry.youtube_url);
  const normalizedTitle = normalize(cleanVideoTitle(entry.title));
  return songs.some(song => {
    if (id && ytVideoId(song.source_url) === id) return true;
    if (song.source_url && song.source_url === entry.youtube_url) return true;
    return normalizedTitle && normalize(song.title) === normalizedTitle;
  });
}

async function roomSnapshot() {
  const token = process.env.KTV_BOT_API_TOKEN || '';
  if (!token) throw new Error('尚未設定 KTV_BOT_API_TOKEN');
  const response = await fetch(`${apiBase}/api/bot/room`, {
    headers: { Authorization: 'Bearer ' + token },
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error(`Node KTV 服務回應 ${response.status}`);
  return response.json();
}

async function searchCandidates(query) {
  const raw = String(query || '').trim();
  if (!raw) return [];
  if (ytdl.validateURL(raw)) {
    const info = await ytdl.getBasicInfo(raw);
    return [{
      title: cleanVideoTitle(info.videoDetails.title),
      youtube_url: `https://www.youtube.com/watch?v=${info.videoDetails.videoId}`,
      duration: Number(info.videoDetails.lengthSeconds) || 0,
    }];
  }
  const result = await yts(raw);
  return (result.videos || []).slice(0, 60).map(video => ({
    title: cleanVideoTitle(video.title),
    youtube_url: video.url,
    duration: Number(video.seconds) || 0,
  }));
}

function pickerPayload(candidates, selected, page) {
  const pageSize = 10;
  const pages = Math.max(1, Math.ceil(candidates.length / pageSize));
  const start = page * pageSize;
  const rows = candidates.slice(start, start + pageSize);
  const options = rows.map((entry, idx) => {
    const absolute = start + idx;
    return {
      label: `${absolute + 1}. ${entry.title}`.slice(0, 100),
      description: `${elapsedTime(entry.duration)} · ${entry.youtube_url}`.slice(0, 100),
      value: String(absolute),
      default: selected.has(absolute),
    };
  });
  const menu = new StringSelectMenuBuilder().setCustomId('pick:songs')
    .setPlaceholder('勾選要加入這次處理的歌曲')
    .setMinValues(0)
    .setMaxValues(Math.max(1, options.length))
    .addOptions(options);
  const controls = new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId('pick:prev').setLabel('上一頁').setStyle(ButtonStyle.Secondary).setDisabled(page <= 0),
    new ButtonBuilder().setCustomId('pick:next').setLabel('下一頁').setStyle(ButtonStyle.Secondary).setDisabled(page >= pages - 1),
    new ButtonBuilder().setCustomId('pick:confirm').setLabel(`確認 (${selected.size})`).setStyle(ButtonStyle.Success),
    new ButtonBuilder().setCustomId('pick:cancel').setLabel('取消').setStyle(ButtonStyle.Danger),
  );
  return {
    content: `搜尋到 ${candidates.length} 首（已排除曲庫重複）\n第 ${page + 1}/${pages} 頁，勾選後可按「下一頁」繼續選片。`,
    components: [new ActionRowBuilder().addComponents(menu), controls],
  };
}

async function chooseSongs(ownerId, botMessage, candidates) {
  const selected = new Set(candidates.map((_, idx) => idx));
  let page = 0;
  await botMessage.edit(pickerPayload(candidates, selected, page));
  const collector = botMessage.createMessageComponentCollector({
    time: 15 * 60 * 1000,
    filter: interaction => interaction.user.id === ownerId,
  });
  return new Promise(resolve => {
    collector.on('collect', async interaction => {
      if (interaction.customId === 'pick:songs') {
        const start = page * 10;
        for (let i = start; i < Math.min(start + 10, candidates.length); i++) selected.delete(i);
        interaction.values.forEach(value => selected.add(Number(value)));
      } else if (interaction.customId === 'pick:prev') {
        page = Math.max(0, page - 1);
      } else if (interaction.customId === 'pick:next') {
        page = Math.min(Math.ceil(candidates.length / 10) - 1, page + 1);
      } else if (interaction.customId === 'pick:confirm') {
        await interaction.deferUpdate();
        collector.stop('confirmed');
        return;
      } else if (interaction.customId === 'pick:cancel') {
        await interaction.deferUpdate();
        collector.stop('cancelled');
        return;
      }
      if (!interaction.deferred) await interaction.deferUpdate();
      await botMessage.edit(pickerPayload(candidates, selected, page));
    });
    collector.on('end', (_items, reason) => {
      if (reason !== 'confirmed') return resolve(null);
      resolve([...selected].sort((a, b) => a - b).map(i => candidates[i]));
    });
  });
}

function csvCell(value) {
  return `"${String(value ?? '').replaceAll('"', '""')}"`;
}

function createInputCsv(rows) {
  const fields = ['youtube_url', 'title'];
  return `${fields.join(',')}\n${rows.map(row => fields.map(field => csvCell(row[field])).join(',')).join('\n')}\n`;
}

function safeLog(value) {
  return String(value || '').replace(/\u001b\[[0-9;]*m/g, '').replaceAll('`', 'ˋ').slice(-1700) || '沒有額外輸出';
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
    const append = data => {
      log = `${log}${data.toString('utf8')}`.slice(-4000);
      statusMessage.edit({ content: `歌曲處理中...\n\`\`\`\n${safeLog(log)}\n\`\`\`` }).catch(() => {});
    };
    child.stdout.on('data', append);
    child.stderr.on('data', append);
    child.once('error', error => resolve({ code: 1, log: `${log}\n${error.message}` }));
    child.once('close', code => resolve({ code: code ?? 1, log }));
  });
}

async function handleAdd(ownerId, initialMessage, query) {
  const catalog = await readCatalog();
  await initialMessage.edit({ content: `正在搜尋：${display(query)}` });
  const found = await searchCandidates(query);
  const candidates = found.filter(item => !isCatalogDuplicate(item, catalog));
  if (!candidates.length) {
    await initialMessage.edit({ content: '沒有可新增歌曲（可能都已在曲庫）。', components: [] });
    return;
  }
  const chosen = await chooseSongs(ownerId, initialMessage, candidates);
  if (!chosen) {
    await initialMessage.edit({ content: '已取消。', components: [] });
    return;
  }
  if (!chosen.length) {
    await initialMessage.edit({ content: '未選取歌曲，已取消。', components: [] });
    return;
  }
  let tempDirectory = '';
  try {
    tempDirectory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'openktv-standalone-bot-'));
    const inputPath = path.join(tempDirectory, 'songs.csv');
    await fs.promises.writeFile(inputPath, createInputCsv(chosen), { encoding: 'utf8', flag: 'wx' });
    await initialMessage.edit({ content: `已送出 ${chosen.length} 首，開始呼叫 script/download_songs.py`, components: [] });
    const result = await runDownloader(inputPath, initialMessage);
    await initialMessage.edit({ content: `${result.code === 0 ? '✅ 完成' : '⚠️ 結束（部分失敗）'}\n\`\`\`\n${safeLog(result.log)}\n\`\`\`` });
  } finally {
    if (tempDirectory) await fs.promises.rm(tempDirectory, { recursive: true, force: true }).catch(() => {});
  }
}

async function runCatalogReply(target, keyword, page) {
  const rows = findSongs(await readCatalog(), keyword);
  const pages = Math.max(1, Math.ceil(rows.length / 10));
  const safePage = Math.min(Math.max(1, Number(page) || 1), pages);
  const embed = catalogEmbed(rows, safePage, keyword);
  if (target.deferReply) {
    await target.reply({ embeds: [embed], ephemeral: true });
  } else {
    await target.channel.send({ embeds: [embed] });
  }
}

async function runSongReply(target, keyword) {
  const rows = findSongs(await readCatalog(), keyword);
  if (!rows.length) {
    if (target.deferReply) await target.reply({ content: '曲庫找不到歌曲。', ephemeral: true });
    else await target.channel.send('曲庫找不到歌曲。');
    return;
  }
  const embed = songEmbed(rows[0]);
  if (target.deferReply) await target.reply({ embeds: [embed], ephemeral: true });
  else await target.channel.send({ embeds: [embed] });
}

async function runRoomReply(target) {
  const data = await roomSnapshot();
  const song = data.currentSong || data.queue?.[0];
  const embed = new EmbedBuilder().setColor(0x77e8ce).setTitle('KTV 歌房狀態').addFields(
    { name: '目前播放', value: song ? `${display(song.title)} — ${display(song.artist)}` : '尚未播放歌曲' },
    { name: '播放狀態', value: data.state?.isPlaying ? '播放中' : '待機', inline: true },
    { name: '播放位置', value: elapsedTime(data.state?.position), inline: true },
    { name: '佇列數量', value: String(data.queue?.length || 0), inline: true },
    { name: '曲庫總數', value: String(data.songCount || 0), inline: true },
  );
  if (target.deferReply) await target.reply({ embeds: [embed], ephemeral: true });
  else await target.channel.send({ embeds: [embed] });
}

async function runQueueReply(target) {
  const data = await roomSnapshot();
  const queue = data.queue || [];
  const text = queue.length ? queue.slice(0, 25).map((song, idx) => `${idx ? `${idx}.` : '▶'} **${display(song.title)}** — ${display(song.artist)}`).join('\n') : '目前沒有排隊歌曲。';
  const embed = new EmbedBuilder().setColor(0x77e8ce).setTitle(`KTV 佇列 (${queue.length})`).setDescription(text.slice(0, 4000));
  if (target.deferReply) await target.reply({ embeds: [embed], ephemeral: true });
  else await target.channel.send({ embeds: [embed] });
}

function modeLinks(mode) {
  if (mode === 'mic') {
    return {
      title: '低延遲 MIC Activity',
      url: `${micBase}?mode=low-latency`,
      note: '此連結對應 KTV 低延遲麥克風頁；請在播放器端開啟混響效果，使用者即可聽到混響後 MIC。',
    };
  }
  return {
    title: 'KTV 播放 Activity',
    url: activityBase,
    note: '此連結會開啟 KTV 播放頁，可用於 Discord 活動集合與外部活動邀請。',
  };
}

async function runActivityReply(target, mode) {
  const pick = modeLinks(mode);
  const embed = new EmbedBuilder().setColor(0x77e8ce).setTitle(pick.title).setDescription(`${pick.note}\n\n${pick.url}`);
  if (target.deferReply) await target.reply({ embeds: [embed], ephemeral: true });
  else await target.channel.send({ embeds: [embed] });
}

async function resolveVoiceChannelId(target, userId) {
  if (target.member?.voice?.channelId) return target.member.voice.channelId;
  if (!target.guild?.members?.fetch) return '';
  try {
    const member = await target.guild.members.fetch(userId);
    return member?.voice?.channelId || '';
  } catch {
    return '';
  }
}

async function createEmbeddedActivityInvite(voiceChannelId) {
  if (!embeddedActivityAppId) throw new Error('尚未設定 DISCORD_ACTIVITY_APPLICATION_ID。');
  const response = await fetch(`https://discord.com/api/v10/channels/${encodeURIComponent(voiceChannelId)}/invites`, {
    method: 'POST',
    headers: {
      Authorization: 'Bot ' + token,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      max_age: 0,
      max_uses: 0,
      temporary: false,
      target_type: 2,
      target_application_id: embeddedActivityAppId,
    }),
    signal: AbortSignal.timeout(10000),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload?.code) {
    throw new Error(`建立 Activity 邀請失敗（${response.status}）：${payload?.message || '未知錯誤'}`);
  }
  return `https://discord.gg/${payload.code}`;
}

async function runActivityLaunchReply(target, userId) {
  const channelId = await resolveVoiceChannelId(target, userId);
  if (!channelId) {
    const text = '請先加入要唱歌的語音房，再執行這個指令。';
    if (target.deferReply) await target.reply({ content: text, ephemeral: true });
    else await target.reply(text);
    return;
  }
  const invite = await createEmbeddedActivityInvite(channelId);
  const embed = new EmbedBuilder()
    .setColor(0x77e8ce)
    .setTitle('Discord 內嵌 Activity 邀請已建立')
    .setDescription(`請在語音房啟用：${invite}\n\n> 這是 Discord 內建 Activity 邀請（不是外跳 OpenKTV 頁面）。`);
  if (target.deferReply) await target.reply({ embeds: [embed], ephemeral: true });
  else await target.reply({ embeds: [embed] });
}

async function registerCommands(clientId, token, guildId) {
  const rest = new REST({ version: '10' }).setToken(token);
  const route = guildId ? Routes.applicationGuildCommands(clientId, guildId) : Routes.applicationCommands(clientId);
  await rest.put(route, { body: commands });
}

const token = process.env.DISCORD_BOT_TOKEN || '';
const clientId = process.env.DISCORD_CLIENT_ID || '';
if (!token || !clientId || !process.env.KTV_BOT_API_TOKEN) {
  console.error('請設定 DISCORD_BOT_TOKEN、DISCORD_CLIENT_ID、KTV_BOT_API_TOKEN');
  process.exit(1);
}

await registerCommands(clientId, token, process.env.DISCORD_GUILD_ID || '');
if (process.argv.includes('--register-only')) {
  console.log('Slash commands registered.');
  process.exit(0);
}

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent, GatewayIntentBits.GuildVoiceStates],
});

client.once('ready', () => console.log(`[Standalone Discord Bot] 已登入為 ${client.user.tag}`));

client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;
  try {
    if (interaction.commandName === 'songs') return runCatalogReply(interaction, interaction.options.getString('keyword') || '', interaction.options.getInteger('page') || 1);
    if (interaction.commandName === 'song') return runSongReply(interaction, interaction.options.getString('keyword', true));
    if (interaction.commandName === 'room') return runRoomReply(interaction);
    if (interaction.commandName === 'queue') return runQueueReply(interaction);
    if (interaction.commandName === 'activity') return runActivityReply(interaction, interaction.options.getString('mode') || 'ktv');
    if (interaction.commandName === 'activitylaunch') return runActivityLaunchReply(interaction, interaction.user.id);
    if (interaction.commandName === 'add') {
      if (intake.size) return interaction.reply({ content: '目前已有歌曲處理工作，請稍後再試。', ephemeral: true });
      intake.add(interaction.user.id);
      try {
        await interaction.reply({ content: '準備新增歌曲...', fetchReply: true });
        const msg = await interaction.fetchReply();
        await handleAdd(interaction.user.id, msg, interaction.options.getString('query', true));
      } finally {
        intake.delete(interaction.user.id);
      }
      return;
    }
  } catch (error) {
    const message = `指令失敗：${String(error?.message || error).slice(0, 1200)}`;
    if (interaction.deferred || interaction.replied) await interaction.followUp({ content: message, ephemeral: true }).catch(() => {});
    else await interaction.reply({ content: message, ephemeral: true }).catch(() => {});
  }
});

client.on('messageCreate', async message => {
  if (message.author.bot || !message.content.startsWith(prefix)) return;
  const payload = message.content.slice(prefix.length).trim();
  if (!payload) return;
  const [command, ...rest] = payload.split(/\s+/);
  const lower = command.toLowerCase();
  const raw = rest.join(' ').trim();
  try {
    if (lower === 'songs') {
      const page = Number(rest.at(-1));
      const keyword = Number.isFinite(page) ? rest.slice(0, -1).join(' ') : raw;
      await runCatalogReply(message, keyword, Number.isFinite(page) ? page : 1);
    } else if (lower === 'song') {
      if (!raw) return void message.reply(`用法：${prefix} song <關鍵字>`);
      await runSongReply(message, raw);
    } else if (lower === 'room') {
      await runRoomReply(message);
    } else if (lower === 'queue') {
      await runQueueReply(message);
    } else if (lower === 'activity') {
      await runActivityReply(message, raw === 'mic' ? 'mic' : 'ktv');
    } else if (lower === 'activitylaunch') {
      await runActivityLaunchReply(message, message.author.id);
    } else if (lower === 'add') {
      if (!raw) return void message.reply(`用法：${prefix} add <關鍵字或 YouTube URL>`);
      if (intake.size) return void message.reply('目前已有歌曲處理工作，請稍後再試。');
      intake.add(message.author.id);
      let status;
      try {
        status = await message.reply('準備新增歌曲...');
        await handleAdd(message.author.id, status, raw);
      } finally {
        intake.delete(message.author.id);
      }
    } else if (lower === 'help') {
      await message.reply(`指令：${prefix} songs [keyword] [page] | ${prefix} song <keyword> | ${prefix} room | ${prefix} queue | ${prefix} activity [ktv|mic] | ${prefix} activitylaunch | ${prefix} add <query>`);
    }
  } catch (error) {
    await message.reply(`指令失敗：${String(error?.message || error).slice(0, 1200)}`);
  }
});

await client.login(token);

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    client.destroy();
    process.exit(0);
  });
}
