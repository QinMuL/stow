<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['pick', 'close'])

const stack = ref([{ cid: 0, name: '网盘根目录' }])
const items = ref([])
const loading = ref(false)
const err = ref('')

const current = () => stack.value[stack.value.length - 1]

async function open(cid) {
  loading.value = true
  err.value = ''
  try {
    const d = await api(`115/dirs?cid=${cid}`)
    items.value = d.items
  } catch (e) {
    err.value = e.message
    items.value = []
  } finally {
    loading.value = false
  }
}

function enter(it) {
  stack.value.push({ cid: it.cid, name: it.name })
  open(it.cid)
}

function back(idx) {
  stack.value = stack.value.slice(0, idx + 1)
  open(current().cid)
}

function pick() {
  emit('pick', { cid: current().cid, name: current().name })
  emit('close')
}

onMounted(() => open(0))
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal-card" style="width:min(480px,100%)">
      <div class="modal-head">
        <div>
          <div class="page-title" style="font-size:17px">选择网盘目录</div>
          <div class="page-sub">
            <template v-for="(s, i) in stack" :key="s.cid">
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
        <div v-else-if="!items.length" class="empty">空目录</div>
        <div v-for="it in items" :key="it.cid" class="dir-row" @click="enter(it)">
          📁 <span>{{ it.name }}</span>
        </div>
      </div>

      <div class="actions" style="margin-top:14px">
        <button class="btn primary" style="flex:1" @click="pick">
          选择当前目录({{ current().cid }})
        </button>
      </div>
    </div>
  </div>
</template>
