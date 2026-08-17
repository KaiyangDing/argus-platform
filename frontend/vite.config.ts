import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// dev 阶段用 Vite 代理转发后端，保持同源、后端零 CORS 配置；
// 生产形态由反代承担同一职责。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/healthz': 'http://localhost:8000',
      '/api': 'http://localhost:8000',
    },
  },
})
