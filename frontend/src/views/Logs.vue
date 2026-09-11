<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'

const route = useRoute()
const files = ref([])
const current = ref('stow.log')
const level = ref('INFO') // 默认 INFO+,DEBUG 噪音需手动放开
const q = ref('')
const items = ref([])
const truncated = ref(false)
const loading = ref(false)
const auto = ref(true)
let timer = null

const LEVELS = [
  { v: '', label: '全部' },
  { v: 'INFO', label: 'INFO+' },
  { v: 'WARNING', label: 'WARN+' },
  { v: 'ERROR', label: 'ERROR' },
]

async function load() {
  loading.value = true
  try {
    const p = new URLSearchParams({ file: current.value, limit: 500, level: level.value, q: q.value })
    const d = await api('logs?' + p)
    files.value = d.files
    items.value = [...d.items].reverse() // 新 → 旧
    truncated.value = d.truncated
  } catch { /* 401 已跳转 */ } finally {
    loading.value = false
  }
}

function schedule() {
  clearInterval(timer)
  if (auto.value) timer = setInterval(load, 10000)
}

function setLevel(v) {
  level.value = v
  load()
}

function fmtSize(n) {
  return n >= 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + 'MB' : Math.max(1, Math.round(n / 1024)) + 'KB'
}

function cls(l) {
  if (l === 'ERROR' || l === 'CRITICAL') return 'lv-error'
  if (l === 'WARNING') return 'lv-warn'
  return l === 'DEBUG' ? 'lv-debug' : 'lv-info'
}

onMounted(() => {
  if (route.query.q) q.value = String(route.query.q)   // 总览「去日志」带关键字跳转
  load()
  schedule()
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div class="page-title">日志</div>
      <div class="page-sub">运行日志 · 尾部 500 条,10 秒自动刷新</div>
    </div>

    <div class="card">
      <div class="log-toolbar">
        <select v-model="current" class="log-file" @change="load">
          <option v-for="f in files" :key="f.name" :value="f.name">
            {{ f.name === 'stow.log' ? '当前日志' : '历史 ' + f.name }} · {{ fmtSize(f.size) }}
          </option>
        </select>
        <div class="chips">
          <button v-for="l in LEVELS" :key="l.v" class="chip" :class="{ on: level === l.v }"
            @click="setLevel(l.v)">{{ l.label }}</button>
        </div>
        <input v-model="q" class="log-search" placeholder="搜索关键字,回车执行" @keyup.enter="load">
        <label class="auto-toggle">
          <input type="checkbox" v-model="auto" @change="schedule"> 自动刷新
        </label>
      </div>

      <div class="log-meta">
        共 {{ items.length }} 条{{ truncated ? '(超出只显示尾部 500,可用级别/关键字缩小范围)' : '' }}
        <button class="btn ghost" style="padding:3px 10px;font-size:12px" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}</button>
      </div>

      <div v-if="items.length" class="log-list">
        <div v-for="(it, i) in items" :key="i" class="log-row">
          <span class="log-ts">{{ it.ts.slice(6) }}</span>
          <span class="log-lv" :class="cls(it.level)">{{ it.level.slice(0, 5) }}</span>
          <span class="log-logger">{{ it.logger }}</span>
          <span class="log-msg">{{ it.msg }}</span>
        </div>
      </div>
      <div v-else class="empty">{{ loading ? '加载中…' : '没有匹配的日志' }}</div>
    </div>
  </div>
</template>
