import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// BASE_PATH lets the same build serve from a sub-path, e.g. /redcliff/ on GitHub Pages.
export default defineConfig({
  base: process.env.BASE_PATH || '/',
  plugins: [react()],
  worker: { format: 'es' },
  optimizeDeps: { exclude: ['@duckdb/duckdb-wasm', 'pyodide'] },
})
