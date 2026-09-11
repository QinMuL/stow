<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, timeAgo } from '../api'

const pipe = ref(null)
const busy = ref('')          // 正在触发的段 key
const expanded = ref({})      // 各段展开状态
const err = ref('')
let timer = null

async function load() {
  try {
    // 健康判据也在这个载荷里(pipeline.health),不再单独拉 /api/status
    pipe.value = await api('pipeline')
    err.value = ''
  } catch (e) {
    err.value = e.message
  }
}

// 系统健康:判据**全在后端**(app/webapp.py 的 _health_block),这里只负责渲染。
// 这样分级阈值(磁盘 80/90、段失败 ≥5 等)能被 pytest 覆盖,前端不必再各写一套判据。
const health = computed(() => pipe.value?.health || null)
const healthRows = computed(() => health.value?.rows || [])
const showHealth = ref(false)

const numbers = computed(() => pipe.value?.numbers || {})
const disk = computed(() => numbers.value.disk || {})
// 磁盘卡:与 CPU/内存 同风格 —— 大号是"整盘已用率"(同一套阈值着色),
// 小字一行给剩余与媒体目录占用;口径见 title(宿主机的盘,不是 115 容量)
const diskHint = computed(() => {
  const d = disk.value
  if (!d.total_bytes) return `媒体目录 ${gb(d.used_bytes)}`
  return `剩 ${gb(d.free_bytes)} · 媒体 ${gb(d.used_bytes)}`
})
// 本机实时资源(CPU / 内存 / 网络):来自 /api/pipeline 的 sys;磁盘仍在 numbers.disk。
// 口径:容器里读到的是承载它的那台机器(WSL 虚拟机),不是 Windows 宿主机。
const sys = computed(() => pipe.value?.sys || {})
const diskUsedPercent = computed(() => disk.value.used_percent ?? null)

// 数值着色:≥90% 红、≥80% 琥珀(与健康判据同一套阈值)
function tone(p) {
  if (p == null) return {}
  return { color: p >= 90 ? 'var(--bad)' : p >= 80 ? 'var(--amber)' : 'var(--text)' }
}

function pct(v) {
  return v == null ? '—' : `${Number(v).toFixed(v >= 100 ? 0 : 1)}%`
}

function rateText(bps) {
  if (bps == null) return '—'
  const v = Number(bps)
  if (v < 1024) return `${v.toFixed(0)} B/s`
  if (v < 1048576) return `${(v / 1024).toFixed(0)} KB/s`
  if (v < 1073741824) return `${(v / 1048576).toFixed(1)} MB/s`
  return `${(v / 1073741824).toFixed(2)} GB/s`
}

// 网络卡大号取"总吞吐",小字给上下行拆分(两者对这条链路都重要:先下载再上传)
const netBps = computed(() => {
  const s = sys.value
  if (s.net_rx_bps == null && s.net_tx_bps == null) return null
  return (s.net_rx_bps || 0) + (s.net_tx_bps || 0)
})
const cpuHint = computed(() => {
  const s = sys.value
  const cores = s.cpu_count ? `${s.cpu_count} 核` : ''
  const load = s.load1 == null ? '' : `负载 ${Number(s.load1).toFixed(2)}`
  return [cores, load].filter(Boolean).join(' · ') || '—'
})
const memHint = computed(() => {
  const s = sys.value
  if (s.mem_total_bytes == null) return '—'
  return `${gb(s.mem_used_bytes)} / ${gb(s.mem_total_bytes)}`
})
const netHint = computed(() => {
  const s = sys.value
  if (s.net_rx_bps == null && s.net_tx_bps == null) return '—'
  return `↓ ${rateText(s.net_rx_bps || 0)} · ↑ ${rateText(s.net_tx_bps || 0)}`
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
    return `在途 ${s.inflight}/${s.limit} · 今日完成 ${s.today} · 失败 ${s.failed}`
  }
  if (s.key === 'process') {
    return `排队 ${s.queued} · 今日完成 ${s.today} · 待人工 ${s.manual}`
  }
  if (s.enabled === false) return '未启用 · 未配置上传目标目录'
  return `在途 ${s.inflight}/${s.limit} · 今日完成 ${s.today} · 失败 ${s.failed}`
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

// D 方案:手写迷你**曲线**图(无依赖)——两条序列合成一张图,共用一把 0 起刻度。
// 坐标系固定(700×110),靠 preserveAspectRatio="none" 横向拉伸铺满容器宽度;
// 曲线用 vector-effect="non-scaling-stroke" 保证线宽不被拉伸变形——代价是**不能画圆点**
// (圆会被拉成椭圆),所以每日数值改由 SVG 热区的 <title> 悬停给出。
const CHART = { w: 700, h: 110, padX: 6, top: 8, base: 102 }

// 平滑曲线:Catmull-Rom → 三次贝塞尔(手写,不引图表库)。
// tension 取 0.6:比标准 Catmull-Rom(1.0)更贴数据、不会在折点处过冲;
// 控制点的 y 再做一次钳制,防止曲线冲出绘图区(跑到基线以下)。
function smoothPath(series, max, tension = 0.6) {
  const list = series.slice(0, 7)
  const n = list.length
  if (!n) return ''
  const step = n > 1 ? (CHART.w - CHART.padX * 2) / (n - 1) : 0
  const span = CHART.base - CHART.top
  const pts = list.map((v, i) => ({
    x: CHART.padX + i * step,
    y: CHART.base - (max > 0 ? (v / max) * span : 0),
  }))
  const at = (i) => ({ x: (pts[i] ?? pts[pts.length - 1]).x, y: (pts[i] ?? pts[pts.length - 1]).y })
  const clampY = (y) => Math.min(CHART.base, Math.max(CHART.top, y))
  const f = (v) => v.toFixed(1)
  let d = `M ${f(pts[0].x)} ${f(pts[0].y)}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = at(i - 1)
    const p1 = at(i)
    const p2 = at(i + 1)
    const p3 = at(i + 2)
    d += ` C ${f(p1.x + ((p2.x - p0.x) / 6) * tension)} ${f(clampY(p1.y + ((p2.y - p0.y) / 6) * tension))}`
      + `, ${f(p2.x - ((p3.x - p1.x) / 6) * tension)} ${f(clampY(p2.y - ((p3.y - p1.y) / 6) * tension))}`
      + `, ${f(p2.x)} ${f(p2.y)}`
  }
  return d
}

const days = computed(() => (pipe.value?.trend?.labels || []).map(l => String(l).slice(5)))

// 三条序列(推卡条数 / 搬运 GB / 上传条数)合成一张曲线图,共用一把 0 起刻度。
// 曲线不像柱状那样各自归一,所以不会出现"三条都顶到天花板"的假象。
const SERIES = [
  { key: 'pushed', cls: 'a', label: '推卡', unit: '条' },
  { key: 'moved_gb', cls: 'b', label: '搬运', unit: 'GB' },
  { key: 'uploaded', cls: 'c', label: '上传', unit: '个' },
]

const series = computed(() => {
  const trend = pipe.value?.trend || {}
  const rows = SERIES.map(s => {
    const values = (trend[s.key] || []).slice(0, 7)
    const texts = values.map(v => (s.key === 'moved_gb' ? fmtMoved(v) : String(v ?? 0)))
    return { ...s, values, texts, last: texts.at(-1) ?? '0' }
  })
  const max = Math.max(1, ...rows.flatMap(r => r.values))
  return rows.map(r => ({ ...r, path: smoothPath(r.values, max) }))
})

// 每日热区:整列可悬停,<title> 一次给出当天三条序列的值
const hitRects = computed(() => {
  const n = days.value.length
  if (!n) return []
  const step = n > 1 ? (CHART.w - CHART.padX * 2) / (n - 1) : 0
  const half = n > 1 ? step / 2 : CHART.w / 2
  return days.value.map((day, i) => {
    const x = Math.max(0, CHART.padX + i * step - half)
    return {
      day,
      x,
      w: Math.min(CHART.w - x, half * 2),
      title: [day, ...series.value.map(s => `${s.label} ${s.texts[i] ?? 0} ${s.unit}`)].join(' · '),
    }
  })
})

// moved_gb 后端已 round 到 2 位小数,原样显示即可(别四舍五入,否则 53.87 会变成 53.9)
function fmtMoved(v) {
  return String(Math.round((v || 0) * 100) / 100)
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 10000)   // 与日志页一致:10s 自动刷新
})
onUnmounted(() => timer && window.clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div class="page-title">系统总览</div>
      <div class="page-sub">三段链运行状态与产出;每 10 秒自动刷新</div>
    </div>

    <div v-if="err" class="msg err" style="margin-bottom:14px">{{ err }}</div>

    <!-- B:页首一行系统健康;点开看按段分组的明细(判据在后端 _health_block)。
         载荷里没有 health 就整条不渲染——旧后端不返回这个键,不该因此挂一条红的"检测中" -->
    <div v-if="health" class="ov-bar" :class="health.level" :title="health.detail"
      @click="showHealth = !showHealth">
      <span class="dot" :class="health.level"></span>
      <span class="ov-bar-text">{{ health.text }}</span>
      <span class="ov-bar-more">{{ showHealth ? '收起' : '明细' }}</span>
    </div>

    <div v-if="showHealth && healthRows.length" class="card ov-health">
      <h3>健康明细</h3>
      <div v-for="r in healthRows" :key="r.key" class="health-row">
        <span class="dot" :class="r.level === 'off' ? '' : r.level"></span>
        <span class="health-name">{{ r.name }}</span>
        <span class="health-text">{{ r.text }}</span>
      </div>
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
      <div class="desc">获取 → 处理 → 上传;点任一段展开在途明细,⟳ 单独跑这段</div>
      <div class="ov-segs">
        <div v-for="(s, idx) in (pipe?.segments || [])" :key="s.key" class="ov-seg"
             @click="expanded[s.key] = !expanded[s.key]">
          <div class="ov-seg-head">
            <span class="dot" :class="segDot(s)"></span>
            <span class="ov-seg-idx">{{ idx + 1 }}</span>
            <span class="ov-seg-name">{{ s.name }}</span>
            <button class="ov-run" :disabled="busy === s.key" title="立即跑这一段"
              @click.stop="run(s.key)">{{ busy === s.key ? '…' : '⟳' }}</button>
          </div>
          <div class="ov-seg-path">{{ s.from }} <span class="ov-arrow">→</span> {{ s.to }}</div>
          <div class="ov-seg-sum">{{ segSummary(s) }}</div>
          <div class="ov-seg-last">
            <template v-if="s.last_activity">上次活动 {{ timeAgo(s.last_activity) }}</template>
            <template v-else>暂无活动</template>
            <span v-if="s.items.length" class="ov-caret">{{ expanded[s.key] ? '▴' : '▾ 展开 ' + s.items.length + ' 个' }}</span>
          </div>
          <div v-if="expanded[s.key] && s.items.length" class="ov-items">
            <div v-for="it in s.items" :key="it.name" class="ov-item">
              <span class="ov-item-name" :title="it.name">{{ it.name }}</span>
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

    <!-- D:本机实时资源四卡(CPU / 内存 / 网络 / 硬盘)。前三项来自 /api/pipeline 的 sys,
         磁盘来自 numbers.disk;CPU% 与网络速率是两次采样的差值,首次为 — -->
    <div class="stat-row">
      <div class="stat">
        <div class="num" :style="tone(sys.cpu_percent)">{{ pct(sys.cpu_percent) }}</div>
        <div class="lbl">CPU</div>
        <div class="hint">{{ cpuHint }}</div>
      </div>
      <div class="stat">
        <div class="num" :style="tone(sys.mem_percent)">{{ pct(sys.mem_percent) }}</div>
        <div class="lbl">内存</div>
        <div class="hint">{{ memHint }}</div>
      </div>
      <div class="stat">
        <div class="num">{{ rateText(netBps) }}</div>
        <div class="lbl">网络</div>
        <div class="hint">{{ netHint }}</div>
      </div>
      <div class="stat" title="承载 media 目录的是宿主机的盘(compose 里 ./media:/app/media);显示的是整盘已用率,不是 115 的容量">
        <div class="num" :style="tone(diskUsedPercent)">{{ pct(diskUsedPercent) }}</div>
        <div class="lbl">硬盘</div>
        <div class="hint">{{ diskHint }}</div>
      </div>
    </div>

    <!-- D:7 日趋势(单图三曲线) -->
    <div class="card">
      <h3>7 日趋势</h3>
      <div class="ov-legend">
        <span v-for="s in series" :key="s.key" class="ov-lg">
          <i class="ov-sw" :class="s.cls"></i>{{ s.label }} <b>{{ s.last }}</b> {{ s.unit }}
        </span>
      </div>
      <div class="ov-chart">
        <svg class="ov-svg" :viewBox="`0 0 ${CHART.w} ${CHART.h}`" preserveAspectRatio="none">
          <line class="ov-base" x1="0" :y1="CHART.base" :x2="CHART.w" :y2="CHART.base"></line>
          <path v-for="s in series" :key="s.key" class="ov-curve" :class="s.cls"
            :d="s.path"></path>
          <rect v-for="r in hitRects" :key="r.day" class="ov-hit"
            :x="r.x" y="0" :width="r.w" :height="CHART.h">
            <title>{{ r.title }}</title>
          </rect>
        </svg>
        <div class="ov-days">
          <span v-for="(d, i) in days" :key="i">{{ d }}</span>
        </div>
      </div>
    </div>

    <!-- 最近推送(压缩版) -->
    <div class="card">
      <h3>最近推送</h3>
      <div v-if="pipe?.recent?.length" class="feed">
        <div v-for="it in pipe.recent" :key="it.code" class="feed-item">
          <span class="dot ok" style="width:7px;height:7px"></span>
          <span class="t" :title="it.title">{{ it.title }}</span>
          <span class="c">{{ it.code }}</span>
          <span class="when">{{ timeAgo(it.pushed_at) }}</span>
        </div>
      </div>
      <div v-else class="empty">还没有推送记录 —— 在 Telegram 给 Bot 发一条 115 分享链接试试</div>
    </div>
  </div>
</template>
