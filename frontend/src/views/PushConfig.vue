<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import DirPickerModal from './DirPickerModal.vue'

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
  {
    title: '目录监控',
    desc: '网盘目录出现新资源时,自动整理→建永久分享→审核通过后推送(30 分钟一轮)',
    fields: [
      { key: 'monitor_dirs', label: '监控目录(可选)', hint: '网盘目录 ID,多个逗号分隔(点 📂 逐个选择);留空不监控', picker: true, multi: true },
    ],
  },
]

const model = ref({})
const channels = ref([])
const chanMsg = ref({ text: '', kind: '' })
const chanBusy = ref(false)

onMounted(async () => {
  cfg.value = await api('config')
  const m = {}
  for (const g of GROUPS) {
    for (const f of g.fields) {
      const v = cfg.value[f.key]
      m[f.key] = Array.isArray(v) ? v.join(',') : (v ?? '')
    }
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
    for (const g of GROUPS) {
      for (const f of g.fields) values[f.key] = model.value[f.key]
    }
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
const pickerTarget = ref('')

function openPicker(key) {
  pickerTarget.value = key
  showPicker.value = true
}

function onPickDir(d) {
  const key = pickerTarget.value
  const cur = String(model.value[key] || '').trim()
  if (key === 'monitor_dirs') {
    // 多值:追加 CID(去重)
    const ids = cur ? cur.split(',').map(x => x.trim()).filter(Boolean) : []
    if (!ids.includes(String(d.cid))) ids.push(String(d.cid))
    model.value[key] = ids.join(',')
  } else {
    model.value[key] = String(d.cid)
  }
  showPicker.value = false
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
      <h3>TG 频道配置</h3>
      <div class="desc">归属分流 · 115 链接与 ed2k 链接各推送到对应归属频道,登记后即时生效</div>

      <div class="chan-tip">
        💡 登记方式:先发 <code>/bind</code> 给 Bot,再把频道里的任意一条消息
        <strong>转发给 Bot</strong>,按提示选择归属(115链接推送频道 / ed2k链接推送频道);
        或在下方手动添加。后续将在此扩展频道监控能力。
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

    <DirPickerModal v-if="showPicker" @pick="onPickDir" @close="showPicker = false" />
  </div>
</template>
