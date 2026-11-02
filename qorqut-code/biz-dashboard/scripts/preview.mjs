#!/usr/bin/env node
// Frontend-only preview: static files from this worktree, API/streams from an
// existing Qorqut installation. No second backend, database or release switch.
import http from 'node:http';
import {createReadStream} from 'node:fs';
import {realpath, stat} from 'node:fs/promises';
import {dirname, extname, resolve, sep} from 'node:path';
import {fileURLToPath} from 'node:url';

const args = process.argv.slice(2);
const option = (key, fallback) => { const index = args.indexOf(key); return index < 0 ? fallback : args[index + 1]; };
const host = option('--host', '127.0.0.1'), port = Number(option('--port', '4199'));
const upstreamOption = option('--upstream');
if (!upstreamOption) throw new Error('Pass --upstream with the HTTP origin of an existing installation, e.g. --upstream http://127.0.0.1:PORT');
const upstream = new URL(upstreamOption);
if (upstream.protocol !== 'http:') throw new Error('Use the installation HTTP origin.');
const root = await realpath(resolve(dirname(fileURLToPath(import.meta.url)), '..'));
const types = {'.html':'text/html; charset=utf-8', '.css':'text/css; charset=utf-8', '.js':'text/javascript; charset=utf-8', '.svg':'image/svg+xml', '.png':'image/png', '.woff2':'font/woff2', '.ico':'image/x-icon'};
const local = path => ({'/':'index.html', '/index.html':'index.html', '/files/view':'file-viewer.html', '/tables/view':'tables-view.html', '/app.css':'app.css'}[path]
  || (/^\/(js|css|assets)\//.test(path) ? path.slice(1) : null));
const headers = request => ({...request.headers, host: upstream.host,
  ...(request.headers.origin ? {origin: upstream.origin} : {})});

const server = http.createServer(async (request, response) => {
  let path;
  try { path = decodeURIComponent(new URL(request.url, 'http://preview').pathname); }
  catch { response.writeHead(400).end(); return; }
  const file = local(path);
  if (file) {
    if (!['GET', 'HEAD'].includes(request.method)) { response.writeHead(405).end(); return; }
    try {
      const target = await realpath(resolve(root, file));
      const info = await stat(target);
      if (!target.startsWith(root + sep) || !info.isFile()) throw new Error('Not a file');
      // Revalidate every load so worktree edits appear immediately, but reuse
      // unchanged scripts/styles across the parent and native pane document.
      const etag = `W/"${info.size}-${info.mtimeMs}"`;
      // Match production framing policy so embedded-pane regressions surface
      // in preview rather than only after deployment.
      const viewer = ['/files/view', '/tables/view'].includes(path);
      const cache = {'cache-control': 'no-cache', etag,
        'x-frame-options': viewer ? 'SAMEORIGIN' : 'DENY',
        'content-security-policy': "frame-ancestors " + (viewer ? "'self'" : "'none'")};
      if (request.headers['if-none-match'] === etag) { response.writeHead(304, cache).end(); return; }
      response.writeHead(200, {'content-type': types[extname(target)] || 'application/octet-stream', ...cache});
      if (request.method === 'HEAD') response.end(); else createReadStream(target).pipe(response);
    } catch { response.writeHead(404).end('Not found'); }
    return;
  }
  const remote = http.request(new URL(request.url, upstream), {method:request.method, headers:headers(request)}, result => {
    response.writeHead(result.statusCode, result.headers);
    result.pipe(response);
    response.on('close', () => result.destroy());
  });
  remote.on('error', () => { if (!response.headersSent) response.writeHead(502); response.end('Live backend unavailable'); });
  request.on('aborted', () => remote.destroy());
  request.pipe(remote);
});
server.on('upgrade', (request, socket, head) => {
  const remote = http.request(new URL(request.url, upstream), {headers:headers(request)});
  remote.on('upgrade', (response, peer, peerHead) => {
    const lines = [`HTTP/1.1 ${response.statusCode} ${response.statusMessage}`];
    for (let i = 0; i < response.rawHeaders.length; i += 2) lines.push(`${response.rawHeaders[i]}: ${response.rawHeaders[i+1]}`);
    socket.write(lines.join('\r\n') + '\r\n\r\n');
    if (peerHead.length) socket.write(peerHead);
    if (head.length) peer.write(head);
    peer.pipe(socket); socket.pipe(peer);
    peer.on('error', () => socket.destroy()); socket.on('error', () => peer.destroy());
    socket.on('close', () => peer.destroy()); peer.on('close', () => socket.destroy());
  });
  remote.on('response', response => { socket.end(`HTTP/1.1 ${response.statusCode} ${response.statusMessage}\r\nConnection: close\r\n\r\n`); response.resume(); });
  remote.on('error', () => socket.destroy());
  socket.on('error', () => remote.destroy());
  remote.end();
});
server.listen(port, host, () => console.log(`Preview http://${host}:${port} → ${upstream.origin} (live data)`));
