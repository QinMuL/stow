<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, timeAgo } from '../api'

const status = ref(null)
const history = ref(null)

async function load() {
  try {
    ;[status.value, history.value] = await Promise.all([api('status'), api('history?limit=15')])
  } catch { /* 401 已跳转 */ }
}

// 链路健康三行:Bot 服务 / 网络代理 / 115 通道(ok=正常,bad=故障,warn=降级可用)
const rows = computed(() => {
  const s = status.value
  if (!s) return []
  const bot = {
    name: 'Bot 服务',
    state: s.bot_running ? 'ok' : 'bad',
    text: s.bot_running ? '在线 · 推卡链运行中' : s.bot_ready ? '异常退出' : '未启动(配置不全)',
    sub: s.bot_running ? '' : s.bot_error || '',
  }
  const p = s.proxy || {}
  const proxy = {
    name: '网络代理',
    state: p.configured ? (p.ok ? 'ok' : 'bad') : p.ok ? 'warn' : 'bad',
    text: p.configured ? (p.ok ? `连通 · ${p.latency_ms}ms` : '不可达') : '未配置 · 直连',
    sub: p.configured ? p.url : p.ok ? 'TG 直连可达' : '直连不可达,' + (p.error || ''),
  }
  const c = s.pan115 || {}
  const pan = {
    name: '115 通道',
    state: c.cookie_set ? (c.ok ? 'ok' : 'bad') : 'warn',
    text: c.cookie_set
      ? c.ok ? `登录有效 · UID ${c.uid}` : 'Cookie 已失效'
      : '匿名模式 · 易触发 405 限流',
    sub: c.cookie_set ? (c.ok ? '走稳定 proapi 通道' : c.error || '') : '建议在推送配置页填入 115 Cookie',
  }
  return [bot, proxy, pan]
})

const healthCount = computed(() => rows.value.filter(r => r.state === 'ok').length)

onMounted(load)
</script>

<template>
  <div>
    <div class="page-head">
      <div class="page-title">总览</div>
      <div class="page-sub">推送链运行状态与最近活动</div>
    </div>

    <div class="stat-row">
      <div class="stat">
        <div class="num">{{ history?.today ?? '—' }}</div>
        <div class="lbl">今日推送</div>
      </div>
      <div class="stat">
        <div class="num">{{ history?.total ?? '—' }}</div>
        <div class="lbl">累计推送</div>
      </div>
      <div class="stat">
        <div class="num" :style="{ color: healthCount === 3 ? 'var(--ok)' : healthCount === 0 ? 'var(--bad)' : 'var(--amber)' }">
          {{ status ? healthCount + '/3' : '—' }}</div>
        <div class="lbl">链路健康</div>
      </div>
    </div>

    <div class="card">
      <h3>LINK HEALTH</h3>
      <div class="desc">推送链健康 · Bot / 代理 / 115 三段实时探测(代理 60s、Cookie 5min 缓存)</div>
      <div v-for="r in rows" :key="r.name" class="health-row">
        <span class="dot" :class="r.state"></span>
        <div class="health-name">{{ r.name }}</div>
        <div class="health-text">
          {{ r.text }}
          <div v-if="r.sub" class="health-sub">{{ r.sub }}</div>
        </div>
      </div>
      <template v-if="status && !status.bot_ready">
        <ul v-if="status.missing.length" class="missing">
          <li v-for="m in status.missing" :key="m">▸ {{ m }}</li>
        </ul>
        <div style="margin-top:14px">
          <router-link class="btn ghost" to="/push" style="display:inline-block">前往补齐配置 →</router-link>
        </div>
      </template>
    </div>

    <div class="card">
      <h3>RECENT</h3>
      <div class="desc">最近推送</div>
      <div v-if="history?.items?.length" class="feed">
        <div v-for="it in history.items" :key="it.code" class="feed-item">
          <span class="dot ok" style="width:7px;height:7px"></span>
          <span class="t">{{ it.title }}</span>
          <span class="c">{{ it.code }}</span>
          <span class="when">{{ timeAgo(it.pushed_at) }}</span>
        </div>
      </div>
      <div v-else class="empty">还没有推送记录 —— 在 Telegram 给 Bot 发一条 115 分享链接试试</div>
    </div>
  </div>
</template>
