<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['refresh-status'])

const cfg = ref(null)
const msg = ref({ text: '', kind: '' })
const busy = ref(false)

const FIELDS = [
  { key: 'tg_bot_token', label: 'Bot Token', hint: 'BotFather 发放的令牌', sensitive: true },
  { key: 'tg_chat_id', label: '频道 Chat ID', hint: '推送目标频道(Bot 需为频道管理员)' },
  { key: 'tg_admin_ids', label: '管理员用户 ID', hint: '逗号分隔,如 123,456', list: true },
  { key: 'tmdb_api_key', label: 'TMDB API Key', hint: 'themoviedb.org 免费申请;留空则卡片无元数据', sensitive: true },
  { key: 'proxy_url', label: '代理地址(可选)', hint: '仅 TG/TMDB 走;115 恒直连' },
]

const model = ref({})

onMounted(async () => {
  cfg.value = await api('config')
  const m = {}
  for (const f of FIELDS) {
    const v = cfg.value[f.key]
    m[f.key] = Array.isArray(v) ? v.join(',') : (v ?? '')
  }
  model.value = m
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
</script>

<template>
  <div>
    <div class="page-head">
      <div class="page-title">推送配置</div>
      <div class="page-sub">115 分享 → TMDB 匹配 → 海报卡片 → Telegram 频道</div>
    </div>

    <div class="card">
      <h3>PUSH CHAIN</h3>
      <div class="desc">连接凭据与投递目标;保存后需重启生效</div>
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
  </div>
</template>
