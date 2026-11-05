// Long-lived, loopback-only authenticated upstream relay. No request/secret logs.
const readline = require('node:readline');

async function main() {
  const { Server } = await import('proxy-chain');
  const input = readline.createInterface({ input: process.stdin });
  const line = await new Promise((resolve, reject) => {
    input.once('line', resolve);
    input.once('close', () => reject(new Error('missing configuration')));
  });
  input.close();
  const { upstreamProxyUrl } = JSON.parse(line);
  const upstream = new URL(upstreamProxyUrl);
  if (!['http:', 'https:', 'socks5:'].includes(upstream.protocol) || !upstream.hostname) {
    throw new Error('invalid upstream');
  }
  const server = new Server({ host: '127.0.0.1', port: 0, verbose: false,
    prepareRequestFunction: () => ({ upstreamProxyUrl: upstream.href }),
  });
  server.on('requestFailed', () => {});
  server.on('error', () => { process.stderr.write('Browser relay failed\n'); process.exit(1); });
  await server.listen();
  process.stdout.write(JSON.stringify({ port: server.port }) + '\n');
  const stop = async () => { await server.close(true); process.exit(0); };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}
main().catch(() => { process.stderr.write('Browser relay unavailable\n'); process.exit(1); });
