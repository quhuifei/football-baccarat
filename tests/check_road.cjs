const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto');
const assert = require('assert');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const road = vm.runInNewContext(source.slice(source.indexOf('function generateStandardRoad('), source.indexOf('function getTooltipContent(')) + ';generateStandardRoad');
let seed = 23;
const digest = crypto.createHash('sha256');
for (let n = 0; n < 200; n++) {
  const matches = Array.from({length: n * 3}, (_, i) => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return {result: 'BPT'[Math.floor(seed / 4294967296 * 3)], _idx: i, _seq: i};
  });
  digest.update(JSON.stringify(road(matches)));
}
// 优化前版本的200组固定种子样本：空序列、开头和局、长龙、转列与碰撞。
assert.equal(digest.digest('hex'), '3d58ed54feca93829270c5544e8728a71d49bf339e4b915f4783ddd46c9f911d');
console.log('200 road regressions passed');
