const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('../../node_modules/jsdom');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
(async () => {
  const chosen = [], cancelled = [], managed = [], queries = [];
  let sample = [{id: 1, name: '你好', preview: '/api/thumb/hash1'},
    {id: 2, name: '收到', preview: '/api/original/2/hello.gif'}];
  let hold = false, pending = [];
  const dom = new JSDOM('<div id="app-mount"></div>', {
    url: 'http://localhost/picker/', runScripts: 'dangerously', beforeParse(w) {
      w.pywebview = {api: {
        ready: async () => ({page_size: 48}),
        search: (...args) => {
          queries.push(args);
          const response = {status: 'ok', items: args[2] ? sample.slice(1) : sample,
            total: args[2] ? 1 : 50, groups: [{id: 7, name: '微信'}]};
          if (hold) return new Promise(resolve => pending.push(() => resolve(response)));
          return Promise.resolve(response);
        },
        choose: async (...args) => {chosen.push(args); return {status: 'preparing'};},
        cancel: id => cancelled.push(id), manage: id => managed.push(id),
      }};
    },
  });
  const w = dom.window, doc = w.document;
  w.eval(fs.readFileSync(path.join(__dirname, '../../src/webui/dist/ohmymeme.js'), 'utf8'));
  await tick();
  w.openSession('new', 2); w.openSession('late', 1); await tick();
  const tiles = () => [...doc.querySelectorAll('.picker-tile')];
  assert.equal(tiles().length, 2);
  assert.equal(tiles()[0].textContent.trim(), '');
  assert.equal(tiles()[1].querySelector('img').getAttribute('src'), '/api/original/2/hello.gif');
  assert.equal(queries[0][0], 'new');
  doc.querySelector('[aria-label="下一页"]').click(); await tick();
  assert.equal(queries.at(-1)[4], 48);
  [...doc.querySelectorAll('nav button')].find(b => b.textContent === '微信').click(); await tick();
  assert.deepEqual(queries.at(-1).slice(3), [7, 0]);
  const search = doc.querySelector('input');
  search.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true}));
  assert.equal(chosen.length, 0);
  hold = true;
  search.value = '收'; search.dispatchEvent(new w.Event('input')); await new Promise(r => setTimeout(r, 140));
  w.openSession('fresh', 3); await tick();
  assert.equal(pending.length, 2);
  pending[1](); await tick(); pending[0](); await tick();
  assert.equal(tiles().length, 2, 'old search cannot overwrite reopened session');
  tiles()[1].click(); tiles()[1].click(); await tick();
  assert.deepEqual(chosen, [['fresh', 1, 2]]);
  search.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  assert.deepEqual(cancelled, ['fresh']);
  hold = false; w.openSession('manage', 4); await tick();
  doc.querySelector('.picker-manage').click();
  assert.deepEqual(managed, ['manage']);
  w.openSession('missing-first', 5); await tick();
  tiles()[0].querySelector('img').dispatchEvent(new w.Event('error')); await tick();
  assert.equal(tiles()[1].tabIndex, 0, 'first missing image must not remove every grid tab stop');
  search.focus(); search.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true})); await tick();
  assert.equal(doc.activeElement, tiles()[1]);
  search.focus(); search.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); await tick();
  assert.deepEqual(chosen.at(-1), ['missing-first', 1, 2]);
  sample = Array.from({length: 10}, (_, index) => ({id: index + 1, name: `图片 ${index}`, preview: `/api/thumb/${index}`}));
  w.openSession('keyboard', 6); await tick();
  for (const index of [1, 4]) tiles()[index].querySelector('img').dispatchEvent(new w.Event('error'));
  await tick(); tiles()[0].focus();
  for (const [key, index] of [['ArrowRight', 2], ['ArrowLeft', 0], ['ArrowDown', 8], ['ArrowUp', 0]]) {
    doc.activeElement.dispatchEvent(new w.KeyboardEvent('keydown', {key, bubbles: true})); await tick();
    assert.equal(doc.activeElement, tiles()[index], `${key} must skip disabled tiles in its direction`);
  }
  const previousChosen = chosen.length;
  for (const tile of tiles()) tile.querySelector('img')?.dispatchEvent(new w.Event('error'));
  await tick(); search.focus();
  search.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); await tick();
  assert.equal(chosen.length, previousChosen, 'all missing images must never submit a choice');
  dom.window.close();
  console.log('PASS: compiled picker mixed grid, image-only, pagination, groups, IME, stale search, double click, Escape, management, missing-image keyboard navigation');
})().catch(error => {console.error(error); process.exitCode = 1});
