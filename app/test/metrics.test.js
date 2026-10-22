import test from 'node:test';
import assert from 'node:assert/strict';
import { createRuntimeMetrics } from '../metrics.js';

test('aggregates runtime metrics counters and latency summaries', () => {
  const metrics = createRuntimeMetrics();
  metrics.trackConnection('admin');
  metrics.trackConnection('remote');
  metrics.trackPermissionDenied('seek');
  metrics.trackQueueMutation();
  metrics.trackPlayerCommand();
  metrics.trackSearch(12);
  metrics.trackSearch(18);
  metrics.trackAction(3);
  metrics.trackAction(9);
  metrics.trackCatalogReload(true);
  metrics.trackCatalogReload(false);
  metrics.trackDisconnection('remote');

  const snapshot = metrics.toJSON({ queueLength: 2, isPlaying: true, clientCount: 4 });
  assert.equal(snapshot.counters.socketConnectionsTotal, 2);
  assert.equal(snapshot.counters.socketDisconnectionsTotal, 1);
  assert.equal(snapshot.counters.permissionDeniedTotal, 1);
  assert.equal(snapshot.counters.catalogReloadFailureTotal, 1);
  assert.equal(snapshot.connectionsByRole.admin, 1);
  assert.equal(snapshot.connectionsByRole.remote, 0);
  assert.equal(snapshot.deniedByAction.seek, 1);
  assert.equal(snapshot.queue.length, 2);
  assert.equal(snapshot.queue.isPlaying, true);
  assert.equal(snapshot.latency.search.count, 2);
  assert.equal(snapshot.latency.action.count, 2);
});

test('renders prometheus text output', () => {
  const metrics = createRuntimeMetrics();
  const snapshot = metrics.toJSON({ queueLength: 0, isPlaying: false, clientCount: 0 });
  const text = metrics.toPrometheusSnapshot(snapshot);
  assert.match(text, /ktv_uptime_seconds/);
  assert.match(text, /ktv_socket_connections_total/);
  assert.match(text, /ktv_queue_length/);
});
