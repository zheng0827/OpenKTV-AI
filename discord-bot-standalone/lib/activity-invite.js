export function classifyActivityInviteError(status, code, fallbackMessage = '') {
  const numeric = Number(code || 0);
  if (status === 403 || numeric === 50013) {
    return '機器人缺少「建立邀請」權限，請在該語音頻道補齊權限後重試。';
  }
  if (status === 404 || numeric === 10003) {
    return '找不到語音頻道，請確認你仍在同一個伺服器語音房。';
  }
  if (status === 400 || numeric === 50035) {
    return '目前頻道不支援啟動 Activity，請改在一般語音頻道重試。';
  }
  if (status === 429) {
    return 'Discord API 目前限流，請稍候再試。';
  }
  return `建立 Activity 邀請失敗（${status}）：${fallbackMessage || '未知錯誤'}`;
}
