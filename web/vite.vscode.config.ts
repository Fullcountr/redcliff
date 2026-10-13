import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The VS Code webview bundle: one script and one stylesheet with fixed names, so the extension
// can point at them; assets (fonts) are referenced relative to the stylesheet.
export default defineConfig({
  base: './',
  publicDir: false,
  plugins: [react()],
  build: {
    outDir: '../vscode/media',
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: 'src/vscode/main.tsx',
      output: {
        entryFileNames: 'webview.js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: (info) => (info.names?.[0]?.endsWith('.css') ? 'webview.css' : 'assets/[name]-[hash][extname]'),
      },
    },
  },
})
