<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, timeAgo } from '../api'

const status = ref(null)
const pipe = ref(null)
const busy = ref('')          // 正在触发的段 key
const expanded = ref({})      // 各段展开状态
const err = ref('')
let timer = null

async function load() {
  try {
    ;[status.value, pipe.value] = await Promise.all([api('status'), api('pipeline')])
    err.value = ''
  } catch (e) {
    err.value = e.message
  }
}

// B 方案:顶栏一行说清;聚合状态复用 /api/status(与顶栏同一口径)
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
  if (m && m.configured && s.bot_running && m.state !== 'running') {
    issues.push(`频道监控${m.state_text}`)
  }
  if (issues.length) return { cls: 'bad', text: '系统异常', detail: issues.join(' · ') }
  return warn
    ? { cls: 'warn', text: '系统运行中(降级)', detail: '115 未配置 Cookie,匿名通道易限流' }
    : { cls: 'ok', text: '系统运行中', detail: 'Bot / 代理 / 115 / 频道监控 / 三段链 全部正常' }
})

const numbers = computed(() => pipe.value?.numbers || {})
const disk = computed(() => numbers.value.disk || {})
const diskText = computed(() => {
  const d = disk.value
  if (!d.total_bytes) return `${gb(d.used_bytes)} / 剩余未知`
  return `已用 ${gb(d.used_bytes)} / 盘剩 ${gb(d.free_bytes)}`
})
const diskState = computed(() => {
  const p = disk.value.used_percent || 0
  return p >= 90 ? 'bad' : p >= 80 ? 'warn' : 'ok'
})

function gb(n) {
  if (!n && n !== 0) return '—'
  const v = n / 1024 ** 3
  return v >= 100 ? `${Math.round(v)}GB` : `${v.toFixed(1)}GB`
}

function segDot(s) {
  if (s.key === 'upload' && s.enabled === false) return 'warn'
  if (s.failed) return 'bad'
  if (s.key === 'process' ? s.queued === 0 && !s.items.length : !s.items.length) return 'ok'
  return 'warn'
}

function segSummary(s) {
  if (s.key === 'fetch') {
    return `在途 ${s.inflight}/${s.limit} · 今日 ${s.today} · 失败 ${s.failed}`
  }
  if (s.key === 'process') {
    return `排队 ${s.queued} · 今日 ${s.today} · 待人工 ${s.manual}`
  }
  if (s.enabled === false) return '未启用(未配置上传目标目录)'
  return `在途 ${s.inflight}/${s.limit} · 今日 ${s.today} · 失败 ${s.failed}`
}

async function run(seg) {
  busy.value = seg
  try {
    const d = await api('pipeline/run', { segment: seg }, 'POST')
    await load()
    window.setTimeout(load, 3000)      // 触发后进度稍后再刷一次
    err.value = ''
    void d
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = ''
  }
}

// D 方案:手写迷你柱状(无依赖)——按 7 日最大值归一
function bars(series) {
  const max = Math.max(1, ...series)
  return series.map(v => ({ v, h: Math.max(2, Math.round((v / max) * 34)) }))
}
const pushedBars = computed(() => bars(pipe.value?.trend?.pushed || []))
const movedBars = computed(() => bars(pipe.value?.trend?.moved_gb || []))

onMounted(() => {
  load()
  timer = window.setInterval(load, 10000)   // 与日志页一致:10s 自动刷新
})
onUnmounted(() => timer && window.clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div class="page-title">总览</div>
      <div class="page-sub">三段链运行状态与产出;每 10 秒自动刷新</div>
    </div>

    <div v-if="err" class="msg err" style="margin-bottom:14px">{{ err }}</div>

    <!-- B:顶栏一行;异常时展开待处理 -->
    <div class="ov-bar" :class="sysState.cls" :title="sysState.detail">
      <span class="dot" :class="sysState.cls"></span>
      <span class="ov-bar-text">{{ sysState.text }}</span>
      <span v-if="pipe" class="ov-bar-nums">
        今日:推卡 {{ numbers.pushed_today ?? 0 }} · 获取 {{ numbers.moved_gb_today ?? 0 }}GB
        · 上传 {{ numbers.uploaded_today ?? 0 }}
      </span>
    </div>

    <!-- 待人工处理:仅非空时出现 -->
    <div v-if="pipe?.attention?.length" class="card ov-attn">
      <h3>需要你处理 ({{ pipe.attention.length }})</h3>
      <div v-for="(a, i) in pipe.attention" :key="i" class="ov-attn-row">
        <span class="dot bad"></span>
        <span class="ov-attn-kind">{{ a.kind }}</span>
        <span class="ov-attn-text">{{ a.text }}</span>
        <span class="ov-attn-reason">{{ a.reason }}</span>
        <router-link class="ov-link" :to="{ path: '/logs', query: { q: a.text.slice(0, 24) } }">
          去日志
        </router-link>
      </div>
    </div>

    <!-- A:三段主轴 -->
    <div class="card">
      <h3>流水线</h3>
      <div class="desc">获取 → 处理 → 上传;点卡片展开在途明细,右上 ⟳ 立即跑一段</div>
      <div class="ov-segs">
        <div v-for="(s, idx) in (pipe?.segments || [])" :key="s.key" class="ov-seg"
             @click="expanded[s.key] = !expanded[s.key]">
          <div class="ov-seg-head">
            <span class="dot" :class="segDot(s)"></span>
            <span class="ov-seg-idx">{{ idx + 1 }}</span>
            <span class="ov-seg-name">{{ s.name }}</span>
            <button class="ov-run" :disabled="busy === s.key" title="立即跑一段"
              @click.stop="run(s.key)">{{ busy === s.key ? '…' : '⟳' }}</button>
          </div>
          <div class="ov-seg-path">{{ s.from }} <span class="ov-arrow">→</span> {{ s.to }}</div>
          <div class="ov-seg-sum">{{ segSummary(s) }}</div>
          <div class="ov-seg-last">
            <template v-if="s.last_activity">{{ timeAgo(s.last_activity) }}有活动</template>
            <template v-else>暂无活动</template>
            <span v-if="s.items.length" class="ov-caret">{{ expanded[s.key] ? '▴' : '▾ 展开 ' + s.items.length + ' 个' }}</span>
          </div>
          <div v-if="expanded[s.key] && s.items.length" class="ov-items">
            <div v-for="it in s.items" :key="it.name" class="ov-item">
              <span class="ov-item-name">{{ it.name }}</span>
              <span class="ov-item-note">{{ it.note }}</span>
              <span class="ov-bar-mini">
                <i :style="{ width: (it.progress || 0) + '%' }"></i>
              </span>
              <span class="ov-item-pct">{{ (it.progress || 0).toFixed(0) }}%</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- D:关键数字 -->
    <div class="stat-row">
      <div class="stat">
        <div class="num">{{ numbers.pushed_today ?? '—' }}</div>
        <div class="lbl">今日推卡</div>
      </div>
      <div class="stat">
        <div class="num">{{ numbers.moved_gb_today ?? '—' }}<span class="unit">GB</span></div>
        <div class="lbl">今日搬运</div>
      </div>
      <div class="stat">
        <div class="num">{{ numbers.uploaded_today ?? '—' }}</div>
        <div class="lbl">今日上传</div>
      </div>
      <div class="stat">
        <div class="num" :style="{ color: diskState === 'bad' ? 'var(--bad)' : diskState === 'warn' ? 'var(--amber)' : 'inherit', fontSize: '17px' }">
          {{ diskText }}
        </div>
        <div class="lbl">磁盘 · 媒体目录 / 所在盘(已用 {{ disk.used_percent ?? '—' }}%)</div>
      </div>
    </div>

    <!-- D:7 日双趋势 -->
    <div class="card">
      <h3>7 日趋势</h3>
      <div class="ov-trend-row">
        <div class="ov-trend">
          <div class="ov-trend-title">推卡(条)</div>
          <div class="ov-bars">
            <div v-for="(b, i) in pushedBars" :key="i" class="ov-bar-col">
              <i :style="{ height: b.h + 'px' }" :title="String(b.v)"></i>
              <span>{{ pipe?.trend?.labels?.[i]?.slice(5) || '' }}</span>
            </div>
          </div>
        </div>
        <div class="ov-trend">
          <div class="ov-trend-title">搬运(GB)</div>
          <div class="ov-bars">
            <div v-for="(b, i) in movedBars" :key="i" class="ov-bar-col">
              <i :style="{ height: b.h + 'px' }" :title="String(b.v)"></i>
              <span>{{ pipe?.trend?.labels?.[i]?.slice(5) || '' }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 最近推送(压缩版) -->
    <div class="card">
      <h3>最近推送</h3>
      <div v-if="pipe?.recent?.length" class="feed">
        <div v-for="it in pipe.recent" :key="it.code" class="feed-item">
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
