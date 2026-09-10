import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { bare: true } },
  {
    path: '/',
    component: () => import('./AppShell.vue'),
    children: [
      { path: '', redirect: '/overview' },
      { path: 'overview', name: 'overview', component: () => import('./views/Overview.vue') },
      { path: 'push', name: 'push', component: () => import('./views/PushConfig.vue') },
      { path: 'logs', name: 'logs', component: () => import('./views/Logs.vue') },
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
