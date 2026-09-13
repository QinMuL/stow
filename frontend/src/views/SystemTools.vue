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
let pollStartedAt = 0
let sawDown = false        // 轮询期间断过线 = 容器重建中
let versionBefore = ''     // 触发升级时的当前版本号

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
  const target = ver.value?.has_update ? `到 v${ver.value.latest}` : '(拉取 latest 镜像重建)'
  if (!window.confirm(
    `确认一键升级${target}?\n由 Watchtower 拉取新镜像并重建容器,页面会短暂断线。`
  )) return
  upgrading.value = true
  versionBefore = ver.value?.current || ''
  sawDown = false
  msg.value = { kind: '', text: '升级中:正在拉取新镜像并重建容器…' }
  try {
    const d = await api('tools/upgrade', {}, 'POST')
    msg.value = { kind: '', text: d.message }
    pollStartedAt = Date.now()
    pollUpgrade()   // 触发成功后轮询感知完成,不靠手动刷新猜
  } catch (e) {
    msg.value = { kind: 'err', text: e.message }
    upgrading.value = false
  }
}

// 感知升级完成:Watchtower 在后台拉镜像 → 停旧容器 → 原配置重建 → 启动。
// 期间版本接口会断(sawDown),恢复后再请求成功即完成;若版本号直接变化
// (没捕捉到断线)也算完成。最长等 5 分钟。
async function pollUpgrade() {
  window.clearTimeout(pollTimer)
  if (Date.now() - pollStartedAt > 5 * 60 * 1000) {
    msg.value = { kind: 'err', text: '升级等待超时,请刷新页面查看当前版本' }
    upgrading.value = false
    return
  }
  try {
    const v = await api('tools/version')
    if (sawDown) {
      const now = v.current || ''
      const changed = now && now !== versionBefore
      msg.value = {
        kind: 'ok',
        text: changed
          ? `升级成功:v${versionBefore} → v${now},正在刷新页面…`
          : `已用最新镜像重建完成(当前 v${now}),正在刷新页面…`,
      }
      window.setTimeout(() => location.reload(), 900)
      return
    }
    if (v.current && v.current !== versionBefore) {
      msg.value = { kind: 'ok', text: `升级成功:v${versionBefore} → v${v.current},正在刷新页面…` }
      window.setTimeout(() => location.reload(), 900)
      return
    }
    // 版本没变且没断过线:还在拉镜像,继续等
  } catch (e) {
    sawDown = true   // 重建中:连接中断
  }
  pollTimer = window.setTimeout(pollUpgrade, 3000)
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
            <div class="tool-desc">检测最新版本,由 Watchtower 拉取镜像并重建容器</div>
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

        <div class="tool-note">升级由 compose 里的 stow-watchtower 容器执行;期间服务短暂不可用,页面自动感知刷新</div>
      </div>
    </div>
  </div>
</template>
