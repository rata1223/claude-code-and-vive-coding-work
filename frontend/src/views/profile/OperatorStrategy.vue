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

export default {
  name: 'OperatorStrategy',
  data() {
    return {
      groups: UNIVERSE_GROUPS,
      runs: [],
      loading: false,
      loadError: '',
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
  },
  methods: {
    async load() {
      this.loading = true
      this.loadError = ''
      try {
        const res = await operatorApi.list()
        this.runs = res?.data?.runs || []
      } catch (e) {
        // The interceptor already toasted the server's message. Keep start
        // disabled: without the list we cannot tell whether the slot is free.
        this.runs = []
        this.loadError = e?.message || this.$t('operator.load_failed')
      } finally {
        this.loading = false
      }
    },
    gateText(run) {
      if (run.paper_gate_met) return this.$t('operator.gate_met')
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
</style>
