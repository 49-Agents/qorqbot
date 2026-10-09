// A small RFB 3.8 peer for the installed noVNC client, never an application stub.
// Owns synthetic pixels only; no Chrome profile, external site or native process.
import { createHash } from 'node:crypto';
import type { Duplex } from 'node:stream';
import type { SyntheticAPI } from './synthetic-api';

type Peer = { send: (data: Buffer) => void; close: () => void };

type Connection = { route: Peer; closed: boolean; shared: number | null; first: string;
  keys: { down: boolean; key: number }[]; pointers: { buttons: number; x: number; y: number }[] };
export class SyntheticBrowser {
  connections: Connection[] = [];
  maxLive = 0;
  get current() { return this.connections.at(-1)!; }
  attach(api: SyntheticAPI) {
    api.server.on('upgrade', (request, socket, head) => {
      if (!/^\/api\/v1\/browser\/[0-9a-f-]+\/stream$/.test(request.url || '')) { socket.destroy(); return; }
      const accept = createHash('sha1').update(request.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
      socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
      const sendFrame = (data: Buffer, opcode = 2) => {
        if (socket.destroyed) return;
        const header = Buffer.alloc(data.length < 126 ? 2 : data.length <= 65535 ? 4 : 10);
        header[0] = 0x80 | opcode;
        if (header.length === 2) header[1] = data.length;
        else if (header.length === 4) { header[1] = 126; header.writeUInt16BE(data.length, 2); }
        else { header[1] = 127; header.writeBigUInt64BE(BigInt(data.length), 2); }
        socket.write(Buffer.concat([header, data]));
      };
      const route: Peer = { send: data => sendFrame(data), close: () => { sendFrame(Buffer.from([3, 232]), 8); socket.end(); } };
      const connection: Connection = { route, closed: false, shared: null, first: '', keys: [], pointers: [] };
      this.connections.push(connection);
      this.maxLive = Math.max(this.maxLive, this.connections.filter(c => !c.closed).length);
      socket.on('close', () => { connection.closed = true; });
      socket.on('error', () => { connection.closed = true; });
      let buffer = Buffer.alloc(0), stage = 0, painted = false;
      const consume = (n: number) => { const result = buffer.subarray(0, n); buffer = buffer.subarray(n); return result; };
      const receive = (raw: Buffer) => {
        buffer = Buffer.concat([buffer, raw]);
        while (buffer.length) {
          if (stage === 0) {
            if (buffer.length < 12) return;
            connection.first = consume(12).toString();
            if (connection.first !== 'RFB 003.008\n') throw new Error('Expected raw RFB, without an application handshake');
            stage = 1; route.send(Buffer.from([1, 1]));
          } else if (stage === 1) {
            if (consume(1)[0] !== 1) throw new Error('Expected no-credentials RFB');
            stage = 2; route.send(Buffer.alloc(4));
          } else if (stage === 2) {
            connection.shared = consume(1)[0]; stage = 3;
            const name = Buffer.from('Synthetic browser'), init = Buffer.alloc(24);
            init.writeUInt16BE(640, 0); init.writeUInt16BE(360, 2);
            Buffer.from([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0]).copy(init, 4);
            init.writeUInt32BE(name.length, 20); route.send(Buffer.concat([init, name]));
          } else {
            const type = buffer[0];
            const size = type === 0 ? 20 : type === 2 ? (buffer.length >= 4 ? 4 + 4 * buffer.readUInt16BE(2) : 4)
              : type === 3 ? 10 : type === 4 ? 8 : type === 5 ? 6 : type === 6 ? (buffer.length >= 8 ? 8 + buffer.readUInt32BE(4) : 8) : 0;
            if (!size) throw new Error(`Unexpected RFB client message ${type}`);
            if (buffer.length < size) return;
            const data = consume(size);
            if (type === 4) connection.keys.push({ down: !!data[1], key: data.readUInt32BE(4) });
            if (type === 5) connection.pointers.push({ buttons: data[1], x: data.readUInt16BE(2), y: data.readUInt16BE(4) });
            if (type === 3 && !painted) {
              painted = true;
              const header = Buffer.alloc(16); header.writeUInt16BE(1, 2); header.writeUInt16BE(640, 8); header.writeUInt16BE(360, 10);
              const pixels = Buffer.alloc(640 * 360 * 4);
              for (let y = 0; y < 360; y++) for (let x = 0; x < 640; x++) {
                const field = x > 80 && x < 560 && y > 130 && y < 230;
                pixels.set(field ? [220, 220, 220, 0] : [70, 60, 50, 0], (y * 640 + x) * 4);
              }
              route.send(Buffer.concat([header, pixels]));
            }
          }
        }
      };
      readFrames(socket, head, receive, route.close);
      route.send(Buffer.from('RFB 003.008\n'));
    });
  }
  disconnect() { this.current.closed = true; this.current.route.close(); }
}

// Bounded test-only binary WebSocket framing. Chrome supplies masked frames.
function readFrames(socket: Duplex, initial: Buffer, receive: (data: Buffer) => void, close: () => void) {
  let bytes = initial;
  const drain = () => {
    while (bytes.length >= 2) {
      const opcode = bytes[0] & 15, masked = !!(bytes[1] & 128), short = bytes[1] & 127;
      const prefix = short < 126 ? 2 : short === 126 ? 4 : 10;
      if (bytes.length < prefix + 4) return;
      const size = short < 126 ? short : short === 126 ? bytes.readUInt16BE(2) : Number(bytes.readBigUInt64BE(2));
      if (!masked || size > 1024 * 1024 || !(bytes[0] & 128)) { socket.destroy(); return; }
      if (bytes.length < prefix + 4 + size) return;
      const data = Buffer.from(bytes.subarray(prefix + 4, prefix + 4 + size));
      for (let i = 0; i < data.length; i++) data[i] ^= bytes[prefix + i % 4];
      bytes = bytes.subarray(prefix + 4 + size);
      if (opcode === 8) { close(); return; }
      if (opcode !== 2) throw new Error('Expected binary RFB');
      receive(data);
    }
  };
  socket.on('data', chunk => { bytes = Buffer.concat([bytes, chunk]); drain(); }); drain();
}
