import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The judge-facing frontend (docs/FRONTEND_PLAN.md). It reads the old dashboard's API client, mock and hooks WITHOUT editing them (alias @old), and runs on
// its own port so the old dashboard on 5173 stays exactly as it is. The proxy is the same as the old dashboard's, aimed at the dev server on 8000.
const old = path.resolve(__dirname, '../dashboard/src')
const API = process.env.MUNIM_API ?? 'http://localhost:8000'            // a second backend (for example a recording copy) can be aimed at with MUNIM_API and MUNIM_WEB_PORT
const PORT = Number(process.env.MUNIM_WEB_PORT ?? 5174)
const PROXY = {
  '/api': API,
  '/approvals': API,
  '/ws': { target: API.replace(/^http/, 'ws'), ws: true },
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@old': old },
    dedupe: ['react', 'react-dom'],           // one React for this app AND the old modules it imports, or hooks and context break
  },
  server: {
    port: PORT,
    strictPort: true,
    fs: { allow: [path.resolve(__dirname, '..')] },
    proxy: PROXY,
  },
  // `npm run build && npm run preview` serves the built app: nothing recompiles during the demo, and new files can never leave stale CSS behind (the dev server
  // needs a restart after files are added). The same proxy, because preview has its own setting.
  preview: { port: PORT, strictPort: true, proxy: PROXY },
})
