import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api'
import { DEFAULT_CONFIG_GROUP } from './configGroups'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { bare: true } },
  {
    path: '/',
    component: () => import('./AppShell.vue'),
    children: [
      { path: '', redirect: '/overview' },
      { path: 'overview', name: 'overview', component: () => import('./views/Overview.vue') },
      // 全局配置按分组分页:一条路由 + 一个组件,切换分组时组件实例不销毁
      // → 未保存的改动不会因为换页而丢(组件重建就会丢)
      { path: 'push', redirect: `/push/${DEFAULT_CONFIG_GROUP}` },
      { path: 'push/:group', name: 'push', component: () => import('./views/PushConfig.vue') },
      { path: 'logs', name: 'logs', component: () => import('./views/Logs.vue') },
      { path: 'tools', name: 'tools', component: () => import('./views/SystemTools.vue') },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/overview' }, // 旧链接(/system 等)回总览
]

export const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (to.path !== '/login' && !getToken()) {
    return { path: '/login' }
  }
})
