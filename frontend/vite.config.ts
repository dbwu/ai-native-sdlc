import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/workbench/',
  plugins: [vue()],
  build: { outDir: '../knowledge/web', emptyOutDir: true },
  server: { proxy: { '/workbench/projects': 'http://127.0.0.1:8000', '/workbench/workflows': 'http://127.0.0.1:8000', '/workbench/proposals': 'http://127.0.0.1:8000', '/entities': 'http://127.0.0.1:8000', '/workflows': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000' } },
})
