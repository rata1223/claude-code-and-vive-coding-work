<template>
  <div class="credential-form-page">
    <van-nav-bar
      title="브로커 자격증명 등록"
      left-arrow
      :border="false"
      @click-left="$router.back()"
    />

    <div class="form-card">
      <div class="section-title">기본 정보</div>

      <van-field
        v-model="form.name"
        label="이름"
        placeholder="예: 내 KIS 계좌"
      />

      <van-cell
        title="브로커"
        :value="selectedBrokerLabel || '브로커 선택'"
        is-link
        @click="showBrokerPicker = true"
      />

      <!-- KIS 전용 필드 -->
      <template v-if="form.exchange_id === 'kis'">
        <div class="section-title" style="margin-top:16px">KIS 한국투자증권</div>
        <van-field v-model="form.api_key" label="앱키" placeholder="KIS 앱키 입력" />
        <van-field v-model="form.secret_key" label="시크릿" type="password" placeholder="KIS 시크릿 입력" />
        <van-field v-model="form.account_no" label="계좌번호" placeholder="12자리 계좌번호" maxlength="12" />
        <van-field v-model="form.hts_id" label="HTS ID" placeholder="HTS 로그인 ID" />
        <div class="switch-row">
          <div>
            <span class="switch-title">모의투자 모드</span>
            <p class="switch-desc">활성화 시 모의투자 서버 사용 (실제 자금 없음)</p>
          </div>
          <van-switch v-model="form.enable_demo_trading" size="20px" />
        </div>
      </template>

      <!-- 키움 전용 필드 -->
      <template v-else-if="form.exchange_id === 'kiwoom'">
        <div class="section-title" style="margin-top:16px">키움증권</div>
        <van-notice-bar
          text="키움증권은 아직 지원 준비 중입니다."
          left-icon="info-o"
          wrapable
          :scrollable="false"
          style="margin-bottom:12px;border-radius:8px"
        />
        <van-field v-model="form.api_key" label="앱키" placeholder="키움 앱키 입력" disabled />
        <van-field v-model="form.secret_key" label="시크릿" type="password" placeholder="키움 시크릿 입력" disabled />
        <van-field v-model="form.account_no" label="계좌번호" placeholder="계좌번호" disabled />
      </template>
    </div>

    <div class="form-card" style="margin-top:0">
      <van-button
        block
        type="primary"
        :loading="saving"
        :disabled="form.exchange_id === 'kiwoom'"
        @click="submit"
      >
        저장
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

export default {
  name: 'CredentialForm',

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
        passphrase: '',
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

    validate() {
      if (!this.form.name.trim()) {
        showToast({ message: '이름을 입력하세요', type: 'fail' })
        return false
      }
      if (!this.form.exchange_id) {
        showToast({ message: '브로커를 선택하세요', type: 'fail' })
        return false
      }
      if (this.form.exchange_id === 'kis') {
        if (!this.form.api_key.trim() || !this.form.secret_key.trim()) {
          showToast({ message: '앱키와 시크릿을 입력하세요', type: 'fail' })
          return false
        }
        if (!this.form.account_no.trim() || this.form.account_no.trim().length !== 12) {
          showToast({ message: '12자리 계좌번호를 입력하세요', type: 'fail' })
          return false
        }
      }
      return true
    },

    async submit() {
      if (!this.validate()) return
      this.saving = true
      try {
        await credentialsApi.create({
          name: this.form.name.trim(),
          exchange_id: this.form.exchange_id,
          api_key: this.form.api_key.trim(),
          secret_key: this.form.secret_key.trim(),
          passphrase: this.form.passphrase.trim(),
          account_no: this.form.account_no.trim(),
          hts_id: this.form.hts_id.trim(),
          enable_demo_trading: this.form.enable_demo_trading
        })
        showToast({ message: '저장 완료', type: 'success' })
        this.$router.replace('/profile/credentials')
      } catch (error) {
        console.error('자격증명 저장 실패:', error)
        showToast({ message: '저장 실패: ' + (error?.message || ''), type: 'fail' })
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
  margin-top: 12px;
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
  padding: 14px 0 4px;
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
  border-radius: 14px;
  height: 48px;
  font-weight: 700;
  background: var(--accent);
  color: var(--text-on-accent);
  border: none;
}
</style>
