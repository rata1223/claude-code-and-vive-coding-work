import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { pinia, useSettingsStore } from './stores'
import i18n from './locales'

import 'vant/lib/index.css'
import './styles/index.css'

import { Capacitor } from '@capacitor/core'
import { App as CapApp } from '@capacitor/app'
import { StatusBar, Style } from '@capacitor/status-bar'
import { SplashScreen } from '@capacitor/splash-screen'

const app = createApp(App)

app.use(pinia)
app.use(i18n)
app.use(router)

const settingsStore = useSettingsStore()

const applyThemeAttr = (theme) => {
  document.documentElement.setAttribute('data-theme', theme || 'dark')
}

const syncStatusBar = async (theme) => {
  if (!Capacitor.isNativePlatform()) return
  try {
    await StatusBar.setOverlaysWebView({ overlay: false })
    if (theme === 'light') {
      await StatusBar.setStyle({ style: Style.Light })
      await StatusBar.setBackgroundColor({ color: '#ffffff' })
    } else {
      await StatusBar.setStyle({ style: Style.Dark })
      await StatusBar.setBackgroundColor({ color: '#000000' })
    }
  } catch (e) {
    console.warn('StatusBar not available:', e)
  }
}

applyThemeAttr(settingsStore.theme)

settingsStore.$subscribe((_mutation, state) => {
  applyThemeAttr(state.theme)
  syncStatusBar(state.theme)
})

const initCapacitor = async () => {
  if (!Capacitor.isNativePlatform()) return

  await syncStatusBar(settingsStore.theme)

  try {
    await SplashScreen.hide()
  } catch (e) {
    console.warn('SplashScreen not available:', e)
  }

  CapApp.addListener('backButton', ({ canGoBack }) => {
    if (canGoBack) {
      router.back()
    } else {
      CapApp.exitApp()
    }
  })
}

app.mount('#app')

router.isReady().then(() => {
  initCapacitor()
})

app.config.errorHandler = (err, vm, info) => {
  console.error('Global error:', err, info)
}
