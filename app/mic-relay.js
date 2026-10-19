const signalTypes = new Set(['offer', 'answer', 'candidate']);

function isLocalAddress(address = '') {
  const normalized = address.toLowerCase().replace(/^::ffff:/, '');
  return normalized === '::1' || normalized === '127.0.0.1' || normalized.startsWith('127.');
}

export function createMicRelay(io) {
  const players = new Set();
  let remoteId = null;
  let playerId = null;

  function stopSession(reason = 'stopped') {
    if (!remoteId) return;
    io.to(remoteId).emit('mic_status', { status: reason });
    if (playerId) io.to(playerId).emit('mic_stop');
    remoteId = null;
    playerId = null;
  }

  return {
    attach(socket) {
      const requestedRole = String(socket.handshake.query.role || '');
      const forwardedAddress = socket.handshake.headers['x-ktv-proxy-client-ip'];
      const address = isLocalAddress(socket.handshake.address) && forwardedAddress
        ? forwardedAddress
        : socket.handshake.address;
      const role = requestedRole === 'player' && !isLocalAddress(address) ? '' : requestedRole;
      socket.data.ktvRole = role;
      if (role === 'player') players.add(socket.id);

      socket.on('mic_request', () => {
        if (role !== 'remote') return;
        if (remoteId) {
          socket.emit('mic_status', { status: 'busy' });
          return;
        }
        const availablePlayer = [...players].find(id => io.sockets.sockets.has(id));
        if (!availablePlayer) {
          socket.emit('mic_status', { status: 'unavailable' });
          return;
        }
        remoteId = socket.id;
        playerId = availablePlayer;
        socket.emit('mic_status', { status: 'waiting' });
        io.to(playerId).emit('mic_request', { remoteId });
      });

      socket.on('mic_ready', () => {
        if (role === 'player' && socket.id === playerId && remoteId) {
          io.to(remoteId).emit('mic_status', { status: 'connecting' });
        }
      });

      socket.on('mic_signal', payload => {
        if (!remoteId || !playerId || !signalTypes.has(payload?.type)) return;
        const target = role === 'remote' && socket.id === remoteId
          ? playerId
          : role === 'player' && socket.id === playerId
            ? remoteId
            : null;
        if (target) io.to(target).emit('mic_signal', { type: payload.type, data: payload.data });
      });

      socket.on('mic_volume', value => {
        if (role !== 'remote' || socket.id !== remoteId || !playerId) return;
        const volume = Number(value);
        if (Number.isFinite(volume)) io.to(playerId).emit('mic_volume', Math.max(0, Math.min(1, volume)));
      });

      socket.on('mic_stop', () => {
        if ((role === 'remote' && socket.id === remoteId) || (role === 'player' && socket.id === playerId)) {
          stopSession();
        }
      });

      socket.on('disconnect', () => {
        if (role === 'player') players.delete(socket.id);
        if ((role === 'remote' && socket.id === remoteId) || (role === 'player' && socket.id === playerId)) {
          stopSession('disconnected');
        }
      });
    },
  };
}
