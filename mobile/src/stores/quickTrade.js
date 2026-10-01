import { defineStore } from 'pinia'

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
