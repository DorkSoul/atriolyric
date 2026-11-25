import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'
import { defineConfig, type Plugin } from 'vite'

// The app version lives in package.json (bump it with every release). It is
// baked into the bundle as __APP_VERSION__ and written to dist/version.json,
// which the server reports at /api/version so an open app can tell it is out
// of date and offer a restart.
const appVersion: string = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf8'),
).version

function versionFile(): Plugin {
  return {
    name: 'atriolyric-version-file',
    apply: 'build',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'version.json',
        source: `${JSON.stringify({ version: appVersion })}\n`,
      })
    },
  }
}

/** dist/asset-list.json: every built file under /assets/. The Android app
 * fetches the ones it does not hold yet, so each view opens with no
 * connection (web/src/main.tsx, mobile's WebCache.kt). */
function assetList(): Plugin {
  return {
    name: 'atriolyric-asset-list',
    apply: 'build',
    generateBundle(_options, bundle) {
      const paths = Object.keys(bundle).filter((name) => name.startsWith('assets/')).sort().map((name) => `/${name}`)
      this.emitFile({ type: 'asset', fileName: 'asset-list.json', source: `${JSON.stringify(paths)}\n` })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), versionFile(), assetList()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  server: {
    // Localhost only by default: the dev server serves source files and has
    // no auth. Set ATRIOLYRIC_DEV_HOST=0.0.0.0 to bind every interface when
    // a published dev-stack port (e.g. through the tunnel) has to reach it.
    host: process.env.ATRIOLYRIC_DEV_HOST || '127.0.0.1',
    port: 3000,
    proxy: {
      '/api': 'http://127.0.0.1:8321',
      '/ws': {
        target: 'ws://127.0.0.1:8321',
        ws: true,
      },
    },
  },
})
