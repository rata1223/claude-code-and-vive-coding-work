import { defineStore } from 'pinia'
import { DEFAULT_SERVER_URL, DEFAULT_THEME } from '@/config'
import { initialLocale, setLocale as applyLocale } from '@/locales'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    serverUrl: localStorage.getItem('serverUrl') || DEFAULT_SERVER_URL,
    theme: localStorage.getItem('theme') || DEFAULT_THEME,
    locale: initialLocale
  }),

  actions: {
    setServerUrl(url) {
      this.serverUrl = url
      if (url) {
        localStorage.setItem('serverUrl', url)
      } else {
        localStorage.removeItem('serverUrl')
      }
    },

    setTheme(theme) {
      this.theme = theme
      localStorage.setItem('theme', theme)
      document.documentElement.setAttribute('data-theme', theme)
    },

    setLocale(locale) {
      this.locale = locale
      applyLocale(locale)
    }
  }
})
