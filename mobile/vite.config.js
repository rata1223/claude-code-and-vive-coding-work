import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { VantResolver } from '@vant/auto-import-resolver'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const pkgVersion = JSON.parse(readFileSync(resolve(__dirname, 'package.json'), 'utf-8')).version

// The app API (dev server only). It used to point at the upstream project's
// host, so a dev login went to a third-party server.
const apiTarget = process.env.VITE_API_TARGET || 'http://localhost:8000'
// kis-ws (operator live feed), dev server only: the built app reaches /socket.io on
// the server it is configured with (ServerConfig).
const wsTarget = process.env.VITE_WS_TARGET || 'http://localhost:5002'

// https://vitejs.dev/config/
export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(pkgVersion)
  },
  plugins: [
    vue(),
    AutoImport({
      resolvers: [VantResolver()],
      imports: ['vue', 'vue-router', 'pinia'],
      dts: true
    }),
    Components({
      resolvers: [VantResolver()]
    })
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    // 代理后端 API（与 PC 端共用 5000 端口）
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true
      },
      '/socket.io': {
        target: wsTarget,
        ws: true,
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets'
  }
})
