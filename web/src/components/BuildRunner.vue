<script setup>
import { ref, computed } from 'vue'

const source = ref('https://github.com/DaveGamble/cJSON')
const target = ref('aarch64-linux-musl')
const items = ref([])
const meta = ref(null)
const result = ref(null)
const verifying = ref(false)
const error = ref('')
const running = ref(false)
const open = ref({})
let es = null

const elapsed = computed(() => items.value.reduce((s, i) => s + (i.ms || 0), 0))

function stop() {
  if (es) { es.close(); es = null }
  running.value = false
  verifying.value = false
}

function start() {
  if (running.value) return
  items.value = []
  meta.value = null
  result.value = null
  error.value = ''
  open.value = {}
  running.value = true

  const q = new URLSearchParams({ source: source.value, target: target.value })
  es = new EventSource(`/api/build?${q}`)

  es.onmessage = (e) => {
    const ev = JSON.parse(e.data)
    if (ev.type === 'run_started') {
      meta.value = ev
    } else if (ev.type === 'message') {
      items.value.push({ kind: 'message', step: ev.step, content: ev.content })
    } else if (ev.type === 'tool_call') {
      items.value.push({ kind: 'tool', id: items.value.length, step: ev.step, tool: ev.tool, args: ev.args, done: false })
    } else if (ev.type === 'tool_result') {
      const slot = items.value.find((i) => i.kind === 'tool' && i.tool === ev.tool && !i.done)
      if (slot) Object.assign(slot, { done: true, ok: ev.ok, ms: ev.ms, result: ev.result, errors: ev.errors, details: ev.details })
    } else if (ev.type === 'verifying') {
      verifying.value = true
    } else if (ev.type === 'finished') {
      result.value = ev
      stop()
    } else if (ev.type === 'error') {
      error.value = ev.message
      stop()
    }
  }

  es.onerror = () => {
    if (running.value && !result.value) {
      error.value = '连接断了。要么后端没起来，要么已经有一个构建在跑。'
    }
    stop()
  }
}

function short(args) {
  return Object.entries(args)
    .map(([k, v]) => `${k}=${String(v).length > 70 ? String(v).slice(0, 70) + '…' : v}`)
    .join('  ')
}
</script>

<template>
<div>
    <form class="bar" @submit.prevent="start">
      <input v-model="source" placeholder="git 仓库地址或本地路径" />
      <input v-model="target" class="target" />
      <button :disabled="running">{{ running ? '跑着呢…' : '开始' }}</button>
      <button v-if="running" type="button" @click="stop">断开</button>
    </form>

    <p v-if="meta" class="meta">
      {{ meta.project }} → {{ meta.target }} · 模型 {{ meta.model }} · 知识库{{ meta.use_kb ? '开' : '关' }} ·
      步数上限 {{ meta.max_steps }} · run #{{ meta.run_id }}
      <span v-if="items.length">· 工具耗时合计 {{ (elapsed / 1000).toFixed(1) }}s</span>
    </p>

    <p v-if="error" class="err">{{ error }}</p>

    <div v-for="(it, idx) in items" :key="idx" class="row">
      <span class="step">{{ it.step }}</span>

      <div v-if="it.kind === 'message'" class="say">{{ it.content }}</div>

      <div v-else class="tool">
        <div class="head" @click="open[idx] = !open[idx]">
          <b>{{ it.tool }}</b>
          <span class="args mono">{{ short(it.args) }}</span>
          <span v-if="!it.done" class="badge wait">跑着</span>
          <span v-else-if="it.errors && it.errors.length" class="badge bad">{{ it.errors.join(' ') }}</span>
          <span v-else-if="!it.ok" class="badge bad">工具失败</span>
          <span v-else class="badge ok">ok</span>
          <span v-if="it.done" class="ms">{{ it.ms }}ms</span>
        </div>
        <pre v-if="it.details && it.details.length && !open[idx]" class="detail">{{ it.details.join('\n') }}</pre>
        <pre v-if="open[idx]" class="out">{{ it.result }}</pre>
      </div>
    </div>

    <p v-if="verifying" class="meta">独立跑一次 cmake --build 验证结果…</p>

    <div v-if="result" class="result" :class="result.passed ? 'pass' : 'fail'">
      <h3>{{ result.passed ? '编译通过' : '没通过' }}</h3>
      <p>
        {{ result.project }} · 状态 {{ result.status }} · {{ result.steps }} 步
        <span v-if="Object.keys(result.errors).length">
          · 报错类型 {{ Object.entries(result.errors).map(([k, v]) => `${k}×${v}`).join('  ') }}
        </span>
      </p>
      <pre v-if="!result.passed" class="out">{{ result.log }}</pre>
    </div>
</div>
</template>

<style scoped>
.bar { display: flex; gap: 8px; margin-bottom: 14px; }
.bar input:first-child { flex: 1; }
.target { width: 200px; }

.meta { color: var(--dim); font-size: 12.5px; margin: 0 0 14px; }
.err { color: var(--bad); }

.row { display: flex; gap: 10px; padding: 5px 0; border-top: 1px solid var(--line); }
.step { color: var(--dim); width: 22px; text-align: right; flex: none; font-size: 12px; padding-top: 2px; }

.say { color: var(--accent); white-space: pre-wrap; }

.tool { flex: 1; min-width: 0; }
.head { display: flex; gap: 10px; align-items: center; cursor: pointer; }
.args { color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }

.badge { font-size: 11px; padding: 1px 7px; border-radius: 10px; flex: none; }
.badge.ok { color: var(--ok); background: #1e2b22; }
.badge.bad { color: var(--bad); background: #2e1f20; }
.badge.wait { color: var(--warn); background: #2c2517; }
.ms { color: var(--dim); font-size: 11px; flex: none; }

.detail { color: var(--bad); font-size: 12px; margin-top: 3px; opacity: .85; }
.out {
  margin-top: 6px; padding: 8px 10px; background: #101216;
  border: 1px solid var(--line); border-radius: 6px;
  max-height: 320px; overflow: auto; font-size: 12.5px;
}

.result { margin-top: 20px; padding: 12px 16px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); }
.result h3 { margin: 0 0 4px; font-size: 15px; }
.result.pass h3 { color: var(--ok); }
.result.fail h3 { color: var(--bad); }
.result p { margin: 0; color: var(--dim); font-size: 12.5px; }
</style>
