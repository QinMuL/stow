// API 封装:token 注入 + 401 统一跳登录
export function getToken() {
  return localStorage.getItem('stow_token') || ''
}
export function setToken(t) {
  localStorage.setItem('stow_token', t)
}
export function clearToken() {
  localStorage.removeItem('stow_token')
}

export async function api(path, body, method) {
  const r = await fetch('/api/' + path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + getToken(),
    },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (r.status === 401) {
    clearToken()
    if (!location.pathname.startsWith('/login')) location.href = '/login'
    throw new Error('登录已过期')
  }
  const d = await r.json().catch(() => ({ detail: '响应异常' }))
  if (!r.ok) throw new Error(d.detail || r.statusText)
  return d
}

export function timeAgo(ts) {
  const diff = Math.max(0, Date.now() / 1000 - ts)
  if (diff < 60) return '刚刚'
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前'
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前'
  return Math.floor(diff / 86400) + ' 天前'
}
