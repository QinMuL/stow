import { ref } from 'vue'

/**
 * 页面级操作条:由 AppShell 渲染在顶栏下方(在内容滚动区之外,不遮挡内容),
 * 视图按需设置,离开时清空。
 *
 * bar = { text, hint?, actionText, busy?, action }
 */
export const pageBar = ref(null)

export function setPageBar(bar) {
  pageBar.value = bar
}

export function clearPageBar() {
  pageBar.value = null
}
