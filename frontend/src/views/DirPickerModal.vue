<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const props = defineProps({
  // '115' = 网盘目录(CID) / 'openlist' = openlist 目录(路径串)
  source: { type: String, default: '115' },
})
const emit = defineEmits(['pick', 'close'])

const ROOT = {
  '115': { value: 0, name: '网盘根目录' },
  openlist: { value: '/', name: 'openlist 根' },
}
const TITLE = { '115': '选择网盘目录', openlist: '选择 openlist 目录' }

const stack = ref([{ ...(ROOT[props.source] || ROOT['115']) }])
const items = ref([])
const loading = ref(false)
const err = ref('')
const isOpenList = props.source === 'openlist'

const current = () => stack.value[stack.value.length - 1]

async function open(value) {
  loading.value = true
  err.value = ''
  try {
    if (isOpenList) {
      const d = await api('openlist/dirs?path=' + encodeURIComponent(value))
      items.value = d.items.map(it => ({ value: it.path, name: it.name }))
    } else {
      const d = await api('115/dirs?cid=' + encodeURIComponent(value))
      items.value = d.items.map(it => ({ value: it.cid, name: it.name }))
    }
  } catch (e) {
    err.value = e.message
    items.value = []
  } finally {
    loading.value = false
  }
}

function enter(it) {
  stack.value.push({ value: it.value, name: it.name })
  open(it.value)
}

function back(idx) {
  stack.value = stack.value.slice(0, idx + 1)
  open(current().value)
}

function pick() {
  // 统一用 cid 字段回传(115 是 CID,openlist 是路径),调用方无需区分
  emit('pick', { cid: current().value, name: current().name })
  emit('close')
}

onMounted(() => open(current().value))
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal-card" style="width:min(480px,100%)">
      <div class="modal-head">
        <div>
          <div class="page-title" style="font-size:17px">{{ TITLE[source] || TITLE['115'] }}</div>
          <div class="page-sub">
            <template v-for="(s, i) in stack" :key="s.value">
              <span class="crumb" :class="{ cur: i === stack.length - 1 }" @click="i < stack.length - 1 && back(i)">{{ s.name }}</span>
              <span v-if="i < stack.length - 1" class="crumb-sep">/</span>
            </template>
          </div>
        </div>
        <button class="modal-close" @click="emit('close')">✕</button>
      </div>

      <div v-if="err" class="msg err">{{ err }}</div>
      <div class="dir-list">
        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!items.length" class="empty">没有子目录</div>
        <div v-for="it in items" :key="it.value" class="dir-row" @click="enter(it)">
          📁 <span>{{ it.name }}</span>
        </div>
      </div>

      <div class="actions" style="margin-top:14px">
        <button class="btn primary" style="flex:1" @click="pick">
          选择当前目录({{ current().value }})
        </button>
      </div>
    </div>
  </div>
</template>
