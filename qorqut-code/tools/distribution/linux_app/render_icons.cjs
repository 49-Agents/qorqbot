'use strict';
// Draws an SVG at several pixel sizes with Electron's own renderer, as PNG files with a transparent background.
// build_linux_app.py uses this when rsvg-convert is not installed (the staged project already has Electron).
// It needs no display and no Qorqbot code: it only loads the SVG text it is given.
//
//   electron --no-sandbox --ozone-platform=headless --disable-gpu render_icons.cjs SOURCE.svg OUTPUT_DIR SIZE...
//
// Writes OUTPUT_DIR/SIZExSIZE.png for each size.
const fs = require('node:fs');
const path = require('node:path');
const { app, BrowserWindow } = require('electron');

const [source, output, ...sizes] = process.argv.slice(process.argv.findIndex((arg) => arg.endsWith('render_icons.cjs')) + 1);

app.disableHardwareAcceleration();

async function main() {
  if (!source || !output || sizes.length === 0 || sizes.some((size) => !/^\d+$/.test(size))) {
    throw new Error('usage: render_icons.cjs SOURCE.svg OUTPUT_DIR SIZE...');
  }
  const svg = fs.readFileSync(source, 'utf8');
  fs.mkdirSync(output, { recursive: true });
  await app.whenReady();
  const window = new BrowserWindow({ show: false, width: 64, height: 64 });
  await window.loadURL('data:text/html,<canvas id=c></canvas>');
  for (const size of sizes.map(Number)) {
    // Drawing the SVG image to a canvas of the target size rasterizes the vectors at that size, not a scaled bitmap.
    const url = await window.webContents.executeJavaScript(`new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        const canvas = document.getElementById('c');
        canvas.width = ${size};
        canvas.height = ${size};
        canvas.getContext('2d').drawImage(image, 0, 0, ${size}, ${size});
        resolve(canvas.toDataURL('image/png'));
      };
      image.onerror = () => reject(new Error('the SVG did not load'));
      image.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(${JSON.stringify(svg)})));
    })`);
    fs.writeFileSync(path.join(output, `${size}x${size}.png`), Buffer.from(url.split(',')[1], 'base64'));
  }
}

main().then(() => app.exit(0), (error) => { console.error('render_icons: ' + error.message); app.exit(1); });
