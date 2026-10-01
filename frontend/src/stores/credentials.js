import { defineStore } from 'pinia'

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
