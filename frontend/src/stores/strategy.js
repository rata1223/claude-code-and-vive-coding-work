import { defineStore } from 'pinia'

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
