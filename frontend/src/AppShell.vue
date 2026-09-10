<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, clearToken } from './api'
import { pageBar } from './pagebar'
import AccountModal from './views/AccountModal.vue'

const route = useRoute()
const router = useRouter()
const status = ref(null)
const version = ref('v0.1')
const showAccount = ref(false)

const nav = [
  { to: '/overview', ic: '◉', label: '总览' },
  { to: '/push', ic: '✦', label: '全局配置' },
  { to: '/logs', ic: '≡', label: '日志' },
]

// 整体系统状态:聚合 Bot 进程 / 代理连通 / 115 通道 / 频道监控四段健康,一眼看出有无异常
const sysState = computed(() => {
  const s = status.value
  if (!s) return { cls: 'bad', text: '检测中…', detail: '' }
  const issues = []
  let warn = false
  if (!s.bot_running) issues.push('Bot 未运行')
  const p = s.proxy || {}
  if (!p.ok) issues.push(p.configured ? '代理不可达' : '代理未配置')
  const c = s.pan115 || {}
  if (c.cookie_set && !c.ok) issues.push('115 Cookie 失效')
  else if (!c.cookie_set) warn = true
  const m = s.monitor
  if (m && m.configured && s.bot_running) {
    if (m.state === 'no-login') issues.push('频道监控未登录')
    else if (m.state !== 'running' || !m.connected) issues.push(`频道监控${m.state_text}`)
  }
  if (issues.length) {
    return { cls: 'bad', text: '系统异常', detail: issues.join(' · ') }
  }
  return warn
    ? { cls: 'warn', text: '系统运行中(降级)', detail: '115 未配置 Cookie,匿名通道易限流' }
    : { cls: 'ok', text: '系统运行中', detail: 'Bot / 代理 / 115 / 频道监控 全部正常' }
})

async function refresh() {
  try {
    status.value = await api('status')
  } catch { /* 401 已自动跳转 */ }
}

function logout() {
  clearToken()
  router.push('/login')
}

onMounted(refresh)
</script>

<template>
  <div class="shell">
    <aside class="side">
      <div class="logo">
        <div class="logo-mark">S</div>
        <div>
          <div class="logo-name">STOW</div>
          <div class="logo-sub">media manager</div>
        </div>
      </div>
      <nav class="nav">
        <router-link v-for="n in nav" :key="n.to" class="nav-item"
          :class="{ active: route.path.startsWith(n.to) }" :to="n.to">
          <span class="ic">{{ n.ic }}</span><span class="txt">{{ n.label }}</span>
        </router-link>
        <!-- 预留位:功能到,页面到
        <router-link class="nav-item" to="/transfer"><span class="ic">⇅</span><span class="txt">自动转存</span><span class="soon">soon</span></router-link>
        -->
      </nav>
      <div class="side-foot">{{ version }} · amber</div>
    </aside>

    <div class="main">
      <div class="topbar">
        <div class="top-status" :title="sysState.detail">
          <span class="dot" :class="sysState.cls"></span>
          {{ sysState.text }}
        </div>
        <div class="top-right">
          <button class="link-btn who-btn" title="账号与服务" @click="showAccount = true">👤 <b>admin</b></button>
          <button class="link-btn" @click="logout">退出</button>
        </div>
      </div>
      <!-- 页面级操作条:在滚动区之外,出现/消失都不会遮挡内容(如"保存并重启") -->
      <div v-if="pageBar" class="page-bar">
        <span class="pb-dot"></span>
        <span class="pb-text">{{ pageBar.text }}</span>
        <span v-if="pageBar.hint" class="pb-hint">{{ pageBar.hint }}</span>
        <button class="btn primary" :disabled="pageBar.busy" @click="pageBar.action()">
          {{ pageBar.busy ? '处理中…' : pageBar.actionText }}
        </button>
      </div>
      <div class="content">
        <div class="page">
          <router-view @refresh-status="refresh" />
        </div>
      </div>
    </div>

    <AccountModal v-if="showAccount" @close="showAccount = false" />
  </div>
</template>
