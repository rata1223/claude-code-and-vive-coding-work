import { defineStore } from 'pinia'
import { useCredentialsStore } from './credentials'
import { useDashboardStore } from './dashboard'
import { useNotificationStore } from './notification'
import { useQuickTradeStore } from './quickTrade'
import { useStrategyStore } from './strategy'
import { useWatchlistStore } from './watchlist'

export const useUserStore = defineStore('user', {
  state: () => ({
    token: localStorage.getItem('token') || '',
    userInfo: null,
    isLoggedIn: !!localStorage.getItem('token')
  }),

  actions: {
    setToken(token) {
      this.token = token
      this.isLoggedIn = !!token
      if (token) {
        localStorage.setItem('token', token)
      } else {
        localStorage.removeItem('token')
      }
    },

    setUserInfo(info) {
      this.userInfo = info
    },

    logout() {
      this.token = ''
      this.userInfo = null
      this.isLoggedIn = false
      localStorage.removeItem('token')
      // Account-scoped stores live in memory until a reload; without this the
      // next account to sign in on this device sees the previous one's
      // balances and strategies until its own data arrives. Settings are
      // per-device and stay.
      for (const useStore of [useDashboardStore, useStrategyStore, useCredentialsStore,
        useNotificationStore, useWatchlistStore, useQuickTradeStore]) {
        useStore().$reset()
      }
    }
  }
})
