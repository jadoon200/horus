import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Default to 5199 (ARGUS dev uses 5173, PHAROS 5188) but honor a PORT env var so
// preview/launch tooling can place the dev server on an assigned port.
export default defineConfig({
  plugins: [react()],
  server: { port: process.env.PORT ? Number(process.env.PORT) : 5199 },
})
