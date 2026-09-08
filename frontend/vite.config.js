import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 产物直接输出到后端静态目录(FastAPI 托管);构建产物随 git 提交,
// 镜像构建无需 Node 阶段(远程 Actions 也只跑 Python 层)。
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8686',
    },
  },
})
