<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, setToken } from '../api'

const router = useRouter()
const username = ref('admin')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  busy.value = true
  try {
    const d = await api('login', { username: username.value.trim(), password: password.value })
    setToken(d.token)
    router.push('/overview')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="bare">
    <div class="login-card">
      <div class="brand">
        <div class="logo-mark">S</div>
        <div>
          <div class="logo-name">STOW</div>
          <div class="logo-sub">media manager</div>
        </div>
      </div>
      <div class="card">
        <h3>登录控制台</h3>
        <div class="desc">首次部署默认账号 admin / admin</div>
        <div v-if="error" class="msg err">{{ error }}</div>
        <div class="field">
          <label>用户名</label>
          <input v-model="username" autocomplete="username" @keyup.enter="submit">
        </div>
        <div class="field">
          <label>密码</label>
          <input v-model="password" type="password" autocomplete="current-password" @keyup.enter="submit">
        </div>
        <button class="btn primary" style="width:100%" :disabled="busy" @click="submit">
          {{ busy ? '登录中…' : '登 录' }}
        </button>
      </div>
    </div>
  </div>
</template>
