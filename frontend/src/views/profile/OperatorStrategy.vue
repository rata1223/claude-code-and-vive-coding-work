<template>
  <div class="operator-page">
    <van-nav-bar :title="$t('operator.title')" left-arrow @click-left="$router.back()" />

    <p class="notice">{{ $t('operator.notice') }}</p>

    <!-- Runs -->
    <div class="section">
      <div class="section-head">
        <span class="section-title">{{ $t('operator.runs') }}</span>
        <van-button size="mini" plain :loading="loading" @click="load">{{ $t('operator.refresh') }}</van-button>
      </div>
      <div v-if="loadError" class="empty error">{{ loadError }}</div>
      <div v-else-if="!loading && runs.length === 0" class="empty">{{ $t('operator.no_runs') }}</div>
      <div v-for="run in runs" :key="run.id" class="run-card" :class="{ occupying: run.occupying }">
        <div class="run-top">
          <span class="run-name">#{{ run.id }} {{ run.name }}</span>
          <van-tag v-if="run.occupying" type="success">{{ run.is_active ? $t('operator.running') : $t('operator.stopping') }}</van-tag>
          <van-tag v-else plain>{{ $t('operator.stopped') }}</van-tag>
        </div>
        <div class="run-line">{{ $t('operator.started_at') }}: {{ formatTime(run.started_at) }}</div>
        <div v-if="run.stopped_at" class="run-line">{{ $t('operator.stopped_at') }}: {{ formatTime(run.stopped_at) }}</div>
        <div class="run-line">{{ $t('operator.run_days', { days: run.run_days ?? '—' }) }}</div>
        <!-- The 4-week gate counts the time the run was actually running, not calendar days: downtime moves the gate date later. -->
        <div v-if="run.uptime_days != null" class="run-line">
          {{ $t('operator.uptime_line', { days: run.uptime_days, hours: run.downtime_hours ?? '—' }) }}
        </div>
        <div class="run-line">
          {{ $t('operator.env_fills', { env: envLabel(run.kis_env), n: run.filled_orders ?? 0 }) }}
          <!-- The worker's ENABLE_LIVE_TRADING at its last start: shadow submits nothing, so it never fills. -->
          <span v-if="run.orders_enabled === false" class="shadow">· {{ $t('operator.shadow') }}</span>
        </div>
        <div class="run-line gate" :class="{ met: run.paper_gate_met }">
          {{ gateText(run) }}
        </div>
        <!-- Any run holding the slot, "Stopping" included. If the worker has
             no session for it (down when the stop arrived, write failed),
             sending it again records a zero-day end and frees the slot; if the
             run is still ending, the worker frees the slot only once it has. -->
        <van-button
          v-if="run.occupying"
          type="danger"
          size="small"
          block
          :loading="stopping === run.id"
          class="run-stop"
          @click="stopRun(run)"
        >
          {{ $t('operator.stop') }}
        </van-button>
      </div>
    </div>

    <!-- Live feed: kis-ws relays the worker's order updates and alerts (operators only). -->
    <div class="section">
      <div class="section-head">
        <span class="section-title">{{ $t('operator.live') }}</span>
        <van-tag :type="liveStatus === 'connected' ? 'success' : 'default'" plain>{{ liveLabel() }}</van-tag>
      </div>
      <div v-if="liveStatus === 'refused' || liveStatus === 'closed'" class="empty">
        {{ liveStatus === 'refused' ? $t('operator.live_hint_refused') : $t('operator.live_hint_closed') }}
        <van-button size="mini" plain class="live-retry" @click="openFeed">{{ $t('operator.live_retry') }}</van-button>
      </div>
      <!-- Account state from the worker (positions, equity). Unknown numbers are
           "—", never 0: a 0 would read as an empty account. -->
      <div v-if="equity || positions" class="account-card">
        <div class="account-top">
          <span>{{ $t('operator.account_total') }}</span>
          <b>{{ $t('operator.account_krw', { v: num(equity && equity.total_eval_krw) }) }}</b>
        </div>
        <van-tag v-if="equity && equity.equity_verified === false" type="warning" plain class="account-unverified">
          {{ $t('operator.account_unverified') }}
        </van-tag>
        <div class="account-line">
          {{ $t('operator.account_cash', { krw: num(equity && equity.cash_krw), usd: num(equity && equity.cash_usd) }) }}
        </div>
        <div v-if="positions && positions.length === 0" class="account-line">{{ $t('operator.account_no_positions') }}</div>
        <div v-for="p in positions || []" :key="p.symbol + ':' + p.market" class="account-pos">
          <span class="pos-symbol">{{ p.symbol }}</span>
          <span>{{ $t('operator.account_qty', { v: num(p.qty) }) }}</span>
          <span>{{ $t('operator.account_avg', { v: num(p.avg_price) }) }}</span>
          <span>{{ $t('operator.account_now', { v: num(p.current_price) }) }}</span>
        </div>
        <!-- Two times: equity and positions are read and published separately,
             and one can keep failing while the other refreshes. -->
        <div class="account-line account-at">
          {{ $t('operator.account_equity_as_of', { at: formatTime(equityAt) }) }} ·
          {{ $t('operator.account_positions_as_of', { at: formatTime(positionsAt) }) }}
        </div>
      </div>
      <div v-if="events.length === 0" class="empty">{{ $t('operator.live_empty') }}</div>
      <div
        v-for="ev in events"
        :key="ev.key"
        class="live-row"
        :class="ev.type === 'alert' ? 'alert-' + alertLevel(ev.data) : ''"
      >
        <span class="live-time">{{ formatClock(ev.at) }}</span>
        <span v-if="ev.type === 'order'">{{ orderLine(ev.data) }}</span>
        <span v-else>{{ alertText(ev.data) }}</span>
      </div>
    </div>

    <!-- Start -->
    <div class="section">
      <span class="section-title">{{ $t('operator.start_title') }}</span>
      <div v-if="occupying" class="empty">{{ $t('operator.slot_taken', { id: occupying.id }) }}</div>
      <div class="form-card">
        <van-field v-model="form.name" :label="$t('operator.name')" maxlength="100" :placeholder="$t('operator.name_placeholder')" />
        <van-field
          v-model.number="form.sizePct"
          type="number"
          :label="$t('operator.position_size')"
          :placeholder="'0.1 – 5'"
          :error-message="sizeError"
        >
          <template #extra>%</template>
        </van-field>
        <van-field
          v-model.number="form.stopPct"
          type="number"
          :label="$t('operator.stop_loss')"
          :placeholder="'1 – 15'"
          :error-message="stopError"
        >
          <template #extra>%</template>
        </van-field>
        <div class="universe">
          <div class="universe-head">{{ $t('operator.universe') }} ({{ form.universe.length }})</div>
          <van-checkbox-group v-model="form.universe">
            <div v-for="group in groups" :key="group.key" class="universe-group">
              <div class="universe-label">{{ $t('operator.group_' + group.key) }}</div>
              <div class="universe-items">
                <van-checkbox v-for="sym in group.symbols" :key="sym" :name="sym" shape="square" class="universe-item">
                  {{ sym }}
                </van-checkbox>
              </div>
            </div>
          </van-checkbox-group>
        </div>
      </div>
      <div class="actions">
        <van-button type="primary" block :disabled="!canStart" :loading="starting" @click="startRun">
          {{ $t('operator.start') }}
        </van-button>
      </div>
    </div>
  </div>
</template>

<script>
import { showConfirmDialog, showToast } from 'vant'
import { operatorApi } from '@/api'
import { UNIVERSE_GROUPS } from '@/constants/tradingUniverse'
import { createOperatorFeed } from '@/services/operatorFeed'

export default {
  name: 'OperatorStrategy',
  data() {
    return {
      groups: UNIVERSE_GROUPS,
      runs: [],
      loading: false,
      loadError: '',
      loadSeq: 0,
      liveStatus: 'connecting',
      liveEverConnected: false,
      equity: null,
      positions: null,
      equityAt: null,
      positionsAt: null,
      events: [],
      eventSeq: 0,
      feed: null,
      reloadTimer: null,
      starting: false,
      stopping: null,
      form: { name: 'house', sizePct: 5, stopPct: 7, universe: ['SPY', 'QQQ'] }
    }
  },
  computed: {
    occupying() {
      return this.runs.find((r) => r.occupying) || null
    },
    sizeError() {
      const v = Number(this.form.sizePct)
      return v > 0 && v <= 5 ? '' : this.$t('operator.size_range')
    },
    stopError() {
      const v = Number(this.form.stopPct)
      return v >= 1 && v <= 15 ? '' : this.$t('operator.stop_range')
    },
    canStart() {
      return !this.loading && !this.loadError && !this.occupying && !this.starting &&
        this.form.name.trim().length > 0 && this.form.universe.length > 0 &&
        !this.sizeError && !this.stopError
    }
  },
  mounted() {
    this.load()
    this.openFeed()
  },
  beforeUnmount() {
    if (this.feed) this.feed.close()
    clearTimeout(this.reloadTimer)
  },
  methods: {
    openFeed() {
      if (this.feed) this.feed.close()
      this.feed = createOperatorFeed({
        onStatus: (s) => this.onFeedStatus(s),
        onEvent: (ev) => this.onFeedEvent(ev)
      })
    },
    onFeedStatus(s) {
      // Socket.IO does not replay what was published while the socket was down:
      // after a reconnect, read the list again (the first connect follows the
      // mount's own load).
      if (s === 'connected' && this.liveEverConnected) this.scheduleReload()
      if (s === 'connected') this.liveEverConnected = true
      this.liveStatus = s
    },
    scheduleReload() {
      clearTimeout(this.reloadTimer)
      this.reloadTimer = setTimeout(() => this.load(), 1500)
    },
    onFeedEvent(ev) {
      if (ev.type === 'equity' || ev.type === 'positions') {
        this.applyAccount(ev)
        return
      }
      this.events = [{ ...ev, key: ++this.eventSeq }, ...this.events].slice(0, 30)
      if (ev.type === 'alert' && this.alertLevel(ev.data) === 'critical') {
        showToast({ message: this.alertText(ev.data), type: 'fail' })
      }
      // An order moved (submitted, filled, cancelled): fill counts and the gate
      // may have changed. One reload for a burst of updates.
      if (ev.type === 'order') this.scheduleReload()
    },
    applyAccount(ev) {
      const d = ev.data && typeof ev.data === 'object' ? ev.data : null
      if (!d) return
      const at = typeof d.at === 'string' ? d.at : null
      if (ev.type === 'equity') {
        this.equity = d
        this.equityAt = at
      } else {
        this.positions = Array.isArray(d.positions) ? d.positions : null
        this.positionsAt = at
      }
    },
    num(v) {
      return typeof v === 'number' && Number.isFinite(v) ? v.toLocaleString() : '—'
    },
    liveLabel() {
      switch (this.liveStatus) {
        case 'connected': return this.$t('operator.live_connected')
        case 'reconnecting': return this.$t('operator.live_reconnecting')
        case 'refused': return this.$t('operator.live_refused')
        case 'closed': return this.$t('operator.live_closed')
        default: return this.$t('operator.live_connecting')
      }
    },
    orderLine(o) {
      const d = o && typeof o === 'object' ? o : {}
      return this.$t('operator.live_order', {
        symbol: d.symbol || '?',
        side: d.side || '?',
        filled: d.filled_qty ?? 0,
        qty: d.qty ?? '?',
        status: d.status || '?'
      })
    },
    alertLevel(a) {
      return a && typeof a === 'object' && typeof a.level === 'string' ? a.level : 'info'
    },
    alertText(a) {
      if (a && typeof a === 'object') return String(a.message ?? a.raw ?? '')
      return String(a ?? '')
    },
    formatClock(ms) {
      return new Date(ms).toLocaleTimeString()
    },
    async load() {
      // Loads can overlap (refresh during the reload after a stop) and finish
      // out of order; only the latest may update the list, or an older "slot
      // free" answer could re-enable Start over a newer "occupied" one.
      const seq = ++this.loadSeq
      this.loading = true
      this.loadError = ''
      try {
        const res = await operatorApi.list()
        if (seq !== this.loadSeq) return
        this.runs = res?.data?.runs || []
      } catch (e) {
        if (seq !== this.loadSeq) return
        // The interceptor already toasted the server's message. Keep start
        // disabled: without the list we cannot tell whether the slot is free.
        this.runs = []
        this.loadError = e?.message || this.$t('operator.load_failed')
      } finally {
        if (seq === this.loadSeq) this.loading = false
      }
    },
    envLabel(env) {
      if (env === 'paper') return this.$t('operator.env_paper')
      if (env === 'real') return this.$t('operator.env_real')
      return this.$t('operator.env_unknown')
    },
    gateText(run) {
      if (run.paper_gate_met) return this.$t('operator.gate_met')
      // The server's reason (promotion_guard.paper_gate_status), most permanent first.
      const reason = run.paper_gate_reason
      if (reason === 'env_not_paper') return this.$t('operator.gate_env_real')
      if (reason === 'env_unknown') return this.$t('operator.gate_env_unknown')
      if (reason === 'no_fills') return this.$t('operator.gate_no_fills')
      // Only a run still going can still reach the gate; a stopped one never will.
      if (run.is_active && !run.stopped_at) {
        return this.$t('operator.gate_at', { at: this.formatTime(run.paper_gate_at) })
      }
      return this.$t('operator.gate_not_met')
    },
    formatTime(value) {
      if (!value) return '—'
      // kis-api sends naive UTC timestamps.
      const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : value + 'Z'
      const d = new Date(iso)
      return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
    },
    async startRun() {
      // Claimed before the dialog: a double tap must not open two dialogs and
      // send two starts.
      if (this.starting) return
      this.starting = true
      try {
        await showConfirmDialog({
          title: this.$t('operator.start_confirm_title'),
          message: this.$t('operator.start_confirm_msg', {
            count: this.form.universe.length,
            size: this.form.sizePct,
            stop: this.form.stopPct
          })
        })
      } catch (e) {
        this.starting = false
        return
      }
      try {
        await operatorApi.start({
          name: this.form.name.trim(),
          universe: [...this.form.universe],
          position_size_pct: Number(this.form.sizePct) / 100,
          stop_loss_pct: Number(this.form.stopPct) / 100
        })
        showToast({ message: this.$t('operator.started'), type: 'success' })
      } catch (e) {
        // Toasted by the interceptor. Never resent: the list below shows what
        // actually happened, and kis-api refuses a second start anyway.
      } finally {
        this.starting = false
        await this.load()
      }
    },
    async stopRun(run) {
      if (this.stopping !== null) return
      this.stopping = run.id
      try {
        await showConfirmDialog({
          title: this.$t('operator.stop_confirm_title'),
          message: this.$t('operator.stop_confirm_msg', { id: run.id })
        })
      } catch (e) {
        this.stopping = null
        return
      }
      try {
        await operatorApi.stop(run.id)
        showToast({ message: this.$t('operator.stop_requested'), type: 'success' })
      } catch (e) {
        // Toasted by the interceptor.
      } finally {
        this.stopping = null
        await this.load()
      }
    }
  }
}
</script>

<style scoped>
.operator-page {
  min-height: 100vh;
  padding-bottom: 40px;
  background: var(--bg);
}

:deep(.van-nav-bar) { background: transparent; }
:deep(.van-nav-bar .van-nav-bar__title),
:deep(.van-nav-bar .van-icon) { color: var(--text); }

.notice {
  margin: 12px 16px 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2);
}

.section { margin: 16px; }

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.section-title {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 8px;
}
.section-head .section-title { margin-bottom: 0; }

.empty {
  padding: 14px 16px;
  font-size: 12px;
  color: var(--text-2);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  margin-bottom: 8px;
}
.empty.error { color: var(--c-red, #ee0a24); }

.run-card {
  padding: 12px 16px;
  margin-bottom: 8px;
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
}
.run-card.occupying { border-color: var(--c-green, #07c160); }

.run-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.run-name { font-size: 14px; font-weight: 600; color: var(--text); }
.run-line { font-size: 12px; color: var(--text-2); line-height: 1.7; }
.run-line.gate.met { color: var(--c-green, #07c160); }
.run-line .shadow { color: var(--c-orange, #ff976a); }
.run-stop { margin-top: 10px; }

.form-card {
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  overflow: hidden;
}

.universe { padding: 12px 16px; }
.universe-head { font-size: 13px; color: var(--text); margin-bottom: 8px; }
.universe-group { margin-bottom: 10px; }
.universe-label { font-size: 11px; color: var(--text-2); margin-bottom: 6px; }
.universe-items { display: flex; flex-wrap: wrap; gap: 8px 14px; }
.universe-item { font-size: 12px; }

.actions { margin-top: 14px; }

.live-retry { margin-left: 8px; }
.account-card {
  padding: 12px 16px;
  margin-bottom: 8px;
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-2);
}
.account-top { display: flex; justify-content: space-between; color: var(--text); font-size: 13px; }
.account-unverified { margin-top: 4px; }
.account-line { line-height: 1.8; }
.account-pos { display: grid; grid-template-columns: 1.2fr 1fr 1.4fr 1.4fr; gap: 4px; padding: 4px 0; border-top: 1px solid var(--border); }
.pos-symbol { color: var(--text); font-weight: 600; }
.account-at { margin-top: 4px; }
.live-row {
  display: flex;
  gap: 8px;
  padding: 6px 4px;
  font-size: 12px;
  color: var(--text);
  border-bottom: 1px solid var(--border);
}
.live-time { color: var(--text-2); flex: none; }
.live-row.alert-critical,
.live-row.alert-error { color: var(--c-red, #ee0a24); }
.live-row.alert-warning { color: var(--c-orange, #ff976a); }
</style>
