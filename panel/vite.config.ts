import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The panel talks to the API on the same origin, under /api. In a deployed installation,
// Caddy (config/caddy/panel.caddy) strips the /api prefix and forwards to the API service
// (port 8001). In development the Vite proxy does the same job. CONNECT_API_URL changes the
// target (e.g. an API running on another machine).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  const apiTarget = env.CONNECT_API_URL || 'http://127.0.0.1:8001'

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        // Regex: matches only /api and /api/..., never panel routes that merely start with "api".
        '^/api(/|$)': {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, '') || '/',
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
    },
  }
})
