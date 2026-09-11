<script setup>
import { ref } from 'vue'
import BuildRunner from './components/BuildRunner.vue'
import RunHistory from './components/RunHistory.vue'

const tab = ref('run')
</script>

<template>
  <header>
    <h1>CrossBuild Agent</h1>
    <span class="sub">C/C++ 项目交叉编译智能体 · 实时执行与运行回放</span>
    <nav>
      <button :class="{ on: tab === 'run' }" @click="tab = 'run'">跑一次</button>
      <button :class="{ on: tab === 'history' }" @click="tab = 'history'">历史运行</button>
    </nav>
  </header>

  <main>
    <BuildRunner v-show="tab === 'run'" />
    <RunHistory v-if="tab === 'history'" />
  </main>
</template>

<style scoped>
header {
  display: flex;
  align-items: baseline;
  gap: 14px;
  padding: 16px 24px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}

h1 { margin: 0; font-size: 17px; letter-spacing: .3px; }
.sub { color: var(--dim); font-size: 12.5px; }
nav { margin-left: auto; display: flex; gap: 8px; }
nav button.on { border-color: var(--accent); color: var(--accent); }

main { max-width: 1080px; margin: 0 auto; padding: 20px 24px 60px; }
</style>
