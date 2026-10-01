import { defineStore } from 'pinia'

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
