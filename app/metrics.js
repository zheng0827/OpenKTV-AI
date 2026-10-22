function nowMs() {
  return Number(process.hrtime.bigint() / 1000000n);
}

function createRollingTimer(maxSamples = 200) {
  const samples = [];
  return {
    push(value) {
      const number = Number(value);
      if (!Number.isFinite(number) || number < 0) return;
      samples.push(number);
      if (samples.length > maxSamples) samples.shift();
    },
    summary() {
      if (!samples.length) return { count: 0, avgMs: 0, p95Ms: 0, maxMs: 0 };
      const sorted = [...samples].sort((a, b) => a - b);
      const sum = samples.reduce((total, value) => total + value, 0);
      const p95Index = Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95));
      return {
        count: samples.length,
        avgMs: Number((sum / samples.length).toFixed(2)),
        p95Ms: Number(sorted[p95Index].toFixed(2)),
        maxMs: Number(sorted[sorted.length - 1].toFixed(2)),
      };
    },
  };
}

export function createRuntimeMetrics() {
  const startedAt = Date.now();
  const counters = {
    socketConnectionsTotal: 0,
    socketDisconnectionsTotal: 0,
    permissionDeniedTotal: 0,
    queueMutationsTotal: 0,
    playerCommandsTotal: 0,
    searchesTotal: 0,
    catalogReloadTotal: 0,
    catalogReloadFailureTotal: 0,
  };
  const byRole = { admin: 0, player: 0, remote: 0, viewer: 0 };
  const byAction = new Map();
  const timers = {
    search: createRollingTimer(),
    action: createRollingTimer(),
  };

  return {
    nowMs,
    trackConnection(role) {
      counters.socketConnectionsTotal += 1;
      if (byRole[role] !== undefined) byRole[role] += 1;
    },
    trackDisconnection(role) {
      counters.socketDisconnectionsTotal += 1;
      if (byRole[role] !== undefined) byRole[role] = Math.max(0, byRole[role] - 1);
    },
    trackPermissionDenied(action) {
      counters.permissionDeniedTotal += 1;
      byAction.set(action, (byAction.get(action) || 0) + 1);
    },
    trackQueueMutation() {
      counters.queueMutationsTotal += 1;
    },
    trackPlayerCommand() {
      counters.playerCommandsTotal += 1;
    },
    trackSearch(durationMs) {
      counters.searchesTotal += 1;
      timers.search.push(durationMs);
    },
    trackAction(durationMs) {
      timers.action.push(durationMs);
    },
    trackCatalogReload(ok) {
      counters.catalogReloadTotal += 1;
      if (!ok) counters.catalogReloadFailureTotal += 1;
    },
    toJSON({ queueLength = 0, isPlaying = false, clientCount = 0 } = {}) {
      return {
        startedAt: new Date(startedAt).toISOString(),
        uptimeSeconds: Math.floor((Date.now() - startedAt) / 1000),
        counters: { ...counters },
        connectionsByRole: { ...byRole },
        deniedByAction: Object.fromEntries(byAction.entries()),
        queue: { length: Number(queueLength) || 0, isPlaying: Boolean(isPlaying) },
        sockets: { clientCount: Number(clientCount) || 0 },
        latency: {
          search: timers.search.summary(),
          action: timers.action.summary(),
        },
      };
    },
    toPrometheusSnapshot(snapshot) {
      const lines = [
        '# TYPE ktv_uptime_seconds gauge',
        `ktv_uptime_seconds ${snapshot.uptimeSeconds}`,
        '# TYPE ktv_socket_connections_total counter',
        `ktv_socket_connections_total ${snapshot.counters.socketConnectionsTotal}`,
        '# TYPE ktv_socket_disconnections_total counter',
        `ktv_socket_disconnections_total ${snapshot.counters.socketDisconnectionsTotal}`,
        '# TYPE ktv_permission_denied_total counter',
        `ktv_permission_denied_total ${snapshot.counters.permissionDeniedTotal}`,
        '# TYPE ktv_queue_length gauge',
        `ktv_queue_length ${snapshot.queue.length}`,
        '# TYPE ktv_search_latency_avg_ms gauge',
        `ktv_search_latency_avg_ms ${snapshot.latency.search.avgMs}`,
        '# TYPE ktv_action_latency_avg_ms gauge',
        `ktv_action_latency_avg_ms ${snapshot.latency.action.avgMs}`,
      ];
      return `${lines.join('\n')}\n`;
    },
  };
}
