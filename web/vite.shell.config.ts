import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'

// The page bundled into the Android app (mobile/): the server set-up screen
// (the app itself is the web app from the user's server). Shown until that server
// answers, then loads the real web app from that server. Built with
// `npm run build:shell` into mobile/shell-dist, which `cap sync` copies into
// the APK. Relative asset paths: it is served from inside the APK.
const appVersion: string = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf8'),
).version

export default defineConfig({
  root: 'shell',
  base: './',
  publicDir: false,
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  build: {
    outDir: '../../mobile/shell-dist',
    emptyOutDir: true,
  },
})
