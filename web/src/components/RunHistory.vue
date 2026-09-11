<script setup>
import { ref, onMounted } from 'vue'

const runs = ref([])
const detail = ref(null)
const loading = ref(false)
const open = ref({})

onMounted(async () => {
  runs.value = await (await fetch('/api/runs?limit=50')).json()
})

async function show(id) {
  loading.value = true
  open.value = {}
  detail.value = await (await fetch(`/api/runs/${id}`)).json()
  loading.value = false
}

function secs(r) {
  return r.finished_at ? `${(r.finished_at - r.started_at).toFixed(0)}s` : '—'
}

function when(t) {
  return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false })
}

function tokens(r) {
  const n = (r.prompt_tokens || 0) + (r.completion_tokens || 0)
  return n > 1000 ? `${(n / 1000).toFixed(1)}k` : n
}

function cls(status) {
  if (status === 'success' || status === 'done') return 'ok'
  if (!status) return 'wait'
  return 'bad'
}
</script>

<template>
  <div class="wrap">
    <div class="scroll">
    <table>
      <thead>
        <tr><th>#</th><th>任务</th><th>状态</th><th>步数</th><th>token</th><th>耗时</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in runs" :key="r.id" :class="{ on: detail && detail.run.id === r.id }" @click="show(r.id)">
          <td class="dim">{{ r.id }}</td>
          <td class="task">{{ r.task }}</td>
          <td><span class="badge" :class="cls(r.status)">{{ r.status || '未结束' }}</span></td>
          <td>{{ r.steps }}</td>
          <td class="dim">{{ tokens(r) }}</td>
          <td class="dim">{{ secs(r) }}</td>
          <td class="dim">{{ when(r.started_at) }}</td>
        </tr>
      </tbody>
    </table>
    </div>

    <section v-if="loading" class="dim">读取中…</section>

    <section v-else-if="detail" class="trace">
      <h3>run #{{ detail.run.id }} · {{ detail.run.task }}</h3>
      <p class="dim">{{ detail.calls.length }} 次工具调用 · 模型 {{ detail.run.model }}</p>

      <div v-for="(c, i) in detail.calls" :key="i" class="row">
        <span class="step">{{ c.step }}</span>
        <div class="tool">
          <div class="head" @click="open[i] = !open[i]">
            <b>{{ c.tool }}</b>
            <span class="args mono">{{ c.args }}</span>
            <span class="badge" :class="c.ok ? 'ok' : 'bad'">{{ c.ok ? 'ok' : '失败' }}</span>
            <span class="dim ms">{{ Math.round(c.duration_ms) }}ms</span>
          </div>
          <pre v-if="open[i]" class="out">{{ c.result }}</pre>
        </div>
      </div>

      <h4>最终输出</h4>
      <pre class="out">{{ detail.run.result || '（没有记录）' }}</pre>
    </section>
  </div>
</template>

<style scoped>
.scroll { overflow-x: auto; }
table { width: 100%; min-width: 620px; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--dim); font-weight: 500; font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
td { padding: 6px 8px; border-bottom: 1px solid var(--line); }
tbody tr { cursor: pointer; }
tbody tr:hover { background: var(--panel); }
tbody tr.on { background: #222833; }
.task { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dim { color: var(--dim); }

.badge { font-size: 11px; padding: 1px 7px; border-radius: 10px; }
.badge.ok { color: var(--ok); background: #1e2b22; }
.badge.bad { color: var(--bad); background: #2e1f20; }
.badge.wait { color: var(--warn); background: #2c2517; }

.trace { margin-top: 24px; }
.trace h3 { margin: 0 0 2px; font-size: 15px; }
.trace h4 { margin: 18px 0 6px; font-size: 13px; color: var(--dim); font-weight: 500; }
.trace p { margin: 0 0 10px; font-size: 12.5px; }

.row { display: flex; gap: 10px; padding: 5px 0; border-top: 1px solid var(--line); }
.step { color: var(--dim); width: 22px; text-align: right; flex: none; font-size: 12px; padding-top: 2px; }
.tool { flex: 1; min-width: 0; }
.head { display: flex; gap: 10px; align-items: center; cursor: pointer; }
.args { color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.ms { font-size: 11px; flex: none; }
.out {
  margin-top: 6px; padding: 8px 10px; background: #101216;
  border: 1px solid var(--line); border-radius: 6px;
  max-height: 320px; overflow: auto; font-size: 12.5px;
}
</style>
