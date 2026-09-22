import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend port can be overridden via VITE_BACKEND_PORT (set automatically by
// the root run.py launcher when --backend-port is customized).
const backendPort = process.env.VITE_BACKEND_PORT || 8000

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
})
