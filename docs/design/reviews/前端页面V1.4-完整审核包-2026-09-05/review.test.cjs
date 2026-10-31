const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const {
  CHAINS,
  PAGES,
  REVIEW_VERSION,
  buildExportPayload,
  filterPages,
  loadReview,
  parsePageFilename,
  saveReview,
  storageKey,
} = require('./review.js');

test('中文文件名可解析为编号和页面名', () => {
  assert.deepEqual(parsePageFilename('09A-数字人口播-用已有音频生成.png'), {
    id: '09A',
    title: '数字人口播-用已有音频生成',
  });
});

test('V1.4 固定包含 21 个唯一审核项', () => {
  assert.equal(PAGES.length, 21);
  assert.equal(new Set(PAGES.map((page) => page.id)).size, 21);
  assert.equal(new Set(PAGES.map((page) => page.file)).size, 21);
});

test('模块和业务链筛选保留原清单顺序', () => {
  assert.deepEqual(
    filterPages(PAGES, { module: '分发' }).map((page) => page.id),
    ['18', '19'],
  );
  assert.deepEqual(
    filterPages(PAGES, { chain: '照片到口播分身' }).map((page) => page.id),
    ['12', '14', '15', '09'],
  );
  assert.deepEqual(
    filterPages(PAGES, { chain: '已有音频到口播' }).map((page) => page.id),
    ['17', '09A', '10', '11'],
  );
});

test('五条业务链按 Manifest 定义覆盖完整衔接顺序', () => {
  assert.deepEqual(CHAINS, {
    文案到口播: ['02', '03', '04', '09', '10', '11', '18'],
    照片到人物置换: ['12', '14', '06', '05', '10'],
    照片到口播分身: ['12', '14', '15', '09'],
    声音到口播: ['12', '16', '09'],
    已有音频到口播: ['17', '09A', '10', '11'],
  });
});

test('审核记录按版本隔离并可恢复', () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  saveReview(storage, '09', { state: '通过', note: '结构清楚' });
  values.set('zhongshu-review-v1.3:09', JSON.stringify({ state: '需修改' }));
  assert.deepEqual(loadReview(storage, '09'), { state: '通过', note: '结构清楚' });
  assert.equal(storageKey('09'), `${REVIEW_VERSION}:09`);
});

test('导出数据包含版本、统计和全部 21 项', () => {
  const reviews = {
    '01': { state: '通过', note: '首页通过' },
    '09': { state: '需修改', note: '替换称谓' },
  };
  const payload = buildExportPayload(PAGES, reviews, new Date('2026-09-05T10:00:00.000Z'));
  assert.equal(payload.version, '1.4');
  assert.equal(payload.summary.total, 21);
  assert.equal(payload.summary.passed, 1);
  assert.equal(payload.summary.needsChanges, 1);
  assert.equal(payload.pages.length, 21);
  assert.deepEqual(payload.pages[0].review, reviews['01']);
});

test('预览层在视口内完整显示并使用最终缺图提示', () => {
  const css = fs.readFileSync('review.css', 'utf8');
  const html = fs.readFileSync('00-打开审核.html', 'utf8');
  const script = fs.readFileSync('review.js', 'utf8');
  assert.match(css, /dialog\{[^}]*height:min\(96dvh/);
  assert.match(css, /#viewer-image\{[^}]*object-fit:contain/);
  assert.match(css, /#viewer-image\{[^}]*max-height:calc\(96dvh - 58px\)/);
  assert.match(html, /图片未加载，请检查文件是否完整或刷新/);
  assert.match(script, /图片未加载，请检查文件是否完整或刷新/);
  assert.doesNotMatch(`${html}\n${script}`, /图片制作中/);
});
