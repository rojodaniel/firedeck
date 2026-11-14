// Minimal ASAR extractor: node asar-extract.js <asar> <outDir> [pathPrefix]
const fs = require('fs');
const path = require('path');
process.noAsar = true;

const asarPath = process.argv[2];
const outDir = process.argv[3];
const prefix = (process.argv[4] || '').replace(/\\/g, '/').replace(/^\/+/, '');

if (!asarPath || !outDir) {
  console.error('usage: node asar-extract.js <asar> <outDir> [pathPrefix]');
  process.exit(2);
}

const fd = fs.openSync(asarPath, 'r');
const sizeBuf = Buffer.alloc(8);
fs.readSync(fd, sizeBuf, 0, 8, 0);
const headerSize = sizeBuf.readUInt32LE(4);
const headerBuf = Buffer.alloc(headerSize);
fs.readSync(fd, headerBuf, 0, headerSize, 8);
const header = JSON.parse(headerBuf.subarray(4).toString('utf8'));
const dataOffset = 8 + headerSize;

let count = 0;
function walk(node, rel) {
  if (!node || !node.files) return;
  for (const [name, child] of Object.entries(node.files)) {
    const childRel = rel ? rel + '/' + name : name;
    if (child.files) {
      walk(child, childRel);
      continue;
    }
    if (prefix && !childRel.startsWith(prefix)) continue;
    if (child.unpacked) continue;
    const target = path.join(outDir, childRel);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    const length = Number(child.size || 0);
    const start = dataOffset + Number(child.offset || 0);
    const buf = Buffer.alloc(length);
    let read = 0;
    while (read < length) {
      read += fs.readSync(fd, buf, read, length - read, start + read);
    }
    fs.writeFileSync(target, buf);
    count++;
  }
}
walk(header, '');
fs.closeSync(fd);
console.log('extracted', count, 'files to', outDir);
