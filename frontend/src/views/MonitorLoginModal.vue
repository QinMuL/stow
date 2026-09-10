<script setup>
import { ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['done', 'close'])

const phone = ref('')
const code = ref('')
const password = ref('')
const stage = ref('phone')   // phone → code → password(两步验证)
const msg = ref({ text: '', kind: '' })
const busy = ref(false)

async function call(path, body) {
  busy.value = true
  msg.value = { text: '', kind: '' }
  try {
    const d = await api('monitor/login/' + path, body)
    msg.value = { text: d.message, kind: d.success ? 'ok' : 'err' }
    if (d.success) {
      if (d.login_stage === 'password') stage.value = 'password'
      else if (!d.login_stage) { emit('done', d); return }
      else stage.value = 'code'
    }
  } catch (e) {
    msg.value = { text: e.message, kind: 'err' }
  } finally {
    busy.value = false
  }
}

const sendCode = () => call('start', { phone: phone.value })
const sendCodeAgain = () => call('start', { phone: phone.value })
const submitCode = () => call('code', { code: code.value })
const submitPassword = () => call('password', { password: password.value })

async function cancel() {
  try { await api('monitor/login/cancel', {}) } catch { /* 关闭即可 */ }
  emit('close')
}
</script>

<template>
  <div class="modal-mask" @click.self="cancel">
    <div class="modal-card" style="width:min(440px,100%)">
      <div class="modal-head">
        <div>
          <div class="page-title" style="font-size:17px">登录监控账号</div>
          <div class="page-sub">用于监听源频道,登录一次即长期有效(会话存在容器数据目录)</div>
        </div>
        <button class="modal-close" @click="cancel">✕</button>
      </div>

      <div v-if="stage === 'phone'" class="field">
        <label>手机号(含国家码)</label>
        <input v-model="phone" placeholder="+8613800138000" autocomplete="off" @keyup.enter="sendCode">
        <div class="hint">验证码会发到该账号的 Telegram 客户端(未登录设备则是短信)</div>
      </div>

      <div v-else-if="stage === 'code'" class="field">
        <label>登录验证码</label>
        <input v-model="code" placeholder="Telegram 里收到的 5 位验证码" autocomplete="off" @keyup.enter="submitCode">
        <div class="hint">收不到?点下方「重新发送验证码」</div>
      </div>

      <div v-else class="field">
        <label>两步验证密码</label>
        <input v-model="password" type="password" placeholder="该账号的两步验证密码" autocomplete="off" @keyup.enter="submitPassword">
        <div class="hint">该账号开启了两步验证,需再输入一次密码</div>
      </div>

      <div v-if="msg.text" class="msg" :class="msg.kind">{{ msg.text }}</div>

      <div class="actions" style="margin-top:14px">
        <button v-if="stage === 'phone'" class="btn primary" style="flex:1" :disabled="busy" @click="sendCode">
          发送验证码
        </button>
        <template v-else-if="stage === 'code'">
          <button class="btn primary" style="flex:1" :disabled="busy" @click="submitCode">登录</button>
          <button class="btn ghost" :disabled="busy" @click="sendCodeAgain">重新发送验证码</button>
        </template>
        <button v-else class="btn primary" style="flex:1" :disabled="busy" @click="submitPassword">
          提交并登录
        </button>
        <button class="btn ghost" :disabled="busy" @click="cancel">取消</button>
      </div>
    </div>
  </div>
</template>
