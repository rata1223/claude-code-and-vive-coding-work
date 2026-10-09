<template>
  <div class="credential-form-page">
    <van-nav-bar
      :title="$t('credentials.add_title')"
      left-arrow
      :border="false"
      @click-left="$router.back()"
    />

    <div class="form-card">
      <div class="section-title">{{ $t('credentials.section_basic') }}</div>
      <van-field
        v-model="form.name"
        :label="$t('credentials.name')"
        :placeholder="$t('credentials.name_placeholder')"
      />
      <van-cell
        :title="$t('credentials.exchange')"
        :value="selectedBrokerLabel || $t('credentials.exchange_placeholder')"
        is-link
        @click="showBrokerPicker = true"
      />

      <!-- KIS: app key/secret, the account orders are placed on, HTS ID. -->
      <template v-if="form.exchange_id === 'kis'">
        <van-field
          v-model="form.api_key"
          label="App Key"
          :placeholder="$t('credentials.api_key_placeholder')"
        />
        <van-field
          v-model="form.secret_key"
          label="App Secret"
          type="password"
          :placeholder="$t('credentials.secret_key_placeholder')"
        />
        <van-field
          v-model="form.account_no"
          :label="$t('credentials.account_no')"
          :placeholder="$t('credentials.account_no_placeholder')"
          maxlength="13"
        />
        <van-field
          v-model="form.hts_id"
          :label="$t('credentials.hts_id')"
          :placeholder="$t('credentials.hts_id_placeholder')"
        />
        <div class="switch-row">
          <div>
            <span class="switch-title">{{ $t('credentials.demo_enable') }}</span>
            <p class="switch-desc">{{ $t('credentials.demo_desc') }}</p>
          </div>
          <van-switch v-model="form.enable_demo_trading" size="20px" />
        </div>
      </template>

      <!-- Kiwoom: not tradable yet, so nothing to save. -->
      <van-notice-bar
        v-else-if="form.exchange_id === 'kiwoom'"
        :text="$t('credentials.kiwoom_unsupported')"
        left-icon="info-o"
        wrapable
        :scrollable="false"
        class="kiwoom-notice"
      />

      <van-button
        block
        type="primary"
        :loading="saving"
        :disabled="form.exchange_id !== 'kis'"
        @click="submit"
      >
        {{ $t('credentials.save') }}
      </van-button>
    </div>

    <van-popup v-model:show="showBrokerPicker" position="bottom" round>
      <van-picker
        :columns="brokerColumns"
        @cancel="showBrokerPicker = false"
        @confirm="onSelectBroker"
      />
    </van-popup>
  </div>
</template>

<script>
import { showToast } from 'vant'
import { credentialsApi } from '@/api'
import { EXCHANGE_OPTIONS } from '@/constants/exchanges'

/**
 * A KIS account is 10 digits: the 8-digit CANO and the 2-digit product code
 * (the backend splits it as [:8] / [8:]). It is usually written 50123456-01,
 * so hyphens and spaces are dropped here — and again by the API.
 */
export function normalizeAccountNo(value) {
  return String(value || '').replace(/[-\s]/g, '')
}

export function isKisAccountNo(value) {
  return /^\d{10}$/.test(normalizeAccountNo(value))
}

export default {
  name: 'CredentialCreate',

  data() {
    return {
      saving: false,
      showBrokerPicker: false,
      form: {
        name: '',
        exchange_id: 'kis',
        api_key: '',
        secret_key: '',
        account_no: '',
        hts_id: '',
        // Paper by default: switching to real money is a deliberate act.
        enable_demo_trading: true
      }
    }
  },

  computed: {
    brokerColumns() {
      return EXCHANGE_OPTIONS.map((item) => ({ text: item.label, value: item.value }))
    },
    selectedBrokerLabel() {
      return EXCHANGE_OPTIONS.find((item) => item.value === this.form.exchange_id)?.label || ''
    }
  },

  methods: {
    onSelectBroker(payload) {
      const selected = payload?.selectedOptions?.[0] || payload?.selectedOption || payload?.[0] || payload
      this.form.exchange_id = selected?.value || 'kis'
      this.showBrokerPicker = false
    },

    fail(key) {
      showToast({ message: this.$t(key), type: 'fail' })
      return false
    },

    validate() {
      if (!this.form.name.trim()) return this.fail('credentials.name_required')
      if (this.form.exchange_id !== 'kis') return this.fail('credentials.kiwoom_unsupported')
      if (!this.form.api_key.trim() || !this.form.secret_key.trim()) {
        return this.fail('credentials.keys_required')
      }
      if (!normalizeAccountNo(this.form.account_no)) return this.fail('credentials.account_no_required')
      if (!isKisAccountNo(this.form.account_no)) return this.fail('credentials.account_no_format')
      return true
    },

    async submit() {
      if (this.saving || !this.validate()) return
      this.saving = true
      try {
        await credentialsApi.create({
          name: this.form.name.trim(),
          exchange_id: this.form.exchange_id,
          api_key: this.form.api_key.trim(),
          secret_key: this.form.secret_key.trim(),
          account_no: normalizeAccountNo(this.form.account_no),
          hts_id: this.form.hts_id.trim(),
          enable_demo_trading: this.form.enable_demo_trading
        })
        showToast({ message: this.$t('credentials.saved'), type: 'success' })
        this.$router.replace('/profile/credentials')
      } catch (error) {
        // The API client has already shown why (its toast would be replaced).
        console.error('Create credential failed:', error)
      } finally {
        this.saving = false
      }
    }
  }
}
</script>

<style scoped>
.credential-form-page {
  min-height: 100vh;
  padding-bottom: 24px;
  background: transparent;
}

.credential-form-page :deep(.van-nav-bar) { background: transparent; }
.credential-form-page :deep(.van-nav-bar__title),
.credential-form-page :deep(.van-nav-bar__arrow),
.credential-form-page :deep(.van-nav-bar .van-icon) { color: var(--text); }

.form-card {
  margin: 16px;
  padding: 18px 16px;
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
}

.section-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-2);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: 10px;
}

.switch-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 14px 0 16px;
  color: var(--text);
}
.switch-row > div:first-child { flex: 1; min-width: 0; }
.switch-title {
  display: block;
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
}
.switch-desc {
  margin-top: 3px;
  font-size: 12px;
  color: var(--text-2);
  line-height: 1.5;
}

.kiwoom-notice {
  margin: 12px 0;
  border-radius: 8px;
}

.credential-form-page :deep(.van-cell) {
  background: transparent;
  padding-left: 0;
  padding-right: 0;
}

.credential-form-page :deep(.van-cell__title),
.credential-form-page :deep(.van-cell__value),
.credential-form-page :deep(.van-cell__right-icon),
.credential-form-page :deep(.van-field__label),
.credential-form-page :deep(.van-field__control) {
  color: var(--text);
}

.credential-form-page :deep(.van-button--primary) {
  margin-top: 10px;
  border-radius: 14px;
  height: 48px;
  font-weight: 700;
  background: var(--accent);
  color: var(--text-on-accent);
  border: none;
}
</style>
