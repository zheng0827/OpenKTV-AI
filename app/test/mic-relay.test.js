import assert from 'node:assert/strict';
import test from 'node:test';
import { createMicRelay } from '../mic-relay.js';

class FakeSocket {
  constructor(id, role, address = '127.0.0.1') {
    this.id = id;
    this.handshake = { query: { role }, address, headers: {} };
    this.data = {};
    this.handlers = new Map();
    this.received = [];
  }

  on(event, handler) {
    this.handlers.set(event, handler);
  }

  emit(event, data) {
    this.received.push({ event, data });
  }

  send(event, data) {
    this.handlers.get(event)?.(data);
  }

  last(event) {
    return this.received.filter(item => item.event === event).at(-1)?.data;
  }
}

function setup() {
  const sockets = new Map();
  const io = {
    sockets: { sockets },
    to(id) {
      return { emit: (event, data) => sockets.get(id)?.emit(event, data) };
    },
  };
  const relay = createMicRelay(io);
  const connect = (id, role, address) => {
    const socket = new FakeSocket(id, role, address);
    sockets.set(id, socket);
    relay.attach(socket);
    return socket;
  };
  return { connect, sockets };
}

test('relays an exclusive remote microphone session between remote and local player', () => {
  const { connect } = setup();
  const player = connect('player-1', 'player');
  const remote = connect('remote-1', 'remote');
  const otherRemote = connect('remote-2', 'remote');

  remote.send('mic_request');
  assert.deepEqual(remote.last('mic_status'), { status: 'waiting' });
  assert.deepEqual(player.last('mic_request'), { remoteId: 'remote-1' });
  player.send('mic_ready');
  assert.deepEqual(remote.last('mic_status'), { status: 'connecting' });

  remote.send('mic_signal', { type: 'offer', data: 'offer-data' });
  assert.deepEqual(player.last('mic_signal'), { type: 'offer', data: 'offer-data' });
  player.send('mic_signal', { type: 'answer', data: 'answer-data' });
  assert.deepEqual(remote.last('mic_signal'), { type: 'answer', data: 'answer-data' });

  remote.send('mic_volume', 3);
  assert.equal(player.last('mic_volume'), 1);
  otherRemote.send('mic_request');
  assert.deepEqual(otherRemote.last('mic_status'), { status: 'busy' });

  remote.send('mic_stop');
  assert.deepEqual(remote.last('mic_status'), { status: 'stopped' });
  assert.equal(player.received.some(item => item.event === 'mic_stop'), true);
  otherRemote.send('mic_request');
  assert.deepEqual(player.last('mic_request'), { remoteId: 'remote-2' });
});

test('rejects signaling from clients without the matching remote or player role', () => {
  const { connect } = setup();
  const player = connect('player-1', 'player');
  const remote = connect('remote-1', 'remote');
  const intruder = connect('intruder', 'remote');
  remote.send('mic_request');

  intruder.send('mic_signal', { type: 'offer', data: 'forged' });
  intruder.send('mic_volume', 0.1);
  player.send('mic_signal', { type: 'ice-candidate', data: 'unsupported' });
  assert.equal(player.last('mic_signal'), undefined);
  assert.equal(player.last('mic_volume'), undefined);

  player.send('disconnect');
  assert.deepEqual(remote.last('mic_status'), { status: 'disconnected' });
});

test('reports unavailable if the Electron player is not connected', () => {
  const { connect } = setup();
  const remote = connect('remote-1', 'remote');
  remote.send('mic_request');
  assert.deepEqual(remote.last('mic_status'), { status: 'unavailable' });
});

test('does not accept a LAN socket impersonating the local player', () => {
  const { connect } = setup();
  const fakePlayer = connect('fake-player', 'player', '192.168.1.50');
  const remote = connect('remote-1', 'remote');
  remote.send('mic_request');
  assert.deepEqual(remote.last('mic_status'), { status: 'unavailable' });
  assert.equal(fakePlayer.last('mic_request'), undefined);
});
