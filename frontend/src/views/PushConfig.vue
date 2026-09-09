<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['refresh-status'])

const cfg = ref(null)
const msg = ref({ text: '', kind: '' })
const busy = ref(false)

const FIELDS = [
  { key: 'tg_bot_token', label: 'Bot Token', hint: 'BotFather 发放的令牌', sensitive: true },
  { key: 'tg_chat_id', label: '默认频道 Chat ID', hint: '未匹配归属时的兜底频道(Bot 需为频道管理员)' },
  { key: 'tg_admin_ids', label: '管理员用户 ID', hint: '逗号分隔,如 123,456', list: true },
  { key: 'tmdb_api_key', label: 'TMDB API Key', hint: 'themoviedb.org 免费申请;留空则卡片无元数据', sensitive: true },
  { key: 'proxy_url', label: '代理地址(可选)', hint: '仅 TG/TMDB 走;115 恒直连' },
  { key: 'pan115_cookie', label: '115 Cookie(可选)', hint: '浏览器登录 115 后 F12 复制;填后走稳定通道读分享', sensitive: true },
]

const model = ref({})
const channels = ref([])
const chanMsg = ref({ text: '', kind: '' })
const chanBusy = ref(false)

onMounted(async () => {
  cfg.value = await api('config')
  const m = {}
  for (const f of FIELDS) {
    const v = cfg.value[f.key]
    m[f.key] = Array.isArray(v) ? v.join(',') : (v ?? '')
  }
  model.value = m
  const d = await api('channels')
  channels.value = d.channels.map(c => ({ ...c }))
})

function isSaved(f) {
  return f.sensitive && cfg.value?.[f.key] === '••••••••'
}

async function save(restart) {
  busy.value = true
  msg.value = { text: '', kind: '' }
  try {
    const values = {}
    for (const f of FIELDS) values[f.key] = model.value[f.key]
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

// ── 频道管理 ──
function addChannel() {
  channels.value.push({ chat_id: '', preset: '115', title: '' })
}

function removeChannel(i) {
  channels.value.splice(i, 1)
}

function presetLabel(p) {
  return p === 'ed2k' ? '🔗 ed2k' : '💿 115 网盘'
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
      <div class="page-title">推送配置</div>
      <div class="page-sub">链接识别 → TMDB 匹配 → 海报卡片 → 按归属推送到对应频道</div>
    </div>

    <div class="card">
      <h3>PUSH CHAIN</h3>
      <div class="desc">基础凭据与默认投递目标;保存后需重启生效</div>
      <div v-if="msg.text" class="msg" :class="msg.kind">{{ msg.text }}</div>

      <div v-for="f in FIELDS" :key="f.key" class="field">
        <label>
          {{ f.label }} <code>{{ f.key }}</code>
          <span v-if="isSaved(f)" class="saved-tag">✓ 已保存</span>
        </label>
        <input v-model="model[f.key]" :placeholder="f.hint" autocomplete="off">
        <div v-if="!isSaved(f)" class="hint">{{ f.hint }}</div>
      </div>

      <div class="actions">
        <button class="btn primary" :disabled="busy" @click="save(false)">保存配置</button>
        <button class="btn ghost" :disabled="busy" @click="save(true)">保存并重启</button>
      </div>
    </div>

    <div class="card">
      <h3>CHANNELS</h3>
      <div class="desc">推送频道归属 · 115 链接与 ed2k 链接可各推到不同频道</div>

      <div class="chan-tip">
        💡 最方便的登记方式:把频道里的任意一条消息<strong>转发给 Bot</strong>,
        按提示选归属即登记,即时生效无需重启。此处用于手动管理。
      </div>

      <div class="chan-list">
        <div v-for="(c, i) in channels" :key="i" class="chan-row">
          <input v-model="c.title" class="chan-title" placeholder="频道名称(选填)">
          <input v-model="c.chat_id" class="chan-id" placeholder="-100xxxxxxxxxx">
          <div class="chan-presets">
            <button class="chip" :class="{ on: c.preset === '115' }" @click="c.preset = '115'">💿 115</button>
            <button class="chip" :class="{ on: c.preset === 'ed2k' }" @click="c.preset = 'ed2k'">🔗 ed2k</button>
          </div>
          <button class="btn danger chan-del" @click="removeChannel(i)">删除</button>
        </div>
      </div>
      <div v-if="!channels.length" class="empty" style="margin-bottom:12px">
        还没有登记频道 —— 转发频道消息给 Bot,或点下方添加
      </div>

      <div v-if="chanMsg.text" class="msg" :class="chanMsg.kind">{{ chanMsg.text }}</div>

      <div class="actions">
        <button class="btn ghost" @click="addChannel">+ 添加频道</button>
        <button class="btn primary" :disabled="chanBusy" @click="saveChannels">保存频道</button>
      </div>
    </div>
  </div>
</template>
