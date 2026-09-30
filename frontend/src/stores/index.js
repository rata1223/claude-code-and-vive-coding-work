import { createPinia, defineStore } from 'pinia'
import { DEFAULT_SERVER_URL, DEFAULT_THEME } from '@/config'
import { initialLocale, setLocale as applyLocale } from '@/locales'

export const pinia = createPinia()

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

export const useStrategyStore = defineStore('strategy', {
  state: () => ({
    strategies: [],
    loading: false
  }),

  getters: {
    statusCounts: (state) => {
      const counts = { running: 0, stopped: 0, error: 0, total: state.strategies.length }
      state.strategies.forEach((item) => {
        if (item.status === 'running') counts.running++
        else if (item.status === 'stopped') counts.stopped++
        else if (item.status === 'error') counts.error++
      })
      return counts
    },
    runningStrategies: (state) => state.strategies.filter((item) => item.status === 'running'),
    alertStrategies: (state) => state.strategies.filter((item) => item.status === 'error'),
    stoppedStrategies: (state) => state.strategies.filter((item) => item.status === 'stopped')
  },

  actions: {
    setStrategies(list) {
      this.strategies = Array.isArray(list) ? list : []
    },

    updateStrategy(id, patch) {
      const target = this.strategies.find((item) => item.id === id)
      if (target) {
        Object.assign(target, patch)
      }
    },

    setLoading(val) {
      this.loading = val
    }
  }
})

export const useCredentialsStore = defineStore('credentials', {
  state: () => ({
    items: [],
    egressIp: null,
    loading: false
  }),

  getters: {
    hasCredentials: (state) => state.items.length > 0,
    // Quick Trade routes every request through the KIS client, so a Kiwoom
    // credential cannot be used there. Same definition the mobile store
    // already carries — the two had diverged, which is how the web picker
    // came to offer a credential the backend cannot use.
    kisItems: (state) => state.items.filter((item) => item.exchange_id === 'kis')
  },

  actions: {
    setItems(list) {
      this.items = Array.isArray(list) ? list : []
    },

    setEgressIp(data) {
      this.egressIp = data || null
    },

    setLoading(val) {
      this.loading = val
    }
  }
})

const knownNumber = (v) => (v === null || v === undefined ? null : Number(v))

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    summary: null,
    loading: false
  }),

  getters: {
    // Portfolio figures from /api/dashboard/summary. null means unknown (lookup
    // failed or no credential) — never shown as 0; portfolioStatus says why.
    portfolioStatus: (state) => state.summary?.portfolio_status || null,
    portfolioErrors: (state) => state.summary?.portfolio_errors || {},
    assetsKrw: (state) => knownNumber(state.summary?.total_assets_krw),
    assetsUsd: (state) => knownNumber(state.summary?.total_assets_usd),
    profitKrw: (state) => knownNumber(state.summary?.total_profit_krw),
    // KIS evaluation P&L (evlu_pfls_smtl_amt) — KR holdings only; US P&L is
    // not parsed yet (#178), so with any US position the total is unknown.
    unrealizedPnl: (state) => {
      const us = state.summary?.us_positions
      if (!Array.isArray(us) || us.length) return null
      return knownNumber(state.summary?.total_profit_krw)
    },
    positions: (state) => {
      const kr = state.summary?.kr_positions
      const us = state.summary?.us_positions
      if (!Array.isArray(kr) && !Array.isArray(us)) return null
      return [...(Array.isArray(kr) ? kr : []), ...(Array.isArray(us) ? us : [])]
    },
    recentTrades: (state) => Array.isArray(state.summary?.recent_trades) ? state.summary.recent_trades : [],
    // null when there is nothing to divide (no closed or no losing trade)
    winRate: (state) => knownNumber(state.summary?.performance?.win_rate),
    totalTrades: (state) => knownNumber(state.summary?.performance?.total_trades),
    profitFactor: (state) => knownNumber(state.summary?.performance?.profit_factor),
    // No source: the API has no per-day P&L for the account. Unknown, not 0.
    todayPnl: () => null
  },

  actions: {
    setSummary(data) {
      this.summary = data
    },

    setLoading(val) {
      this.loading = val
    }
  }
})

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

export const useNotificationStore = defineStore('notification', {
  state: () => ({
    notifications: [],
    unreadCount: 0
  }),

  actions: {
    setNotifications(list) {
      this.notifications = Array.isArray(list) ? list : []
      this.unreadCount = this.notifications.filter((item) => !item.is_read && !item.read).length
    },

    setUnreadCount(count) {
      this.unreadCount = Number(count || 0)
    },

    markAsRead(id) {
      const notification = this.notifications.find((item) => item.id === id)
      if (notification && !notification.is_read && !notification.read) {
        notification.is_read = 1
        notification.read = true
        this.unreadCount = Math.max(0, this.unreadCount - 1)
      }
    },

    markAllAsRead() {
      this.notifications.forEach((item) => {
        item.is_read = 1
        item.read = true
      })
      this.unreadCount = 0
    }
  }
})

export const useWatchlistStore = defineStore('watchlist', {
  state: () => ({
    items: [],
    activeSymbol: localStorage.getItem('watchlist_active_symbol') || '',
    activeMarket: localStorage.getItem('watchlist_active_market') || 'Crypto',
    loading: false
  }),

  getters: {
    activeItem: (state) => state.items.find((i) => i.symbol === state.activeSymbol) || null
  },

  actions: {
    setItems(list) {
      this.items = Array.isArray(list) ? list : []
      if (!this.activeSymbol && this.items.length > 0) {
        const first = this.items.find((i) => (i.market || '').toLowerCase() === 'crypto') || this.items[0]
        if (first) {
          this.activeSymbol = first.symbol
          this.activeMarket = first.market || 'Crypto'
          localStorage.setItem('watchlist_active_symbol', this.activeSymbol)
          localStorage.setItem('watchlist_active_market', this.activeMarket)
        }
      }
    },

    setActive(symbol, market) {
      this.activeSymbol = symbol || ''
      if (market) this.activeMarket = market
      if (symbol) localStorage.setItem('watchlist_active_symbol', symbol)
      else localStorage.removeItem('watchlist_active_symbol')
      if (market) localStorage.setItem('watchlist_active_market', market)
    },

    setLoading(val) {
      this.loading = val
    }
  }
})

export const useQuickTradeStore = defineStore('quickTrade', {
  state: () => ({
    selectedCredentialId: null,
    marketType: 'spot',
    balance: null,
    positions: [],
    history: [],
    loading: false
  }),

  actions: {
    setSelectedCredential(id) {
      this.selectedCredentialId = id || null
    },

    setMarketType(type) {
      this.marketType = type || 'spot'
    },

    setBalance(data) {
      this.balance = data || null
    },

    setPositions(list) {
      this.positions = Array.isArray(list) ? list : []
    },

    setHistory(list) {
      this.history = Array.isArray(list) ? list : []
    },

    setLoading(val) {
      this.loading = val
    }
  }
})

export default pinia
