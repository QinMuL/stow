<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { CONFIG_GROUPS, DEFAULT_CONFIG_GROUP, configGroup } from '../configGroups'
import { clearPageBar, setPageBar } from '../pagebar'
import DirPickerModal from './DirPickerModal.vue'
import MonitorLoginModal from './MonitorLoginModal.vue'

const emit = defineEmits(['refresh-status'])
const route = useRoute()
const router = useRouter()

const cfg = ref(null)
const msg = ref({ text: '', kind: '' })
const busy = ref(false)

// ── 分组页(2026-09-12 用户要求:侧栏下拉 → 点分组只显示该组) ──────────
// 一条路由 /push/:group + 同一个组件实例:切分组时组件不重建,
// 所以表单里**未保存的改动不会丢**(独立页面组件就会丢,撞"切换不得作废状态"那条规矩)。
const GROUP_KEYS = CONFIG_GROUPS.map((g) => g.key)
const activeGroup = computed(() => (
  GROUP_KEYS.includes(String(route.params.group)) ? String(route.params.group) : DEFAULT_CONFIG_GROUP
))
const activeMeta = computed(() => configGroup(activeGroup.value))
// 非法分组(手改地址栏)拉回默认,免得地址栏与内容对不上
watch(() => route.params.group, (g) => {
  if (g && !GROUP_KEYS.includes(String(g))) router.replace(`/push/${DEFAULT_CONFIG_GROUP}`)
}, { immediate: true })
// 只显示本组的区块(注意:formValues() 仍遍历全部 GROUPS,不能按分组过滤,否则漏保存)
const visibleGroups = computed(() => GROUPS.filter((g) => g.group === activeGroup.value))

// 五组卡片:每组独立保存(实际提交全部字段,后端全量校验)
// 每个区块声明 key(用于"未保存"圆点按区比对)与 group(归到哪个分组页)。
// 分组 key/label 定义在 src/configGroups.js(与侧栏下拉共用)。
// 字段的 ph = 输入框占位示例(短),hint = 字段下方的 💡 提示(完整说明)——两者不重复。
const GROUPS = [
  {
    key: 'bot',
    group: 'creds',
    title: 'Telegram Bot 机器人配置',
    desc: 'Bot 凭据与管理员;缺任一项 Bot 都不会启动',
    fields: [
      { key: 'tg_bot_token', label: 'Bot Token', sensitive: true,
        ph: '123456789:AAH…', hint: 'BotFather 发给你的令牌。填错或过期,Bot 直接起不来。' },
      { key: 'tg_admin_ids', label: '管理员用户 ID',
        ph: '123456789, 987654321',
        hint: '能用这个 Bot 的 Telegram 用户 ID,多个用逗号分隔。不知道自己的 ID,把 /start 发给 @userinfobot 问一下。' },
    ],
  },
  {
    key: 'proxy',
    group: 'creds',
    title: '项目代理配置',
    desc: '只有 Telegram 与 TMDB 走代理;115 始终直连',
    fields: [
      { key: 'proxy_url', label: '代理地址(可选)',
        ph: 'http://127.0.0.1:7897',
        hint: '留空即直连。地址要填服务能访问到的那个:与代理同机部署时用 127.0.0.1,填局域网 IP 通常连不通。' },
    ],
  },
  {
    key: 'tmdb',
    group: 'creds',
    title: 'TMDB API Key 配置',
    desc: '卡片元数据的来源',
    fields: [
      { key: 'tmdb_api_key', label: 'TMDB API Key', sensitive: true,
        ph: 'a1b2c3d4e5…',
        hint: 'themoviedb.org 免费申请的 v3 key。留空时卡片没有海报、评分和简介,但链接照常推送。' },
    ],
  },
  {
    key: 'pan115',
    group: 'creds',
    title: '115 Cookie 配置',
    desc: '读分享与转存的身份;没它只能匿名读,容易触限流',
    fields: [
      { key: 'pan115_cookie', label: '115 Cookie(可选)', sensitive: true,
        ph: 'UID=1234567_A1_…',
        hint: '浏览器登录 115 后按 F12,在任一请求里复制整行 Cookie 粘进来。转存、建分享、目录监控都必须有它。' },
    ],
  },
  {
    key: 'save_pipeline',
    group: 'sharing',
    title: '转存流水线',
    desc: '/save <链接> 触发:转存 → 整理 → 建永久分享 → 过审后推卡',
    fields: [
      { key: 'pipeline_root_dir', label: '流水线根目录', picker: true,
        ph: '/我的资源/待整理',
        hint: '转存的落盘根目录,其下会自动派生「待整理 / 已发布 / 违规」三个子目录。填目录 ID(点 📂 选)或路径;填路径且不存在时会自动创建。' },
    ],
  },
]

// 各区块占用哪些字段 —— 只给"未保存"圆点用(按区比对 baseline)。
// 注意 formValues() 仍遍历**全部** GROUPS,不能按分组过滤,否则会漏保存字段。
const SECTION_KEYS = {
  bot: ['tg_bot_token', 'tg_admin_ids'],
  proxy: ['proxy_url'],
  tmdb: ['tmdb_api_key'],
  pan115: ['pan115_cookie'],
  save_pipeline: ['pipeline_root_dir'],
  monitor: ['monitor_dirs'],
  openlist: ['openlist_base_url', 'openlist_token', 'openlist_monitor_dirs',
             'openlist_dest_path', 'openlist_max_tasks', 'fetch_interval_minutes'],
  process: ['process_interval_minutes', 'min_size_mb', 'min_age_seconds', 'clean_enabled'],
  upload: ['cd2_address', 'cd2_token', 'cd2_source_path', 'cd2_dest_path',
           'upload_max_tasks', 'upload_interval_minutes'],
  monitor_tg: ['tg_api_id', 'tg_api_hash', 'monitor_channels'],
}

// 每个分组页显示哪些区块(顺序即展示顺序)
const GROUP_CARDS = {
  creds: ['bot', 'proxy', 'tmdb', 'pan115'],
  channels: ['channels', 'monitor_tg'],
  pipeline: ['openlist', 'process', 'upload'],
  sharing: ['save_pipeline', 'monitor'],
}

const model = ref({})
const channels = ref([])
const monitorRows = ref([])
const chanMsg = ref({ text: '', kind: '' })
const chanBusy = ref(false)

// ── 频道监控(TG 源频道 → ed2k 卡片) ──
const mon = ref(null)
const monitorChannelRows = ref([])
const fetchRows = ref([])          // openlist 监控目录(一栏一项)
const monMsg = ref({ text: '', kind: '' })
const showLogin = ref(false)

const apiHashSaved = computed(() => cfg.value?.tg_api_hash === '••••••••')
const okChannels = computed(() => (mon.value?.channels || []).filter(c => c.ok).length)
const monDot = computed(() => {
  if (!mon.value) return ''
  if (mon.value.state === 'running') return mon.value.connected ? 'ok' : 'bad'
  if (mon.value.state === 'disabled') return 'warn'
  return 'bad'
})

async function refreshMonitor() {
  try {
    mon.value = await api('monitor')
  } catch (e) {
    monMsg.value = { text: e.message, kind: 'err' }
  }
}

function addMonitorChannel() {
  monitorChannelRows.value.push('')
}

function startLogin() {
  monMsg.value = { text: '', kind: '' }
  if (!mon.value?.api_set) {
    monMsg.value = { text: '请先填 tg_api_id / tg_api_hash,点顶部「保存并重启」后再登录', kind: 'warn' }
    return
  }
  showLogin.value = true
}

function onLoginDone(d) {
  showLogin.value = false
  if (d) {
    mon.value = d
    monMsg.value = { text: d.message, kind: 'ok' }
  }
  refreshMonitor()  // 同步"登录进行中/已完成"状态到卡片
}

function onLoginClose() {
  showLogin.value = false
  refreshMonitor()  // 关面板不清会话:回来时按钮应显示"继续登录"
}

async function logoutMonitor() {
  if (!window.confirm('退出登录会删除会话文件,下次需重新用手机号登录。继续?')) return
  try {
    const d = await api('monitor/logout', {})
    mon.value = d
    monMsg.value = { text: d.message, kind: 'ok' }
  } catch (e) {
    monMsg.value = { text: e.message, kind: 'err' }
  }
}

onMounted(async () => {
  cfg.value = await api('config')
  const m = {}
  for (const g of GROUPS) {
    for (const f of g.fields) {
      const v = cfg.value[f.key]
      m[f.key] = Array.isArray(v) ? v.join(',') : (v ?? '')
    }
  }
  // 频道监控的 API 凭据不在 GROUPS 里,单独回填(数字 0 视为未填)
  m.tg_api_id = cfg.value.tg_api_id || ''
  m.tg_api_hash = cfg.value.tg_api_hash ?? ''
  m.openlist_base_url = cfg.value.openlist_base_url ?? ''
  m.openlist_token = cfg.value.openlist_token ?? ''
  m.openlist_dest_path = cfg.value.openlist_dest_path || ''   // 不硬编码具体挂载名,留空走占位示例
  m.openlist_max_tasks = cfg.value.openlist_max_tasks || 2
  m.fetch_interval_minutes = cfg.value.fetch_interval_minutes || 5
  m.process_interval_minutes = cfg.value.process_interval_minutes || 5
  m.min_size_mb = cfg.value.min_size_mb ?? 50
  m.min_age_seconds = cfg.value.min_age_seconds ?? 60
  m.clean_enabled = cfg.value.clean_enabled !== false && cfg.value.clean_enabled !== 'false'
  m.cd2_address = cfg.value.cd2_address ?? ''
  m.cd2_token = cfg.value.cd2_token ?? ''
  m.cd2_source_path = cfg.value.cd2_source_path || '/clouddrive'
  m.cd2_dest_path = cfg.value.cd2_dest_path ?? ''
  m.upload_interval_minutes = cfg.value.upload_interval_minutes || 5
  m.upload_max_tasks = cfg.value.upload_max_tasks || 2
  model.value = m
  monitorRows.value = String(cfg.value.monitor_dirs || '')
    .split(',').map(x => x.trim()).filter(Boolean)
  monitorChannelRows.value = String(cfg.value.monitor_channels || '')
    .split(',').map(x => x.trim()).filter(Boolean)
  fetchRows.value = String(cfg.value.openlist_monitor_dirs || '')
    .split(',').map(x => x.trim()).filter(Boolean)
  const d = await api('channels')
  channels.value = d.channels.map(c => ({ ...c }))
  restartPending.value = !!cfg.value.restart_pending
  baseline.value = snapshot()   // 基线:之后与它比对判断有无未保存变更
  await refreshMonitor()
})

onUnmounted(clearPageBar)

function isSaved(f) {
  return f.sensitive && cfg.value?.[f.key] === '••••••••'
}

// ── 提交内容 / 变更检测 ──────────────────────────────────
function formValues() {
  const values = {}
  for (const g of GROUPS) {
    for (const f of g.fields) values[f.key] = model.value[f.key]
  }
  values.monitor_dirs = monitorRows.value.join(',')
  values.monitor_channels = monitorChannelRows.value.join(',')
  values.tg_api_id = model.value.tg_api_id
  values.tg_api_hash = model.value.tg_api_hash
  values.openlist_base_url = model.value.openlist_base_url
  values.openlist_token = model.value.openlist_token
  values.openlist_monitor_dirs = fetchRows.value.join(',')
  values.openlist_dest_path = model.value.openlist_dest_path
  values.openlist_max_tasks = model.value.openlist_max_tasks
  values.fetch_interval_minutes = model.value.fetch_interval_minutes
  values.process_interval_minutes = model.value.process_interval_minutes
  values.min_size_mb = model.value.min_size_mb
  values.min_age_seconds = model.value.min_age_seconds
  values.clean_enabled = model.value.clean_enabled
  values.cd2_address = model.value.cd2_address
  values.cd2_token = model.value.cd2_token
  values.cd2_source_path = model.value.cd2_source_path
  values.cd2_dest_path = model.value.cd2_dest_path
  values.upload_interval_minutes = model.value.upload_interval_minutes
  values.upload_max_tasks = model.value.upload_max_tasks
  return values
}

function snapshot() {
  return JSON.stringify({ values: formValues(), channels: channels.value })
}

const baseline = ref('')
const restartPending = ref(false)   // 后端口径:改过配置但运行中的 Bot 还没吃上
const dirty = computed(() => baseline.value !== '' && snapshot() !== baseline.value)
const needsRestart = computed(() => dirty.value || restartPending.value)

// ── 未保存标记(按区) ──────────────────────────────────
// 区块的「保存」在卡片内,换到别的分组页就看不见了 → 标题行与分组切换条上必须有记号,
// 否则"哪一组还有没保存的改动"完全看不出来。
const channelsDirty = computed(() => {
  if (!baseline.value) return false
  return JSON.stringify(channels.value) !== JSON.stringify(JSON.parse(baseline.value).channels)
})
function cardDirty(key) {
  if (key === 'channels') return channelsDirty.value     // 频道归属走独立保存(saveChannels)
  if (!baseline.value) return false
  const base = JSON.parse(baseline.value).values || {}
  const now = formValues()
  return (SECTION_KEYS[key] || []).some((k) => String(now[k] ?? '') !== String(base[k] ?? ''))
}
function groupDirty(key) {
  return (GROUP_CARDS[key] || []).some(cardDirty)
}

// 顶部操作条:只在"有需要重启的变更"时出现(未保存 或 已保存未生效)
watch([needsRestart, dirty, busy], () => {
  if (!needsRestart.value) {
    clearPageBar()
    return
  }
  setPageBar({
    text: dirty.value ? '有未保存的配置变更' : '配置已保存,尚未生效',
    hint: dirty.value
      ? '保存并重启后生效;仅点卡片内「保存」不会作用于运行中的 Bot'
      : '服务重启后生效',
    actionText: '保存并重启',
    busy: busy.value,
    action: () => save(true),
  })
}, { immediate: true })

async function save(restart) {
  busy.value = true
  msg.value = { text: '', kind: '' }
  try {
    const d = await api('config', { values: formValues() }, 'PUT')
    baseline.value = snapshot()
    restartPending.value = !!d.restart_pending
    if (restart) {
      await api('restart', {})
      msg.value = { text: '已保存,服务重启中…页面将在数秒后自动恢复', kind: 'ok' }
      setTimeout(() => location.reload(), 6000)
    } else {
      msg.value = d.bot_ready
        ? { text: '已保存,配置齐全。重启后 Bot 即启动', kind: 'ok' }
        : { text: '已保存,但仍有缺项:' + d.missing.join(';'), kind: 'warn' }
      emit('refresh-status')
    }
  } catch (e) {
    msg.value = { text: e.message, kind: 'err' }
  } finally {
    busy.value = false
  }
}

// ── 网盘目录选择器 ──
const showPicker = ref(false)
const pickerTarget = ref('')            // 'pipeline_root_dir' 或行索引(字符串)
const pickerSource = ref('115')         // '115' | 'openlist'

function openPicker(target, source = '115') {
  pickerTarget.value = String(target)
  pickerSource.value = source
  showPicker.value = true
}

function onPickDir(d) {
  if (pickerSource.value === 'cd2') {
    model.value[pickerTarget.value] = d.cid      // cd2_dest_path / cd2_source_path
  } else if (pickerSource.value === 'openlist') {
    if (pickerTarget.value === 'openlist_dest_path') {
      model.value.openlist_dest_path = d.cid     // 落地点
    } else {
      fetchRows.value[Number(pickerTarget.value)] = d.cid   // 监控目录(一栏一项)
    }
  } else if (pickerTarget.value === 'pipeline_root_dir') {
    model.value.pipeline_root_dir = d.cid
  } else {
    monitorRows.value[Number(pickerTarget.value)] = d.cid
  }
  showPicker.value = false
}

function addMonitorRow() {
  monitorRows.value.push('')
}

// ── TG 频道配置:归属频道管理 ──
const PRESET_NAMES = { '115': '115链接推送频道', ed2k: 'ed2k链接推送频道' }

function presetLabel(p) {
  return PRESET_NAMES[p] || p
}

function addChannel() {
  channels.value.push({ chat_id: '', preset: '115', title: '' })
}

function removeChannel(i) {
  channels.value.splice(i, 1)
}

async function saveChannels() {
  chanBusy.value = true
  chanMsg.value = { text: '', kind: '' }
  try {
    const d = await api('channels', { channels: channels.value }, 'PUT')
    chanMsg.value = { text: d.message, kind: 'ok' }
    baseline.value = snapshot()
    restartPending.value = !!d.restart_pending
  } catch (e) {
    chanMsg.value = { text: e.message, kind: 'err' }
  } finally {
    chanBusy.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div class="page-title">全局配置</div>
      <div class="page-sub">{{ activeMeta.desc }}</div>
    </div>

    <!-- 分组切换条:窄屏(≤900px 侧栏收成纯图标、手机是底部导航)放不下侧栏子项,
         所以这一排是必须的入口;大屏也保留,免得每次都把鼠标甩到最左边 -->
    <div class="grouptabs">
      <router-link v-for="g in CONFIG_GROUPS" :key="g.key" class="grouptab"
        :class="{ active: g.key === activeGroup }" :to="`/push/${g.key}`">
        {{ g.label }}<span v-if="groupDirty(g.key)" class="unsaved-dot" title="这一组有未保存的改动"></span>
      </router-link>
    </div>

    <div v-if="msg.text" class="msg" :class="msg.kind" style="margin-bottom:14px">{{ msg.text }}</div>

    <div v-for="g in visibleGroups" :key="g.title" class="card">
      <h3>{{ g.title }}<span v-if="cardDirty(g.key)" class="unsaved-dot" title="这一区有未保存的改动"></span></h3>
      <div class="desc">{{ g.desc }}</div>

      <div v-for="f in g.fields" :key="f.key" class="field">
        <label>
          {{ f.label }} <code>{{ f.key }}</code>
          <span v-if="isSaved(f)" class="saved-tag">✓ 已保存</span>
        </label>
        <div v-if="f.picker" style="display:flex;gap:8px">
          <input v-model="model[f.key]" :placeholder="f.ph" autocomplete="off">
          <button class="btn ghost" style="flex:none;padding:8px 12px"
            title="浏览网盘选择目录" @click="openPicker(f.key)">📂</button>
        </div>
        <input v-else v-model="model[f.key]" :placeholder="f.ph" autocomplete="off">
        <!-- 提示恒显:原来写了 v-if="!isSaved(f)",字段一存过说明就消失了 -->
        <div class="chan-tip">💡 {{ f.hint }}</div>
      </div>

      <div class="actions">
        <button class="btn primary" :disabled="busy" @click="save(false)">保存</button>
      </div>
    </div>

    <div v-if="activeGroup === 'sharing'" class="card">
      <h3>目录监控<span v-if="cardDirty('monitor')" class="unsaved-dot" title="这一区有未保存的改动"></span></h3>
      <div class="desc">网盘目录里出现新资源时,自动整理 → 建永久分享 → 过审后推卡(每 30 分钟扫一轮)</div>

      <div class="chan-tip">
        💡 每行一个监控目录:点 <strong>📂</strong> 浏览网盘选择(回填目录 ID),也可手动输入目录 ID。
        资源处理完会自动移出监控目录,不用手动清理。
      </div>

      <div class="chan-list">
        <div v-for="(d, i) in monitorRows" :key="i" class="chan-row">
          <input v-model="monitorRows[i]" class="chan-id" placeholder="网盘目录 ID 或路径" style="flex:1">
          <button class="btn ghost" style="flex:none;padding:8px 12px" title="浏览网盘选择"
            @click="openPicker(String(i))">📂</button>
          <button class="btn danger chan-del" @click="monitorRows.splice(i, 1)">删除</button>
        </div>
      </div>
      <div v-if="!monitorRows.length" class="empty" style="margin-bottom:12px">
        还没有监控目录 —— 点下方添加,或直接等资源放进来
      </div>

      <div class="actions">
        <button class="btn ghost" @click="addMonitorRow">+ 添加目录</button>
        <button class="btn primary" :disabled="busy" @click="save(false)">保存</button>
      </div>
    </div>

    <div v-if="activeGroup === 'pipeline'" class="card">
      <h3>资源获取(openlist)<span v-if="cardDirty('openlist')" class="unsaved-dot" title="这一区有未保存的改动"></span></h3>
      <div class="desc">
        监控 openlist 里的目录,新资源自动<strong>移动</strong>到本地落地点(media/openlist),再交给处理链。
        是移动不是复制,源盘不留副本
      </div>

      <div class="field">
        <label>openlist 地址 <code>openlist_base_url</code></label>
        <input v-model="model.openlist_base_url" placeholder="http://127.0.0.1:5244" autocomplete="off">
        <div class="chan-tip">💡 自建 openlist 的服务地址(不是官网文档的地址)。本机部署一般就是 http://127.0.0.1:5244。</div>
      </div>
      <div class="field">
        <label>
          openlist 令牌 <code>openlist_token</code>
          <span v-if="cfg?.openlist_token === '••••••••'" class="saved-tag">✓ 已保存</span>
        </label>
        <input v-model="model.openlist_token" placeholder="openlist-xxxxxxxx…" autocomplete="off">
        <div class="chan-tip">💡 在 openlist 网页端「个人设置 → API 令牌」里生成。填错或过期,获取段会连不上。</div>
      </div>

      <div class="chan-tip">
        💡 每行一个<strong>监控目录</strong>(openlist 侧路径,点 <strong>📂</strong> 浏览选择):
        该目录下一出现新条目就会被搬走。全部留空 = 获取段不启动。
      </div>
      <div class="chan-list">
        <div v-for="(r, i) in fetchRows" :key="i" class="chan-row">
          <input v-model="fetchRows[i]" class="chan-id" placeholder="/网盘名/影库目录">
          <button class="btn ghost" style="flex:none;padding:8px 12px" title="浏览 openlist 选择目录"
            @click="openPicker(String(i), 'openlist')">📂</button>
          <button class="btn danger chan-del" @click="fetchRows.splice(i, 1)">删除</button>
        </div>
      </div>
      <div v-if="!fetchRows.length" class="empty" style="margin-bottom:12px">
        还没有监控目录 —— 获取段当前不会启动
      </div>

      <div class="field">
        <label>落地点 <code>openlist_dest_path</code></label>
        <div style="display:flex;gap:8px">
          <input v-model="model.openlist_dest_path" placeholder="/我的资源/下载落地" autocomplete="off">
          <button class="btn ghost" style="flex:none;padding:8px 12px" title="浏览 openlist 选择目录"
            @click="openPicker('openlist_dest_path', 'openlist')">📂</button>
        </div>
        <div class="chan-tip">💡 要选那个<strong>挂载到本项目 media/openlist 的 openlist 目录</strong> —— 挂载是 openlist 那边建的,选错的话文件搬过去也不会出现在本地。</div>
      </div>
      <div class="grid2">
        <div class="field">
          <label>并发上限 <code>openlist_max_tasks</code></label>
          <input v-model="model.openlist_max_tasks" type="number" min="1" max="5">
          <div class="chan-tip">💡 同时最多搬几个文件。默认 2,调大更快但更吃机器。</div>
        </div>
        <div class="field">
          <label>扫描间隔(分钟)<code>fetch_interval_minutes</code></label>
          <input v-model="model.fetch_interval_minutes" type="number" min="1">
          <div class="chan-tip">💡 多久扫一轮监控目录。默认 5 分钟。</div>
        </div>
      </div>

      <div class="actions">
        <button class="btn ghost" @click="fetchRows.push('')">+ 添加监控目录</button>
        <button class="btn primary" :disabled="busy" @click="save(false)">保存</button>
      </div>
    </div>

    <div v-if="activeGroup === 'pipeline'" class="card">
      <h3>处理链<span v-if="cardDirty('process')" class="unsaved-dot" title="这一区有未保存的改动"></span></h3>
      <div class="desc">
        落地点里的新文件自动:探测(ffprobe)→ 识别(TMDB)→ 重命名 → 算 ed2k → 推卡 → 归档到上传源。
        识别不出的会拦下、留在原地并通知你,不会硬走
      </div>
      <div class="grid3">
        <div class="field">
          <label>扫描间隔(分钟)<code>process_interval_minutes</code></label>
          <input v-model="model.process_interval_minutes" type="number" min="1">
          <div class="chan-tip">💡 多久扫一次落地点。默认 5 分钟。</div>
        </div>
        <div class="field">
          <label>体积下限(MB)<code>min_size_mb</code></label>
          <input v-model="model.min_size_mb" type="number" min="0">
          <div class="chan-tip">💡 小于这个体积的文件直接跳过,免得把样片、字幕包当正片处理。默认 50MB。</div>
        </div>
        <div class="field">
          <label>静默年龄(秒)<code>min_age_seconds</code></label>
          <input v-model="model.min_age_seconds" type="number" min="0">
          <div class="chan-tip">💡 文件写入后至少静默这么久才动手 —— 宁等一轮,也不处理还在写的半截文件。默认 60 秒。</div>
        </div>
      </div>
      <label class="switch-row">
        <input class="switch" type="checkbox" v-model="model.clean_enabled">
        <span class="switch-state">元数据清洗</span>
      </label>
      <div class="chan-tip">
        💡 只在探测到广告类脏数据时才重封装(容器广告标签 / 垃圾章节 / 广告音轨字幕轨),干净文件一个字节都不动。
        零重编码,校验视频轨与时长后再同名替换,失败则原件不动。
      </div>
      <div class="actions">
        <button class="btn primary" :disabled="busy" @click="save(false)">保存</button>
      </div>
    </div>

    <div v-if="activeGroup === 'pipeline'" class="card">
      <h3>上传链(CD2 → 115)<span v-if="cardDirty('upload')" class="unsaved-dot" title="这一区有未保存的改动"></span></h3>
      <div class="desc">
        把上传源里的成品<strong>移动</strong>到 115 网盘(走 CD2)。上传完成后本地源会被删除,磁盘空间随之释放
      </div>
      <div class="field">
        <label>CD2 地址 <code>cd2_address</code></label>
        <input v-model="model.cd2_address" placeholder="127.0.0.1:19798" autocomplete="off">
        <div class="chan-tip">💡 CloudDrive2 的 gRPC 地址,端口默认 19798。本机部署就填 127.0.0.1:19798。</div>
      </div>
      <div class="field">
        <label>
          CD2 令牌 <code>cd2_token</code>
          <span v-if="cfg?.cd2_token === '••••••••'" class="saved-tag">✓ 已保存</span>
        </label>
        <input v-model="model.cd2_token" placeholder="CloudDrive2 里创建的 API 令牌" autocomplete="off">
        <div class="chan-tip">💡 在 CloudDrive2 界面「设置 → API 令牌」里创建(不是登录密码)。</div>
      </div>
      <div class="field">
        <label>待上传目录(CD2 侧本地视图)<code>cd2_source_path</code></label>
        <div style="display:flex;gap:8px">
          <input v-model="model.cd2_source_path" placeholder="/clouddrive" autocomplete="off">
          <button class="btn ghost" style="flex:none;padding:8px 12px" title="浏览 CloudDrive 选择目录"
            @click="openPicker('cd2_source_path', 'cd2')">📂</button>
        </div>
        <div class="chan-tip">💡 CD2 侧看到的本地待上传目录,通常就是 /clouddrive —— 它对应本项目的 media/clouddrive。</div>
      </div>
      <div class="field">
        <label>上传目标目录(115 侧)<code>cd2_dest_path</code></label>
        <div style="display:flex;gap:8px">
          <input v-model="model.cd2_dest_path" placeholder="点 📂 选择目标目录" autocomplete="off">
          <button class="btn ghost" style="flex:none;padding:8px 12px" title="浏览 CloudDrive 选择目录"
            @click="openPicker('cd2_dest_path', 'cd2')">📂</button>
        </div>
        <div class="chan-tip">💡 成品传到 115 的哪个目录,点 📂 在 CloudDrive 目录树里逐级选择。留空 = 上传段不启动;目标目录不存在会自动创建。</div>
      </div>
      <div class="grid2">
        <div class="field">
          <label>上传并发上限 <code>upload_max_tasks</code></label>
          <input v-model="model.upload_max_tasks" type="number" min="1" max="5">
          <div class="chan-tip">💡 同时上传几个文件。默认 2 —— CD2 本身能同时跑更多。</div>
        </div>
        <div class="field">
          <label>上传轮询间隔(分钟)<code>upload_interval_minutes</code></label>
          <input v-model="model.upload_interval_minutes" type="number" min="1">
          <div class="chan-tip">💡 多久扫一次上传源。默认 5 分钟。</div>
        </div>
      </div>
      <div class="actions">
        <button class="btn primary" :disabled="busy" @click="save(false)">保存</button>
      </div>
    </div>

    <div v-if="activeGroup === 'channels'" class="card">
      <h3>TG 频道配置<span v-if="cardDirty('channels')" class="unsaved-dot" title="这一区有未保存的改动"></span></h3>
      <div class="desc">归属分流:115 链接与 ed2k 链接各推各的频道,登记后即时生效</div>

      <div class="chan-tip">
        💡 登记方式:先发 <code>/bind</code> 给 Bot,再把频道里的任意一条消息
        <strong>转发给 Bot</strong>,按提示选择归属(115链接推送频道 / ed2k链接推送频道);也可以直接在下方手动添加。
        想让 Bot 把别处频道里的 ed2k 自动转进来,见「推送与频道」里的「频道监控」。
      </div>
      <div class="chan-tip">
        💡 下方每行一个频道:<strong>频道名称</strong>只是写给你自己看的(选填);
        <strong>chat_id</strong> 是 <code>-100</code> 开头的那串;<strong>右侧胶囊是归属</strong>,点一下在 115 / ed2k 之间切换。
        <strong>同一归属可以配多个频道,推送时每个都会收到</strong>。
      </div>

      <div class="chan-list">
        <div v-for="(c, i) in channels" :key="i" class="chan-row">
          <input v-model="c.title" class="chan-title" placeholder="频道名称(选填)">
          <input v-model="c.chat_id" class="chan-id" placeholder="-100xxxxxxxxxx">
          <button class="chip on" :title="'点击切换归属(当前:' + presetLabel(c.preset) + ')'"
            @click="c.preset = c.preset === '115' ? 'ed2k' : '115'">
            {{ presetLabel(c.preset) }}
          </button>
          <button class="btn danger chan-del" @click="removeChannel(i)">删除</button>
        </div>
      </div>
      <div v-if="!channels.length" class="empty" style="margin-bottom:12px">
        还没有登记归属频道 —— 转发频道消息给 Bot,或点下方添加
      </div>

      <div v-if="chanMsg.text" class="msg" :class="chanMsg.kind">{{ chanMsg.text }}</div>

      <div class="actions">
        <button class="btn ghost" @click="addChannel">+ 添加频道</button>
        <button class="btn primary" :disabled="chanBusy" @click="saveChannels">保存频道</button>
      </div>
    </div>

    <div v-if="activeGroup === 'channels'" class="card">
      <h3>频道监控<span v-if="cardDirty('monitor_tg')" class="unsaved-dot" title="这一区有未保存的改动"></span></h3>
      <div class="desc">
        盯住 TG 源频道:消息里的 ed2k 链接会自动转成卡片,推到 ed2k 归属频道
        (需先在上面「TG 频道配置」里登记 ed2k 归属)
      </div>

      <div class="field">
        <label>TG API ID <code>tg_api_id</code></label>
        <input v-model="model.tg_api_id" placeholder="1234567" autocomplete="off">
        <div class="chan-tip">💡 在 my.telegram.org → API development tools 里创建应用后得到,是一串纯数字。</div>
      </div>
      <div class="field">
        <label>
          TG API Hash <code>tg_api_hash</code>
          <span v-if="apiHashSaved" class="saved-tag">✓ 已保存</span>
        </label>
        <input v-model="model.tg_api_hash" placeholder="api_hash" autocomplete="off">
        <div class="chan-tip">💡 与 API ID 一起发放,是 32 个十六进制字符。这一对是「以你的账号身份监听源频道」用的,与 Bot Token 不是一回事。</div>
      </div>

      <div class="chan-tip">
        💡 每行一个源频道:公开频道填 <strong>@用户名</strong> 或 <code>t.me/xxx</code>,
        私有频道填 <code>-100</code> 开头的频道 ID。首次接入<strong>只从当前消息开始</strong>监听、不回补历史;
        停机期间的漏档会在重启后按游标补扫。改动后点页面顶部出现的「保存并重启」生效。
      </div>

      <div class="chan-list">
        <div v-for="(r, i) in monitorChannelRows" :key="i" class="chan-row">
          <input v-model="monitorChannelRows[i]" class="chan-id" placeholder="@频道用户名 / t.me 链接 / -100 频道 ID">
          <button class="btn danger chan-del" @click="monitorChannelRows.splice(i, 1)">删除</button>
        </div>
      </div>
      <div v-if="!monitorChannelRows.length" class="empty" style="margin-bottom:12px">
        还没有源频道 —— 添加后保存(顶部会出现「保存并重启」),再点「登录账号」开始监听
      </div>

      <div class="mon-line">
        <span class="dot" :class="monDot"></span>
        <span>{{ mon ? mon.state_text : '状态加载中…' }}</span>
        <span v-if="mon?.account" class="mon-sub">· {{ mon.account }}</span>
        <span v-if="mon?.login_stage" class="mon-sub">
          · 登录进行中({{ mon.login_stage === 'password' ? '待两步密码' : '待验证码' }})
        </span>
        <span v-if="mon?.channels?.length" class="mon-sub">
          · {{ okChannels }}/{{ mon.channels.length }} 频道可达
        </span>
      </div>

      <div v-if="mon?.channels?.length" class="mon-chans">
        <div v-for="c in mon.channels" :key="c.ref" class="mon-chan">
          <span class="dot" :class="c.error ? 'bad' : c.ok ? 'ok' : 'warn'"></span>
          <span class="mon-chan-name">{{ c.title || c.ref }}</span>
          <span class="mon-sub">{{ c.ref }}</span>
          <span v-if="c.error" class="mon-err">{{ c.error }}</span>
          <span v-else-if="c.chat_id" class="mon-sub">游标消息 {{ c.last_msg_id }}</span>
          <span v-else class="mon-sub">尚未接入(重启后解析)</span>
        </div>
      </div>

      <div v-if="monMsg.text" class="msg" :class="monMsg.kind">{{ monMsg.text }}</div>

      <div class="actions">
        <button class="btn ghost" @click="addMonitorChannel">+ 添加源频道</button>
        <button class="btn primary" :disabled="busy" @click="save(false)">保存</button>
        <button v-if="mon?.account" class="btn ghost" style="margin-left:auto" @click="logoutMonitor">
          退出登录
        </button>
        <button v-else class="btn ghost" style="margin-left:auto" @click="startLogin">
          {{ mon?.login_stage ? '继续登录' : '登录账号' }}
        </button>
      </div>
    </div>

    <DirPickerModal v-if="showPicker" :source="pickerSource" @pick="onPickDir" @close="showPicker = false" />
    <MonitorLoginModal v-if="showLogin" :mon="mon" @done="onLoginDone" @close="onLoginClose" />
  </div>
</template>
