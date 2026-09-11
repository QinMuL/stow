// 全局配置的分组定义 —— 侧栏下拉与页内切换条**共用这一份**。
// 两处各写一遍必然漂移(改了一处忘了另一处),所以放这里做单一真源。
//
// key 同时是路由参数(`/push/<key>`)与分组标识;顺序即展示顺序。

export const CONFIG_GROUPS = [
  { key: 'creds', label: '基础凭据', desc: 'Bot / 代理 / TMDB / 115 Cookie —— 让系统跑起来的最小集' },
  { key: 'channels', label: '推送与频道', desc: '频道归属与 TG 源频道监控' },
  { key: 'pipeline', label: '资源流水线', desc: '获取 → 处理 → 上传,三段链路' },
  { key: 'sharing', label: '转存与分享', desc: '/save 转存整理建永久分享,与目录监控' },
]

export const DEFAULT_CONFIG_GROUP = 'creds'

export function configGroup(key) {
  return CONFIG_GROUPS.find((g) => g.key === key) || CONFIG_GROUPS[0]
}
