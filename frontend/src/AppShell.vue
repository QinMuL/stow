<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, clearToken } from './api'
import { CONFIG_GROUPS } from './configGroups'
import { pageBar } from './pagebar'
import AccountModal from './views/AccountModal.vue'

const route = useRoute()
const router = useRouter()
const status = ref(null)
const version = computed(() => status.value?.version ? 'v' + status.value.version : '—')
const showAccount = ref(false)

// 「全局配置」是父项:点它只展开子菜单、不导航(2026-09-12 用户要求),
// 子项才是真正的分组页 /push/<key>。子项定义与页内切换条共用 configGroups.js。
const nav = [
  { to: '/overview', ic: '◉', label: '系统总览' },
  { to: '/push', ic: '✦', label: '全局配置',
    children: CONFIG_GROUPS.map((g) => ({ to: `/push/${g.key}`, label: g.label })) },
  { to: '/logs', ic: '≡', label: '系统日志' },
  { to: '/tools', ic: '⚙', label: '系统工具' },
]

// 展开状态:用户手动开合过才记,否则跟随当前路由(在配置页里就自动展开)
const openOverride = ref({})
function isOpen(n) {
  return openOverride.value[n.to] ?? route.path.startsWith(n.to)
}
function onNavClick(n) {
  if (!n.children) {
    router.push(n.to)
    return
  }
  // 窄屏没地方放子项(≤900px 侧栏收成纯图标、手机是底部导航)→ 直接进默认分组
  if (window.matchMedia('(max-width: 900px)').matches) {
    router.push(n.to)
    return
  }
  openOverride.value[n.to] = !isOpen(n)
}
// 离开某个父项的子树后清掉手动状态,下次进来重新跟随路由
watch(() => route.path, (p) => {
  for (const n of nav) {
    if (n.children && !p.startsWith(n.to)) delete openOverride.value[n.to]
  }
})

// 系统健康指示已收敛到「系统总览」页内那一行(2026-09-11 用户确认取消顶栏那份)。
// 顶栏这份原先只在页面挂载时取一次、不轮询,状态会陈旧;还多占一条全局视线的位置。
// 这里保留 status 只为侧栏版本号(version)。
async function refresh() {
  try {
    status.value = await api('status')
  } catch { /* 401 已自动跳转 */ }
}

function logout() {
  clearToken()
  router.push('/login')
}

onMounted(refresh)
</script>

<template>
  <div class="shell">
    <aside class="side">
      <div class="logo">
        <div class="logo-mark">S</div>
        <div>
          <div class="logo-name">STOW</div>
          <div class="logo-sub">media manager</div>
        </div>
      </div>
      <nav class="nav">
        <template v-for="n in nav" :key="n.to">
          <button v-if="n.children" class="nav-item" :class="{ active: route.path.startsWith(n.to) }"
            @click="onNavClick(n)">
            <span class="ic">{{ n.ic }}</span><span class="txt">{{ n.label }}</span>
            <span class="caret">{{ isOpen(n) ? '▾' : '▸' }}</span>
          </button>
          <router-link v-else class="nav-item" :class="{ active: route.path.startsWith(n.to) }" :to="n.to">
            <span class="ic">{{ n.ic }}</span><span class="txt">{{ n.label }}</span>
          </router-link>
          <div v-if="n.children && isOpen(n)" class="subnav">
            <router-link v-for="c in n.children" :key="c.to" class="subnav-item"
              :class="{ active: route.path === c.to }" :to="c.to">{{ c.label }}</router-link>
          </div>
        </template>
      </nav>
      <div class="side-foot">{{ version }} · amber</div>
    </aside>

    <div class="main">
      <div class="topbar">
        <!-- 手机端:项目标识在顶栏左侧(底部导航只留四个菜单项)。
             两份标记是有意的——CSS 没法把一个节点搬进另一个容器,按断点各显一份最稳 -->
        <div class="logo top-logo">
          <div class="logo-mark">S</div>
          <div class="logo-name">STOW</div>
        </div>
        <div class="top-right">
          <button class="link-btn who-btn" title="账号与服务" @click="showAccount = true">👤 <b>admin</b></button>
          <button class="link-btn" @click="logout">退出</button>
        </div>
      </div>
      <!-- 页面级操作条:在滚动区之外,出现/消失都不会遮挡内容(如"保存并重启") -->
      <div v-if="pageBar" class="page-bar">
        <span class="pb-dot"></span>
        <span class="pb-text">{{ pageBar.text }}</span>
        <span v-if="pageBar.hint" class="pb-hint">{{ pageBar.hint }}</span>
        <button class="btn primary" :disabled="pageBar.busy" @click="pageBar.action()">
          {{ pageBar.busy ? '处理中…' : pageBar.actionText }}
        </button>
      </div>
      <div class="content">
        <div class="page">
          <router-view @refresh-status="refresh" />
        </div>
      </div>
    </div>

    <AccountModal v-if="showAccount" @close="showAccount = false" />
  </div>
</template>
