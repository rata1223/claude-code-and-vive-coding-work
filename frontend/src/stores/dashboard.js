import { defineStore } from 'pinia'

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
    recentOrders: (state) => Array.isArray(state.summary?.recent_orders) ? state.summary.recent_orders : [],
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
