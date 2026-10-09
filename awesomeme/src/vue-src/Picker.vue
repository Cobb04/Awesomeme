<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'

type Item = { id: number; name: string; preview: string }
const items = ref<Item[]>([]), groups = ref<{id: number; name: string}[]>([])
const keyword = ref(''), group = ref<number | null>(null), offset = ref(0), total = ref(0)
const loading = ref(false), committing = ref(false), error = ref(''), active = ref(-1)
const search = ref<HTMLInputElement>(), grid = ref<HTMLElement>(), failed = ref(new Set<number>())
let session: string | null = null, generation = 0, request = 0, shownRequest = 0
let timer: ReturnType<typeof setTimeout> | undefined, bridgeTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const page = computed(() => Math.floor(offset.value / 48) + 1)
const pages = computed(() => Math.max(1, Math.ceil(total.value / 48)))

// 每个查询绑定当前会话和请求代次；迟到结果不能覆盖新搜索。
async function load() {
  if (!session || committing.value) return
  const ownSession = session, ownRequest = ++request
  loading.value = true; error.value = ''; active.value = -1
  try {
    const result = await window.pywebview.api.search(ownSession, ownRequest, keyword.value, group.value, offset.value)
    if (session !== ownSession || request !== ownRequest) return
    if (result?.status !== 'ok') throw new Error('query_failed')
    items.value = result.items; groups.value = result.groups; total.value = result.total
    active.value = items.value.length ? 0 : -1
    shownRequest = ownRequest; failed.value = new Set()
    grid.value?.scrollTo?.({top: 0})
  } catch (_) {
    if (session === ownSession && request === ownRequest) {
      items.value = []; error.value = '图库加载失败，请重试'
    }
  } finally {
    if (session === ownSession && request === ownRequest) loading.value = false
  }
}

// 搜索正在组词时不提交查询；“最近使用”中的搜索切回全部图库。
function searchChanged(event?: Event) {
  if ((event as InputEvent)?.isComposing) return
  clearTimeout(timer)
  if (group.value === -3 && keyword.value) group.value = null
  offset.value = 0; request++; loading.value = true
  timer = setTimeout(load, 120)
}

function selectGroup(id: number | null) {
  if (committing.value) return
  clearTimeout(timer); group.value = id; offset.value = 0
  if (id === -3) keyword.value = ''
  load()
}

function turnPage(delta: number) {
  if (loading.value || committing.value) return
  offset.value = Math.max(0, offset.value + delta * 48); load()
}

// 只向后端提交记录 ID，不把路径或图片字节放进 JS 桥。
async function choose(item: Item) {
  if (!session || loading.value || committing.value || failed.value.has(item.id)) return
  const ownSession = session
  committing.value = true
  try {
    const result = await window.pywebview.api.choose(ownSession, shownRequest, item.id)
    if (session === ownSession && result?.status !== 'preparing') throw new Error('choose_failed')
  } catch (_) {
    if (session === ownSession) { committing.value = false; error.value = '图片未能选中，请重试' }
  }
}

function cancel() {
  const old = session; session = null; request++; clearTimeout(timer)
  if (old) window.pywebview.api.cancel(old)
}

function manage() {
  const old = session; session = null; request++; clearTimeout(timer)
  if (old) window.pywebview.api.manage(old)
}

// 沿导航方向跳过加载失败的图片，没有可用项时返回 -1。
function availableIndex(start: number, step: number) {
  for (let index = start; index >= 0 && index < items.value.length; index += step) {
    if (!failed.value.has(items.value[index].id)) return index
  }
  return -1
}

// 当前项不可用时把唯一的网格 Tab 入口移到邻近可用项。
function ensureActive() {
  if (active.value >= 0 && items.value[active.value] && !failed.value.has(items.value[active.value].id)) return
  const start = Math.max(0, active.value)
  const next = availableIndex(start, 1)
  active.value = next >= 0 ? next : availableIndex(start - 1, -1)
}

// 只聚焦可用图片；全部不可用时回到搜索框。
function focusActive() {
  const tile = grid.value?.querySelectorAll<HTMLButtonElement>('.picker-tile')[active.value]
  if (tile && !tile.disabled) tile.focus()
  else search.value?.focus()
}

// 图片加载失败后同步选中项和网格内已有的键盘焦点。
function onPreviewError(id: number) {
  const restoreGridFocus = !!grid.value?.contains(document.activeElement)
  failed.value.add(id)
  ensureActive()
  if (restoreGridFocus) nextTick(focusActive)
}

function onKey(event: KeyboardEvent) {
  if (event.isComposing) return
  if (event.key === 'Escape') { event.preventDefault(); cancel(); return }
  if (loading.value || committing.value) return
  const target = event.target as HTMLElement
  const inGrid = target.closest('.picker-grid')
  if (event.key === 'Enter' && target === search.value) {
    event.preventDefault(); ensureActive(); const item = items.value[active.value]; if (item) choose(item)
  } else if ((target === search.value && event.key === 'ArrowDown') ||
      (inGrid && ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'].includes(event.key))) {
    event.preventDefault()
    ensureActive()
    if (active.value < 0) return
    if (target !== search.value) {
      const step = ({ArrowDown: 4, ArrowUp: -4, ArrowLeft: -1, ArrowRight: 1} as Record<string, number>)[event.key]
      const start = Math.max(0, Math.min(items.value.length - 1, active.value + step))
      const next = availableIndex(start, step)
      if (next >= 0) active.value = next
    }
    nextTick(focusActive)
  }
}

// 每次呼出都从共享图库重读，清除上次搜索与选图状态。
function openSession(id: string, nextGeneration: number) {
  if (nextGeneration <= generation) return
  clearTimeout(timer); generation = nextGeneration; session = id; request = 0
  committing.value = false; keyword.value = ''; group.value = null; offset.value = 0
  items.value = []; error.value = ''; load(); nextTick(() => search.value?.focus())
}

// pywebview 可能先注入空 api 对象，必须等待方法就绪。
async function ready() {
  if (disposed) return
  if (typeof window.pywebview?.api?.ready !== 'function') {
    bridgeTimer = setTimeout(ready, 50); return
  }
  try { await window.pywebview.api.ready() }
  catch (_) { error.value = '面板初始化失败，请重新打开应用' }
}

onMounted(() => {
  (window as any).openSession = openSession
  document.addEventListener('keydown', onKey)
  ready()
})
onBeforeUnmount(() => {
  disposed = true; session = null; clearTimeout(timer); clearTimeout(bridgeTimer)
  document.removeEventListener('keydown', onKey)
  delete (window as any).openSession
})
</script>

<template>
  <main class="picker-shell" aria-label="Awesomeme 表情面板">
    <header class="picker-search">
      <img class="picker-brand" :src="'/resources/icon.png'" alt="Awesomeme" width="28" height="28">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg>
      <input ref="search" v-model="keyword" aria-label="搜索表情" placeholder="搜索名称或标签…" maxlength="200"
        :disabled="committing" @input="searchChanged" @compositionend="searchChanged">
      <button class="picker-esc" aria-label="收起面板" @click="cancel">esc</button>
    </header>
    <nav class="picker-groups" aria-label="分组">
      <button :class="{selected: group === null}" :aria-pressed="group === null" :disabled="committing" @click="selectGroup(null)">全部</button>
      <button :class="{selected: group === -3}" :aria-pressed="group === -3" :disabled="committing" @click="selectGroup(-3)">最近使用</button>
      <button v-for="entry in groups" :key="entry.id" :class="{selected: group === entry.id}" :aria-pressed="group === entry.id" :disabled="committing" @click="selectGroup(entry.id)">{{ entry.name }}</button>
    </nav>
    <div ref="grid" class="picker-grid" :aria-busy="loading || committing">
      <button v-for="(item, index) in items" :key="item.id" class="picker-tile" :class="{active: active === index}" :aria-label="item.name"
        :tabindex="active === index ? 0 : -1"
        :disabled="loading || committing || failed.has(item.id)" @focus="active = index" @click="choose(item)">
        <img v-if="!failed.has(item.id)" :src="item.preview" :alt="item.name" loading="lazy" draggable="false" @error="onPreviewError(item.id)">
        <span v-else class="picker-missing">图片不可用</span>
      </button>
      <div v-if="error" class="picker-empty" role="status">{{ error }}<button :disabled="committing" @click="load">重试</button></div>
      <div v-else-if="!items.length" class="picker-empty" role="status">{{ loading ? '正在加载…' : keyword ? '没有找到，试试名称或手动标签' : group === -3 ? '用过的表情会出现在这里' : '这里还没有表情' }}</div>
    </div>
    <footer class="picker-footer">
      <div class="picker-status"><span role="status">{{ committing ? '正在准备图片…' : `共 ${total} 张` }}</span><span class="picker-key-hint">方向键选择 · 回车使用</span></div>
      <div class="picker-pages">
        <button aria-label="上一页" :disabled="loading || committing || !offset" @click="turnPage(-1)">‹</button>
        <span>{{ page }} / {{ pages }}</span>
        <button aria-label="下一页" :disabled="loading || committing || offset + 48 >= total" @click="turnPage(1)">›</button>
      </div>
      <button class="picker-manage" @click="manage">管理表情 ↗</button>
    </footer>
  </main>
</template>

<style scoped>
.picker-shell{height:100vh;display:flex;flex-direction:column;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:13px;overflow:hidden;font:13px -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}
.picker-shell button{font:inherit;border:0;cursor:pointer;background:transparent;color:inherit}
.picker-shell button:disabled{cursor:default;opacity:.45}
.picker-shell :focus-visible{outline:2px solid var(--primary);outline-offset:-2px}
.picker-brand{border-radius:8px;flex-shrink:0}
.picker-search{height:62px;flex-shrink:0;display:flex;align-items:center;gap:10px;padding:0 16px;border-bottom:1px solid var(--border);background:var(--surface)}
.picker-search:focus-within{box-shadow:inset 0 -2px var(--primary)}
.picker-search svg{width:17px;height:17px;color:var(--muted);flex-shrink:0}
.picker-search input{width:100%;min-width:0;border:0;background:transparent;color:var(--fg);font:inherit;font-size:14px;outline:none}
.picker-search input:focus-visible{outline:none}
.picker-search input::placeholder{color:var(--muted)}
.picker-search .picker-esc{font-size:10px;color:var(--muted);border:1px solid var(--border);border-radius:5px;padding:3px 5px}
.picker-groups{display:flex;gap:6px;flex-shrink:0;padding:12px 13px 8px;overflow-x:auto;white-space:nowrap}
.picker-groups button{padding:6px 10px;border-radius:7px;font-size:12px;color:var(--fg-secondary)}
.picker-groups button:hover{background:var(--surface-2)}
.picker-groups button.selected{background:var(--primary-light);color:var(--primary-strong);font-weight:600}
.picker-grid{flex:1;min-height:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));grid-auto-rows:calc((100vw - 52px)/4);align-content:start;gap:8px;padding:8px 13px 12px;overflow-y:auto}
.picker-shell .picker-tile{height:100%;min-height:0;min-width:0;display:flex;align-items:center;justify-content:center;overflow:hidden;border-radius:10px;padding:5px;border:1px solid var(--border-hair);background:var(--surface)}
.picker-shell .picker-tile:hover{background:var(--surface-2);border-color:var(--primary)}
.picker-shell .picker-tile.active,.picker-shell .picker-tile:focus-visible{border-color:var(--primary);box-shadow:inset 0 0 0 1px var(--primary);background:var(--primary-light)}
.picker-tile img{display:block;width:100%;height:100%;object-fit:contain;border-radius:6px}
.picker-missing{font-size:11px;color:var(--muted)}
.picker-empty{grid-column:1/-1;text-align:center;padding:58px 8px;color:var(--muted);line-height:1.7;font-size:12px}
.picker-empty button{display:block;margin:12px auto;text-decoration:underline;color:var(--primary-strong)}
.picker-footer{min-height:48px;flex-shrink:0;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 14px;border-top:1px solid var(--border);font-size:11px;color:var(--muted);background:var(--surface)}
.picker-status{display:flex;flex-direction:column;gap:3px}
.picker-key-hint{font-size:10px}
.picker-pages{display:flex;align-items:center;gap:5px;font-variant-numeric:tabular-nums}
.picker-pages button{font-size:18px;padding:3px 8px}
.picker-footer .picker-manage{font-size:11px;color:var(--primary-strong);white-space:nowrap}
</style>
