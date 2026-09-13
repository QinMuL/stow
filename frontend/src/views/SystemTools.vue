<script setup>
// 系统工具:运维与手动触发类工具的入口页(工具项按需求逐个加入)
import { onMounted, ref } from 'vue'
import { api } from '../api'

// 工具一:版本检测
const ver = ref(null)        // { current, latest, has_update } | { error }
const checking = ref(false)
const msg = ref('')

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

onMounted(check)
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
          <span class="tool-icon">🔍</span>
          <div class="tool-title">
            <div class="tool-name">版本检测</div>
            <div class="tool-desc">检测 GitHub 发布的最新版本</div>
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
          <button class="btn sm ghost" :disabled="checking" @click="check">
            {{ checking ? '检测中…' : '重新检测' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
