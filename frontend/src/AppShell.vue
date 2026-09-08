<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, clearToken } from './api'

const route = useRoute()
const router = useRouter()
const status = ref(null)
const version = ref('v0.1')

const nav = [
  { to: '/overview', ic: '◉', label: '总览' },
  { to: '/push', ic: '✦', label: '推送配置' },
  { to: '/system', ic: '⚙', label: '系统设置' },
]

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
        <router-link class="nav-item" to="/logs"><span class="ic">≡</span><span class="txt">日志</span><span class="soon">soon</span></router-link>
        -->
      </nav>
      <div class="side-foot">{{ version }} · amber</div>
    </aside>

    <div class="main">
      <div class="topbar">
        <div class="top-status">
          <span class="dot" :class="status?.bot_running ? 'ok' : 'bad'"></span>
          {{ status?.bot_running ? 'Bot 运行中' : 'Bot 未运行' }}
        </div>
        <div class="top-right">
          <span class="who">👤 <b>admin</b></span>
          <button class="link-btn" @click="logout">退出</button>
        </div>
      </div>
      <div class="content">
        <div class="page">
          <router-view @refresh-status="refresh" />
        </div>
      </div>
    </div>
  </div>
</template>
