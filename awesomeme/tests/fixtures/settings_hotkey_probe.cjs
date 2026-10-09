const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {JSDOM, VirtualConsole} = require('jsdom');
const root = path.resolve(__dirname, '../..');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const settings = {hotkey: 'Win+E', cloud_direct: false, sync_type: ''};

// Run real settings and compiled guide code against an isolated in-memory bridge.
function fixture(html, platform) {
  const calls = [], errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(String(error)));
  const dom = new JSDOM(html, {url:'http://127.0.0.1:12345/', runScripts:'outside-only', pretendToBeVisual:true, virtualConsole});
  const w = dom.window;
  Object.defineProperty(w.navigator, 'platform', {value: platform});
  w.matchMedia = () => ({matches:false, addEventListener(){}, removeEventListener(){}});
  w.ResizeObserver = class {observe(){} disconnect(){}};
  w.IntersectionObserver = class {observe(){} disconnect(){}};
  w.scrollTo = () => {};
  w.pywebview = {api: new Proxy({}, {get(_target, method) {return async(...args) => {
    calls.push([method, ...args]);
    if (method === 'get_settings' || method === 'reset_settings') return {...settings};
    if (method === 'get_init_data') return {memes:[], tags:[], collections:[], guide_ok:true};
    if (['plugin_list', 'get_tags', 'get_collections', 'search_memes'].includes(method)) return [];
    if (method === 'get_current_version') return '0.1.0';
    if (method === 'count_memes') return 0;
    if (method === 'check_update') return {disabled:true};
    return null;
  }}})};
  return {dom, w, calls, errors};
}

async function checkSettings(platform) {
  const f = fixture(fs.readFileSync(path.join(root,'src/webui/settings.html'),'utf8'), platform);
  const {w, calls, errors} = f;
  try {
    w.eval(fs.readFileSync(path.join(root,'src/webui/settings.js'),'utf8'));
    await w.getSettings();
    await tick();
    const input = w.document.getElementById('s-hotkey');
    const isMac = platform === 'MacIntel';
    assert.equal(input.disabled, isMac);
    assert.equal(w.document.getElementById('btn-env-check').style.display === 'none', isMac);
    await w.openEnvCheck();
    assert.equal(calls.some(call => call[0] === 'open_env_check'), !isMac);
    assert.equal(w.document.getElementById('s-hotkey-hint').hidden, !isMac);
    assert.equal(input.value, isMac ? '⌘ E' : 'Win+E');
    // Even direct invocation must not record unsupported Mac combinations.
    w.startHotkeyCapture(input);
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', {key:'k', ctrlKey:true, bubbles:true}));
    assert.equal(input.value, isMac ? '⌘ E' : 'Ctrl+K');
    if (isMac) input.value = 'Win+K';
    await w.saveSettings();
    assert.equal(calls.filter(call => call[0] === 'save_settings').at(-1)[1].hotkey, isMac ? 'Cmd+E' : 'Ctrl+K');
    await w.resetSettings();
    assert.equal(input.value, isMac ? '⌘ E' : 'Win+E');
    assert.equal(input.disabled, isMac);
    assert.deepEqual(errors, []);
  } finally { await tick(); await tick(); f.dom.window.close(); }
}

async function checkGuide(platform) {
  const f = fixture('<div id="app-mount"></div>', platform);
  const {w, calls, errors} = f;
  try {
    w.eval(fs.readFileSync(path.join(root,'src/webui/dist/ohmymeme.js'),'utf8'));
    await tick(); await tick();
    w.showGuide();
    await tick();
    w.document.querySelector('.guide-footer .btn-primary').click();
    await tick();
    const input = w.document.querySelector('.guide-hk');
    const isMac = platform === 'MacIntel';
    assert(input, 'Guide must reach the hotkey step');
    assert.equal(input.disabled, isMac);
    assert.equal(input.value, isMac ? '⌘ E' : 'Win+E');
    assert.equal(!!w.document.querySelector('.guide-row button'), !isMac);
    input.click();
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', {key:'k', ctrlKey:true, bubbles:true}));
    await tick();
    assert.equal(input.value, isMac ? '⌘ E' : 'Ctrl+K');
    w.document.querySelector('.guide-footer .btn-primary').click();
    await tick();
    assert.equal(calls.filter(call => call[0] === 'save_settings').at(-1)[1].hotkey, isMac ? 'Cmd+E' : 'Ctrl+K');
    assert.deepEqual(errors, []);
  } finally { await tick(); await tick(); f.dom.window.close(); }
}

(async () => {
  for (const platform of ['MacIntel', 'Win32']) {
    await checkSettings(platform);
    await checkGuide(platform);
  }
  console.log('settings hotkey contract: pass (Mac fixed, Windows capture, save, reset, guide)');
})().catch(error => {console.error(error); process.exitCode = 1;});
