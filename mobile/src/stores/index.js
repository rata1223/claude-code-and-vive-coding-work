// One module per store; this barrel keeps `import { … } from '@/stores'`
// working everywhere. frontend/ and mobile/ carry identical copies of this
// directory (tests/integration/test_frontend_store_parity.py).
export { pinia } from './pinia'
export { useCredentialsStore } from './credentials'
export { useDashboardStore } from './dashboard'
export { useNotificationStore } from './notification'
export { useQuickTradeStore } from './quickTrade'
export { useSettingsStore } from './settings'
export { useStrategyStore } from './strategy'
export { useUserStore } from './user'
export { useWatchlistStore } from './watchlist'
