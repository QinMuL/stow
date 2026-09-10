<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['close'])

const account = ref(null)
const msg = ref({ text: '', kind: '' })
const busy = ref(false)
const form = ref({ current_password: '', new_username: '', new_password: '' })

onMounted(async () => {
  try {
    account.value = await api('account')
  } catch { /* 401 已跳转 */ }
})

async function saveAccount() {
  busy.value = true
  msg.value = { text: '', kind: '' }
  try {
    const d = await api('account', { ...form.value }, 'PUT')
    msg.value = { text: '已修改,当前账号:' + d.username, kind: 'ok' }
    form.value = { current_password: '', new_username: '', new_password: '' }
    account.value = await api('account')
  } catch (e) {
    msg.value = { text: e.message, kind: 'err' }
  } finally {
    busy.value = false
  }
}

async function restart() {
  if (!confirm('确认重启服务?约 5 秒后恢复')) return
  await api('restart', {})
  msg.value = { text: '服务重启中…页面将自动恢复', kind: 'ok' }
  setTimeout(() => location.reload(), 6000)
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal-card">
      <div class="modal-head">
        <div>
          <div class="page-title" style="font-size:17px">账号与服务</div>
          <div class="page-sub">登录凭据与运维操作</div>
        </div>
        <button class="modal-close" @click="emit('close')">✕</button>
      </div>

      <div class="card" style="margin-bottom:12px">
        <h3>ACCOUNT</h3>
        <div class="desc">Web 控制台登录凭据</div>
        <div v-if="account?.default_password" class="msg warn">
          ⚠ 当前仍在使用默认密码 admin/admin,请立即修改
        </div>
        <div v-if="msg.text" class="msg" :class="msg.kind">{{ msg.text }}</div>
        <div class="field">
          <label>当前密码 <code>必填</code></label>
          <input v-model="form.current_password" type="password" autocomplete="current-password">
        </div>
        <div class="grid2">
          <div class="field">
            <label>新用户名 <code>可选</code></label>
            <input v-model="form.new_username" :placeholder="account?.username || ''" autocomplete="username">
          </div>
          <div class="field">
            <label>新密码 <code>可选 · ≥6位</code></label>
            <input v-model="form.new_password" type="password" autocomplete="new-password">
          </div>
        </div>
        <button class="btn ghost" :disabled="busy" @click="saveAccount">修改账号</button>
      </div>

      <div class="card" style="margin-bottom:0">
        <h3>SERVICE</h3>
        <div class="desc">重启会以当前保存的配置重新拉起服务</div>
        <button class="btn danger" @click="restart">重启服务</button>
      </div>
    </div>
  </div>
</template>
