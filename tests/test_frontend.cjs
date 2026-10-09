// Pure DOM/bridge contract harness: no browser or account access.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const full = fs.readFileSync(process.argv[2], 'utf8');
const source = full.slice(full.indexOf('/* 飞书导入：'), full.indexOf('/* 企业微信导入：') < 0 ? undefined : full.indexOf('/* 企业微信导入：'));
assert(source.startsWith('/* 飞书导入：'));
const elements = new Map();
const timers = new Map();
let sequence = 0;
let state = {status:'idle', busy:false};
let calls = [];
let startReply = {ok:true};
let delayed = null;
let delayedStart = null;
let parameters = [];
let failRefresh = false;
const context = vm.createContext({
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {style:{}, textContent:'', src:'', disabled:false,
      removeAttribute(name) { delete this[name]; }});
    return elements.get(id);
  }},
  window: {addEventListener() {}},
  setTimeout(fn) {const id=++sequence; timers.set(id,fn);return id;},
  clearTimeout(id) {timers.delete(id);},
  async api(method, ...args) {
    calls.push(method);
    parameters.push([method,...args]);
    if (failRefresh && method.startsWith('refresh_')) throw new Error('window unavailable');
    if (method === 'get_feishu_import_progress') return delayed ? await delayed : state;
    if (method === 'start_feishu_import') {if(delayedStart) await delayedStart; return startReply;}
    if (method === 'cancel_feishu_import') {state={status:'cancelled',busy:false,generation:(state.generation||0)+1}; return {ok:true};}
  }
});
vm.runInContext(source, context);
const el = id => elements.get('feishu-import-' + id);
(async () => {
  await context.openFeishuImportDialog();
  assert.equal(el('overlay').style.display, 'flex');
  assert.equal(el('start').disabled, false);
  state={status:'waiting_scan',busy:true,qr_src:'data:image/svg+xml;base64,PHN2Zy8+'};
  await context.startFeishuImport();
  assert.equal(el('qr').style.display, '');
  assert.equal(el('start').disabled, true);
  const count=calls.filter(x=>x==='start_feishu_import').length;
  await context.startFeishuImport();
  assert.equal(calls.filter(x=>x==='start_feishu_import').length,count);
  await context.closeFeishuImportDialog();
  assert.equal(el('qr').src, undefined);
  assert.equal(el('start').disabled,false);
  await context.closeFeishuImportDialog();
  assert.equal(el('overlay').style.display,'none');
  // A late poll response must not repopulate a QR after cancellation.
  state={status:'running',busy:true};
  await context.openFeishuImportDialog();
  let resolve;
  delayed = new Promise(r => resolve=r);
  const latePoll=context.openFeishuImportDialog();
  delayed=null;
  await context.closeFeishuImportDialog();
  resolve({status:'waiting_scan',busy:true,qr_src:'data:image/svg+xml;base64,OLD'});
  await latePoll;
  assert.equal(el('qr').src,undefined);
  assert.equal(el('start').disabled,false);
  // Cancellation must carry the same host generation as the in-flight start.
  state={status:'idle',busy:false,generation:41};
  await context.openFeishuImportDialog();
  let releaseStart;
  delayedStart=new Promise(r=>releaseStart=r);
  const pendingStart=context.startFeishuImport();
  await context.closeFeishuImportDialog();
  startReply={ok:false,error:'stale generation'};
  releaseStart();
  await pendingStart;
  delayedStart=null;
  assert.deepEqual(parameters.filter(p=>p[0]==='start_feishu_import').at(-1),['start_feishu_import',41]);
  assert.deepEqual(parameters.filter(p=>p[0]==='cancel_feishu_import').at(-1),['cancel_feishu_import',41]);
  assert.equal(el('start').disabled,false);
  assert.equal(el('qr').src,undefined);
  startReply={ok:false,error:'缺少依赖'};
  await context.startFeishuImport();
  assert.equal(el('message').textContent,'缺少依赖');
  assert.equal(el('start').disabled,false);
  context.renderFeishuState({status:'done',busy:false,result:{ids:[1],skipped_dup:2,rejected:0,unaccounted:0}});
  assert(el('result').textContent.includes('新增 1'));
  // Finishing an import must refresh the real main window through its existing API.
  for (const status of ['done', 'partial', 'cancelled']) {
    calls=[];
    state={status,busy:false,result:{ids:[1],skipped_dup:0,rejected:0,unaccounted:0}};
    await context.openFeishuImportDialog();
    assert(calls.includes('refresh_memes'), `${status}: main grid remains stale`);
    assert(calls.includes('refresh_collections'), `${status}: group tree remains stale`);
  }
  calls=[];
  state={status:'done',busy:false,result:{ids:[],skipped_dup:4,rejected:0,unaccounted:0}};
  await context.openFeishuImportDialog();
  assert(!calls.includes('refresh_memes'));
  state={status:'done',busy:false,result:{ids:[],existing_ids:[1],skipped_dup:1,rejected:0,unaccounted:0}};
  await context.openFeishuImportDialog();
  assert(calls.includes('refresh_collections'));
  failRefresh=true;
  state={status:'done',busy:false,message:'导入已完成',result:{ids:[1],skipped_dup:0,rejected:0,unaccounted:0}};
  await context.openFeishuImportDialog();
  assert.equal(el('message').textContent,'导入已完成');
  assert.equal(el('start').disabled,false);
  console.log('frontend contract: pass (QR, cancel, stale poll, duplicate start, dependency error, result)');
})().catch(error => {console.error(error);process.exitCode=1;});
