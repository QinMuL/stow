<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import DirPickerModal from './DirPickerModal.vue'
import MonitorLoginModal from './MonitorLoginModal.vue'

const emit = defineEmits(['refresh-status'])

const cfg = ref(null)
const msg = ref({ text: '', kind: '' })
const busy = ref(false)

// 五组卡片:每组独立保存(实际提交全部字段,后端全量校验)
const GROUPS = [
  {
    title: 'Telegram Bot 机器人配置',
    desc: 'Bot 凭据与管理员;缺一 Bot 无法启动',
    fields: [
      { key: 'tg_bot_token', label: 'Bot Token', hint: 'BotFather 发放的令牌', sensitive: true },
      { key: 'tg_admin_ids', label: '管理员用户 ID', hint: '逗号分隔,如 123,456', list: true },
    ],
  },
  {
    title: '项目代理配置',
    desc: '仅 TG / TMDB 走代理;115 恒直连',
    fields: [
      { key: 'proxy_url', label: '代理地址(可选)', hint: '如 http://127.0.0.1:7897;留空则直连' },
    ],
  },
  {
    title: 'TMDB API Key 配置',
    desc: '卡片元数据来源;留空则卡片无 TMDB 信息仍可推送',
    fields: [
      { key: 'tmdb_api_key', label: 'TMDB API Key', hint: 'themoviedb.org 免费申请', sensitive: true },
    ],
  },
  {
    title: '115 Cookie 配置',
    desc: '读分享通道;填后走稳定通道,空则匿名易限流',
    fields: [
      { key: 'pan115_cookie', label: '115 Cookie(可选)', hint: '浏览器登录 115 后 F12 复制整行 Cookie', sensitive: true },
    ],
  },
  {
    title: '转存流水线',
    desc: '/save <链接> 触发:转存→整理→建永久分享→审核通过后推送;需先配置上方 Cookie',
    fields: [
      { key: 'pipeline_root_dir', label: '流水线根目录', hint: '填网盘目录 ID(点 📂 选择)或路径;不存在自动创建', picker: true },
    ],
  },
]

const model = ref({})
const channels = ref([])
const monitorRows = ref([])
const chanMsg = ref({ text: '', kind: '' })
const chanBusy = ref(false)

// ── 频道监控(TG 源频道 → ed2k 卡片) ──
const mon = ref(null)
const monitorChannelRows = ref([])
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
    monMsg.value = { text: '请先填 tg_api_id / tg_api_hash,点「保存并重启」后再登录', kind: 'warn' }
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
  model.value = m
  monitorRows.value = String(cfg.value.monitor_dirs || '')
    .split(',').map(x => x.trim()).filter(Boolean)
  monitorChannelRows.value = String(cfg.value.monitor_channels || '')
    .split(',').map(x => x.trim()).filter(Boolean)
  const d = await api('channels')
  channels.value = d.channels.map(c => ({ ...c }))
  await refreshMonitor()
})

function isSaved(f) {
  return f.sensitive && cfg.value?.[f.key] === '••••••••'
}

async function save(restart) {
  busy.value = true
  msg.value = { text: '', kind: '' }
  try {
    const values = {}
    for (const g of GROUPS) {
      for (const f of g.fields) values[f.key] = model.value[f.key]
    }
    values.monitor_dirs = monitorRows.value.join(',')
    values.monitor_channels = monitorChannelRows.value.join(',')
    values.tg_api_id = model.value.tg_api_id
    values.tg_api_hash = model.value.tg_api_hash
    const d = await api('config', { values }, 'PUT')
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
const pickerTarget = ref('')  // 'pipeline' 或监控行索引(字符串)

function openPicker(target) {
  pickerTarget.value = String(target)
  showPicker.value = true
}

function onPickDir(d) {
  if (pickerTarget.value === 'pipeline_root_dir') {
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
      <div class="page-sub">凭据 · 代理 · 频道归属;各卡片独立保存</div>
    </div>

    <div v-if="msg.text" class="msg" :class="msg.kind" style="margin-bottom:14px">{{ msg.text }}</div>

    <div v-for="g in GROUPS" :key="g.title" class="card">
      <h3>{{ g.title }}</h3>
      <div class="desc">{{ g.desc }}</div>

      <div v-for="f in g.fields" :key="f.key" class="field">
        <label>
          {{ f.label }} <code>{{ f.key }}</code>
          <span v-if="isSaved(f)" class="saved-tag">✓ 已保存</span>
        </label>
        <div v-if="f.picker" style="display:flex;gap:8px">
          <input v-model="model[f.key]" :placeholder="f.hint" autocomplete="off">
          <button class="btn ghost" style="flex:none;padding:8px 12px"
            title="浏览网盘选择目录" @click="openPicker(f.key)">📂</button>
        </div>
        <input v-else v-model="model[f.key]" :placeholder="f.hint" autocomplete="off">
        <div v-if="!isSaved(f)" class="hint">{{ f.hint }}</div>
      </div>

      <div class="actions">
        <button class="btn primary" :disabled="busy" @click="save(false)">保存</button>
      </div>
    </div>

    <div class="card">
      <h3>目录监控</h3>
      <div class="desc">网盘目录出现新资源时,自动整理→建永久分享→审核通过后推送(30 分钟一轮)</div>

      <div class="chan-tip">
        💡 每行一个监控目录:点 <strong>📂</strong> 浏览网盘选择(回填目录 ID),也可手动输入目录 ID。
        处理完成的资源会自动移出监控目录,无需手动清理。
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

    <div class="card">
      <h3>TG 频道配置</h3>
      <div class="desc">归属分流 · 115 链接与 ed2k 链接各推送到对应归属频道,登记后即时生效</div>

      <div class="chan-tip">
        💡 登记方式:先发 <code>/bind</code> 给 Bot,再把频道里的任意一条消息
        <strong>转发给 Bot</strong>,按提示选择归属(115链接推送频道 / ed2k链接推送频道);
        或在下方手动添加。让 Bot 自动把别处的 ed2k 转进来,见下方「频道监控」卡片。
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
        <button class="btn ghost" :disabled="busy" @click="save(true)" style="margin-left:auto">保存并重启</button>
      </div>
    </div>

    <div class="card">
      <h3>频道监控</h3>
      <div class="desc">
        盯住 TG 源频道:消息里的 ed2k 链接按本项目卡片模板自动转发到
        ed2k 链接推送频道(需先登记 ed2k 归属频道)
      </div>

      <div class="field">
        <label>TG API ID <code>tg_api_id</code></label>
        <input v-model="model.tg_api_id" placeholder="my.telegram.org 申请的数字 api_id" autocomplete="off">
        <div class="hint">在 my.telegram.org → API development tools 申请(可与旧项目复用同一份)</div>
      </div>
      <div class="field">
        <label>
          TG API Hash <code>tg_api_hash</code>
          <span v-if="apiHashSaved" class="saved-tag">✓ 已保存</span>
        </label>
        <input v-model="model.tg_api_hash" placeholder="32 位 api_hash" autocomplete="off">
      </div>

      <div class="chan-tip">
        💡 每行一个源频道:公开频道填 <strong>@用户名</strong> 或 <code>t.me/xxx</code>,
        私有频道填 <code>-100</code> 开头的频道 ID。首次接入<strong>只从当前消息开始</strong>监听、
        不回补历史;停机期间的漏档会在重启后按游标补扫。改动后点「保存并重启」生效。
      </div>

      <div class="chan-list">
        <div v-for="(r, i) in monitorChannelRows" :key="i" class="chan-row">
          <input v-model="monitorChannelRows[i]" class="chan-id" placeholder="@频道用户名 / t.me 链接 / -100 频道 ID">
          <button class="btn danger chan-del" @click="monitorChannelRows.splice(i, 1)">删除</button>
        </div>
      </div>
      <div v-if="!monitorChannelRows.length" class="empty" style="margin-bottom:12px">
        还没有源频道 —— 添加后「保存并重启」,再点右下角「登录账号」开始监听
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
        <button class="btn ghost" :disabled="busy" @click="save(true)">保存并重启</button>
      </div>
    </div>

    <DirPickerModal v-if="showPicker" @pick="onPickDir" @close="showPicker = false" />
    <MonitorLoginModal v-if="showLogin" :mon="mon" @done="onLoginDone" @close="onLoginClose" />
  </div>
</template>
