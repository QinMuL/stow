<script setup>
// 系统工具:运维与手动触发类工具的入口页(工具项按需求逐个加入)
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'

// 工具一:版本检测 + 一键升级
const ver = ref(null)        // { current, latest, has_update } | { error }
const checking = ref(false)
const upgrading = ref(false)
const msg = ref('')
let pollTimer = null

async function check() {
  checking.value = true
  msg.value = ''
  try {
    ver.value = await api('tools/version')
  } catch (e) {
    msg.value = { kind: 'err', text: e.message }
  } finally {
    checking.value = false
  }
}

async function upgrade() {
  const target = ver.value?.has_update ? `到 v${ver.value.latest}` : '(拉取 latest 镜像)'
  if (!window.confirm(
    `确认一键升级${target}?\n将拉取新镜像并重建容器,页面会短暂断线。`
  )) return
  upgrading.value = true
  msg.value = { kind: '', text: '升级中:正在拉取新镜像并重建容器…' }
  try {
    await api('tools/upgrade', {}, 'POST')
    pollResult()   // 升级启动后轮询结果,不靠手动刷新猜
  } catch (e) {
    msg.value = { kind: 'err', text: e.message }
    upgrading.value = false
  }
}

// 轮询 /api/tools/upgrade/status:重建期间请求会失败(服务重启),持续重试;
// 读到明确结果(成功/失败)后停止,成功则自动刷新页面。
async function pollResult() {
  window.clearTimeout(pollTimer)
  try {
    const s = await api('tools/upgrade/status')
    if (s.ok === true) {
      msg.value = { kind: 'ok', text: `升级成功 → v${s.to_version},正在刷新页面…` }
      window.setTimeout(() => location.reload(), 800)
      return
    }
    if (s.ok === false) {
      msg.value = { kind: 'err', text: `升级失败:${s.message}` }
      upgrading.value = false
      return
    }
  } catch (e) {
    // 重建进行中(连接断开/尚无结果),静默重试
  }
  pollTimer = window.setTimeout(pollResult, 2500)
}

onMounted(check)
onUnmounted(() => window.clearTimeout(pollTimer))
</script>

<template>
  <div>
    <div class="page-head">
      <div class="page-title">系统工具</div>
      <div class="page-sub">运维与手动触发</div>
    </div>

    <div v-if="msg" class="msg" :class="msg.kind" style="display:block">{{ msg.text }}</div>

    <!-- 工具卡片:一个工具一张小卡片(网格排布,新工具逐个加入) -->
    <div class="tool-grid">
      <div class="tool-card">
        <div class="tool-head">
          <span class="tool-icon">⬆️</span>
          <div class="tool-title">
            <div class="tool-name">版本升级</div>
            <div class="tool-desc">检测最新版本,一键拉取镜像并重建容器</div>
          </div>
        </div>

        <div class="ver-rows">
          <div class="ver-row">
            <span class="ver-k">当前版本</span>
            <span class="ver-v mono">{{ ver?.current || '—' }}</span>
          </div>
          <div class="ver-row">
            <span class="ver-k">最新版本</span>
            <span class="ver-v mono">{{ ver?.latest || (checking ? '检测中…' : '—') }}</span>
          </div>
          <div class="ver-row">
            <span class="ver-k">状态</span>
            <span class="ver-v">
              <template v-if="checking">检测中…</template>
              <template v-else-if="ver?.error">{{ ver.error }}</template>
              <template v-else-if="ver?.has_update">有新版本</template>
              <template v-else>已是最新</template>
            </span>
          </div>
        </div>

        <div class="tool-foot">
          <button class="btn sm ghost" :disabled="checking || upgrading" @click="check">
            {{ checking ? '检测中…' : '重新检测' }}
          </button>
          <!-- 按钮常驻:版本号没变化时也能点,拉取 latest 镜像重建(强制刷新到最新镜像) -->
          <button class="btn sm primary" :disabled="checking || upgrading" @click="upgrade">
            {{ upgrading ? '升级中…' : (ver?.has_update ? `升级到 v${ver.latest}` : '拉取 latest 镜像') }}
          </button>
        </div>

        <div class="tool-note">升级需要容器挂载 docker socket;期间服务短暂不可用</div>
      </div>
    </div>
  </div>
</template>
