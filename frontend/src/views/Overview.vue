<script setup>
import { onMounted, ref } from 'vue'
import { api, timeAgo } from '../api'

const status = ref(null)
const history = ref(null)

async function load() {
  try {
    ;[status.value, history.value] = await Promise.all([api('status'), api('history?limit=15')])
  } catch { /* 401 已跳转 */ }
}

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
        <div class="num" :style="{ color: status?.bot_running ? 'var(--ok)' : 'var(--bad)' }">
          {{ status?.bot_running ? '在线' : '离线' }}</div>
        <div class="lbl">Bot 状态</div>
      </div>
      <div class="stat">
        <div class="num">{{ history?.today ?? '—' }}</div>
        <div class="lbl">今日推送</div>
      </div>
      <div class="stat">
        <div class="num">{{ history?.total ?? '—' }}</div>
        <div class="lbl">累计推送</div>
      </div>
    </div>

    <div class="card">
      <h3>SERVICE</h3>
      <div class="desc">服务状态{{ status && !status.bot_ready ? ' · 待补配置' : '' }}</div>
      <template v-if="status">
        <div v-if="status.bot_running" style="display:flex;align-items:center;gap:10px;color:var(--sub)">
          <span class="dot ok"></span> 推卡链运行中 · 配置齐全
        </div>
        <template v-else>
          <div style="display:flex;align-items:center;gap:10px;color:var(--sub)">
            <span class="dot bad"></span>
            {{ status.bot_ready ? 'Bot 异常退出:' + status.bot_error : 'Bot 未启动(配置不全)' }}
          </div>
          <ul v-if="status.missing.length" class="missing">
            <li v-for="m in status.missing" :key="m">▸ {{ m }}</li>
          </ul>
          <div style="margin-top:14px">
            <router-link class="btn ghost" to="/push" style="display:inline-block">前往补齐配置 →</router-link>
          </div>
        </template>
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
