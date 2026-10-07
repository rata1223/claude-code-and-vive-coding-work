<template>
  <div class="login-page">
    <div class="login-shell">
      <div class="login-toolbar">
        <button
          type="button"
          class="lang-btn"
          :aria-label="$t('login.language_switch')"
          @click="showLangSheet = true"
        >
          {{ langButtonLabel }}
        </button>
      </div>
      <div class="login-header">
        <div class="logo-wrap">
          <img :src="logoUrl" alt="Logo" class="logo-image" />
        </div>
        <p class="subtitle">{{ $t('login.subtitle') }}</p>
      </div>

      <div class="tab-bar">
        <div
          v-for="tab in tabs"
          :key="tab.value"
          :class="['tab', { active: mode === tab.value }]"
          @click="switchMode(tab.value)"
        >
          {{ tab.label }}
        </div>
      </div>

      <div class="login-form">
        <!-- Login -->
        <template v-if="mode === 'login'">
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="user-o" class="input-icon" />
              <input
                v-model="loginForm.username"
                type="text"
                :placeholder="$t('login.placeholder_username')"
                class="input"
                autocomplete="username"
              />
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="lock" class="input-icon" />
              <input
                v-model="loginForm.password"
                :type="showPassword ? 'text' : 'password'"
                :placeholder="$t('login.placeholder_password')"
                class="input"
                autocomplete="current-password"
                @keyup.enter="handleLogin"
              />
              <van-icon
                :name="showPassword ? 'eye-o' : 'closed-eye'"
                class="eye-icon"
                @click="showPassword = !showPassword"
              />
            </div>
          </div>
          <div class="row-between">
            <van-checkbox v-model="rememberMe" shape="square" icon-size="16">
              {{ $t('login.remember') }}
            </van-checkbox>
            <span class="link" @click="switchMode('forgot')">{{ $t('login.forgot_password') }}</span>
          </div>
          <div class="row-agreement">
            <van-checkbox v-model="agreeTerms" shape="square" icon-size="16">
              <span class="agree-line">
                {{ $t('login.agree_prefix') }}
                <span class="link-inline" role="button" tabindex="0" @click.stop.prevent="openLegal('terms')">{{
                  $t('login.agree_terms_link')
                }}</span>
                {{ $t('login.agree_connector') }}
                <span
                  class="link-inline"
                  role="button"
                  tabindex="0"
                  @click.stop.prevent="openLegal('disclaimer')"
                  >{{ $t('login.agree_disclaimer_link') }}</span
                >
              </span>
            </van-checkbox>
          </div>
        </template>

        <!-- Register -->
        <template v-else-if="mode === 'register'">
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="envelop-o" class="input-icon" />
              <input
                v-model="registerForm.email"
                type="email"
                :placeholder="$t('login.placeholder_email')"
                class="input"
                autocomplete="email"
              />
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="shield-o" class="input-icon" />
              <input
                v-model="registerForm.code"
                type="text"
                inputmode="numeric"
                maxlength="6"
                :placeholder="$t('login.placeholder_code')"
                class="input"
              />
              <button
                type="button"
                class="code-btn"
                :disabled="codeCountdown > 0 || sendingCode"
                @click="sendCode('register')"
              >
                {{ codeBtnText }}
              </button>
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="user-o" class="input-icon" />
              <input
                v-model="registerForm.username"
                type="text"
                :placeholder="$t('login.placeholder_new_username')"
                class="input"
                autocomplete="username"
              />
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="lock" class="input-icon" />
              <input
                v-model="registerForm.password"
                :type="showPassword ? 'text' : 'password'"
                :placeholder="$t('login.placeholder_new_password')"
                class="input"
                autocomplete="new-password"
              />
              <van-icon
                :name="showPassword ? 'eye-o' : 'closed-eye'"
                class="eye-icon"
                @click="showPassword = !showPassword"
              />
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="friends-o" class="input-icon" />
              <input
                v-model="registerForm.referralCode"
                type="text"
                :placeholder="$t('login.placeholder_referral')"
                class="input"
              />
            </div>
          </div>
          <div class="row-agreement">
            <van-checkbox v-model="agreeTerms" shape="square" icon-size="16">
              <span class="agree-line">
                {{ $t('login.agree_prefix') }}
                <span class="link-inline" role="button" tabindex="0" @click.stop.prevent="openLegal('terms')">{{
                  $t('login.agree_terms_link')
                }}</span>
                {{ $t('login.agree_connector') }}
                <span
                  class="link-inline"
                  role="button"
                  tabindex="0"
                  @click.stop.prevent="openLegal('disclaimer')"
                  >{{ $t('login.agree_disclaimer_link') }}</span
                >
              </span>
            </van-checkbox>
          </div>
        </template>

        <!-- Forgot Password -->
        <template v-else-if="mode === 'forgot'">
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="envelop-o" class="input-icon" />
              <input
                v-model="forgotForm.email"
                type="email"
                :placeholder="$t('login.placeholder_email')"
                class="input"
              />
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="shield-o" class="input-icon" />
              <input
                v-model="forgotForm.code"
                type="text"
                inputmode="numeric"
                maxlength="6"
                :placeholder="$t('login.placeholder_code')"
                class="input"
              />
              <button
                type="button"
                class="code-btn"
                :disabled="codeCountdown > 0 || sendingCode"
                @click="sendCode('reset_password')"
              >
                {{ codeBtnText }}
              </button>
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="lock" class="input-icon" />
              <input
                v-model="forgotForm.newPassword"
                :type="showPassword ? 'text' : 'password'"
                :placeholder="$t('login.placeholder_new_password')"
                class="input"
              />
              <van-icon
                :name="showPassword ? 'eye-o' : 'closed-eye'"
                class="eye-icon"
                @click="showPassword = !showPassword"
              />
            </div>
          </div>
          <div class="form-item">
            <div class="input-wrapper">
              <van-icon name="lock" class="input-icon" />
              <input
                v-model="forgotForm.confirmPassword"
                :type="showPassword ? 'text' : 'password'"
                :placeholder="$t('login.confirm_password')"
                class="input"
              />
            </div>
          </div>
        </template>

        <div v-if="securityConfig.turnstile_enabled" class="security-panel">
          <div class="security-copy">
            <span class="security-title">{{ $t('login.turnstile_title') }}</span>
            <span class="security-desc">{{ $t('login.turnstile_desc') }}</span>
          </div>
          <div class="turnstile-card">
            <div ref="turnstileRef" class="turnstile-mount"></div>
            <p v-if="turnstileError" class="turnstile-error">{{ turnstileError }}</p>
          </div>
        </div>

        <van-button
          type="primary"
          block
          :loading="loading"
          :disabled="!canSubmit"
          class="submit-btn"
          @click="handleSubmit"
        >
          {{ submitLabel }}
        </van-button>

        <div class="alt-link">
          <span v-if="mode === 'login'" class="link" @click="switchMode('register')">{{ $t('login.to_register') }}</span>
          <span v-else class="link" @click="switchMode('login')">{{ $t('login.to_login') }}</span>
        </div>

        <div class="footer">
          <p>{{ $t('login.footer_tip') }}</p>
        </div>
      </div>
    </div>

    <van-action-sheet
      v-model:show="showLangSheet"
      :actions="langSheetActions"
      :cancel-text="$t('common.cancel')"
      close-on-click-action
      @select="onLangSheetSelect"
    />

    <van-popup
      v-model:show="legalVisible"
      position="bottom"
      round
      :style="{ height: '78vh' }"
      class="legal-popup-root"
    >
      <div class="legal-popup">
        <div class="legal-popup__head">
          <span
            :class="['legal-tab', { active: legalTab === 'terms' }]"
            @click="legalTab = 'terms'"
            >{{ $t('about.terms_tab') }}</span
          >
          <span
            :class="['legal-tab', { active: legalTab === 'disclaimer' }]"
            @click="legalTab = 'disclaimer'"
            >{{ $t('about.disclaimer_tab') }}</span
          >
        </div>
        <div class="legal-popup__body">{{ legalPopupBody }}</div>
        <div class="legal-popup__foot">
          <van-button type="primary" block round @click="legalVisible = false">{{
            $t('common.confirm')
          }}</van-button>
        </div>
      </div>
    </van-popup>
  </div>
</template>

<script>
import { showToast } from 'vant'
import { authApi } from '@/api'
import { useUserStore, useSettingsStore } from '@/stores'
import { getLegal } from '@/constants/legal'
import logoUrl from '@/assets/logo.png'

/** Normalize backend variants for Turnstile (PC / mobile API parity). */
function parseTurnstileConfig(raw) {
  const d = raw && typeof raw === 'object' ? raw : {}
  const nested = (d.security && typeof d.security === 'object' && d.security) || {}
  const nested2 = (d.turnstile && typeof d.turnstile === 'object' && d.turnstile) || {}
  const pickKey = (...objs) => {
    const keys = [
      'turnstile_site_key',
      'turnstileSiteKey',
      'cf_turnstile_site_key',
      'cloudflare_turnstile_site_key',
      'site_key',
      'siteKey',
      'public_key'
    ]
    for (const obj of objs) {
      if (!obj || typeof obj !== 'object') continue
      for (const k of keys) {
        const v = obj[k]
        if (v != null && String(v).trim()) return String(v).trim()
      }
    }
    return ''
  }
  const siteKey = pickKey(d, nested, nested2)
  const rawEn =
    d.turnstile_enabled ??
    d.cf_turnstile_enabled ??
    d.cloudflare_turnstile_enabled ??
    d.turnstile ??
    nested.turnstile_enabled ??
    nested.enabled ??
    nested2.enabled

  if (!siteKey) {
    return { enabled: false, siteKey: '' }
  }
  if (rawEn === false || rawEn === 0 || rawEn === '0' || rawEn === 'false') {
    return { enabled: false, siteKey }
  }
  if (rawEn === true || rawEn === 1 || rawEn === '1' || rawEn === 'true') {
    return { enabled: true, siteKey }
  }
  return { enabled: true, siteKey }
}

const TURNSTILE_SCRIPT_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
let turnstileScriptPromise

const ensureTurnstileScript = () => {
  if (window.turnstile) {
    return Promise.resolve(window.turnstile)
  }

  if (turnstileScriptPromise) {
    return turnstileScriptPromise
  }

  turnstileScriptPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${TURNSTILE_SCRIPT_SRC}"]`)
    if (existing) {
      existing.addEventListener('load', () => resolve(window.turnstile), { once: true })
      existing.addEventListener('error', () => reject(new Error('Turnstile 脚本加载失败')), { once: true })
      return
    }

    const script = document.createElement('script')
    script.src = TURNSTILE_SCRIPT_SRC
    script.async = true
    script.defer = true
    script.onload = () => resolve(window.turnstile)
    script.onerror = () => reject(new Error('Turnstile 脚本加载失败'))
    document.head.appendChild(script)
  })

  return turnstileScriptPromise
}

export default {
  name: 'Login',

  data() {
    return {
      logoUrl,
      mode: 'login',
      loginForm: { username: '', password: '' },
      registerForm: { email: '', code: '', username: '', password: '', referralCode: '' },
      forgotForm: { email: '', code: '', newPassword: '', confirmPassword: '' },
      securityConfig: {
        turnstile_enabled: false,
        turnstile_site_key: ''
      },
      showPassword: false,
      rememberMe: true,
      agreeTerms: false,
      loading: false,
      sendingCode: false,
      codeCountdown: 0,
      codeTimer: null,
      turnstileWidgetId: null,
      turnstileToken: '',
      turnstileError: '',
      turnstileErrorCode: '',
      showLangSheet: false,
      legalVisible: false,
      legalTab: 'terms'
    }
  },

  computed: {
    settingsStore() {
      return useSettingsStore()
    },
    isLightTheme() {
      return this.settingsStore.theme === 'light'
    },
    turnstileWidgetTheme() {
      return this.isLightTheme ? 'light' : 'dark'
    },
    tabs() {
      return [
        { value: 'login', label: this.$t('login.tab_login') },
        { value: 'register', label: this.$t('login.tab_register') },
        { value: 'forgot', label: this.$t('login.tab_forgot') }
      ]
    },
    submitLabel() {
      if (this.mode === 'login') return this.$t('login.login')
      if (this.mode === 'register') return this.$t('login.register')
      return this.$t('login.reset_submit')
    },
    codeBtnText() {
      if (this.codeCountdown > 0) {
        return this.$t('login.resend_in', { seconds: this.codeCountdown })
      }
      return this.$t('login.send_code')
    },
    langButtonLabel() {
      const map = {
        'en-US': 'EN',
        'zh-CN': '中文',
        'zh-TW': '繁',
        'ja-JP': 'JA',
        'ko-KR': 'KO'
      }
      return map[this.settingsStore.locale] || 'EN'
    },
    langSheetActions() {
      return [
        { name: this.$t('language.en_us'), value: 'en-US' },
        { name: this.$t('language.zh_cn'), value: 'zh-CN' },
        { name: this.$t('language.zh_tw'), value: 'zh-TW' },
        { name: this.$t('language.ja_jp'), value: 'ja-JP' },
        { name: this.$t('language.ko_kr'), value: 'ko-KR' }
      ]
    },
    legalPopupBody() {
      const loc = this.settingsStore.locale
      const doc = getLegal(loc)
      return this.legalTab === 'terms' ? doc.terms : doc.disclaimer
    },
    canSubmit() {
      const turnstileReady = !this.securityConfig.turnstile_enabled || !!this.turnstileToken
      if (!turnstileReady) return false

      if (this.mode === 'login') {
        return !!(
          this.loginForm.username.trim() &&
          this.loginForm.password &&
          this.agreeTerms
        )
      }
      if (this.mode === 'register') {
        return !!(
          this.registerForm.email.trim() &&
          this.registerForm.code.trim() &&
          this.registerForm.username.trim() &&
          this.registerForm.password &&
          this.agreeTerms
        )
      }
      return !!(
        this.forgotForm.email.trim() &&
        this.forgotForm.code.trim() &&
        this.forgotForm.newPassword &&
        this.forgotForm.confirmPassword
      )
    }
  },

  watch: {
    isLightTheme() {
      this.$nextTick(() => this.renderTurnstileIfNeeded())
    }
  },

  async mounted() {
    await this.initSecurity()
  },

  beforeUnmount() {
    if (this.codeTimer) clearInterval(this.codeTimer)
    this.destroyTurnstileWidget()
  },

  methods: {
    switchMode(mode) {
      if (this.mode === mode) return
      this.mode = mode
      this.showPassword = false
      if (mode === 'login' || mode === 'register') {
        this.agreeTerms = false
      }
      this.$nextTick(() => this.renderTurnstileIfNeeded())
    },

    onLangSheetSelect(action) {
      if (!action || action.value == null) return
      this.settingsStore.setLocale(action.value)
      this.showLangSheet = false
      showToast({ message: this.$t('common.success'), type: 'success' })
    },

    openLegal(tab) {
      this.legalTab = tab === 'disclaimer' ? 'disclaimer' : 'terms'
      this.legalVisible = true
    },

    async initSecurity() {
      try {
        const res = await authApi.getSecurityConfig()
        if (res.code === 1 && res.data) {
          const ts = parseTurnstileConfig(res.data)
          this.securityConfig = {
            turnstile_enabled: ts.enabled,
            turnstile_site_key: ts.siteKey
          }
        }
      } catch (err) {
        console.warn('Failed to load security config:', err)
      }
      await this.$nextTick()
      await this.renderTurnstileIfNeeded()
    },

    destroyTurnstileWidget() {
      if (this.turnstileWidgetId !== null && window.turnstile?.remove) {
        try {
          window.turnstile.remove(this.turnstileWidgetId)
        } catch (e) {
          console.warn('Turnstile remove:', e)
        }
      }
      this.turnstileWidgetId = null
      this.turnstileToken = ''
    },

    async renderTurnstileIfNeeded() {
      if (!this.securityConfig.turnstile_enabled || !this.securityConfig.turnstile_site_key) {
        this.destroyTurnstileWidget()
        return
      }
      try {
        this.turnstileError = ''
        this.turnstileErrorCode = ''
        await ensureTurnstileScript()
        if (!this.$refs.turnstileRef || !window.turnstile) return

        this.destroyTurnstileWidget()

        this.turnstileWidgetId = window.turnstile.render(this.$refs.turnstileRef, {
          sitekey: this.securityConfig.turnstile_site_key,
          theme: this.turnstileWidgetTheme,
          size: 'flexible',
          callback: (token) => {
            this.turnstileToken = token
            this.turnstileError = ''
            this.turnstileErrorCode = ''
          },
          'expired-callback': () => {
            this.turnstileToken = ''
            this.turnstileError = this.$t('login.turnstile_fail')
            this.turnstileErrorCode = 'expired'
          },
          'error-callback': (errorCode) => {
            this.turnstileToken = ''
            this.turnstileErrorCode = String(errorCode || '')
            this.turnstileError = this.getTurnstileErrorMessage(errorCode)
          }
        })
      } catch (err) {
        console.error('Turnstile render failed:', err)
        this.turnstileError = this.$t('login.turnstile_fail')
      }
    },

    resetTurnstile() {
      if (!this.securityConfig.turnstile_enabled || this.turnstileWidgetId === null || !window.turnstile) {
        return
      }
      this.turnstileToken = ''
      this.turnstileError = ''
      this.turnstileErrorCode = ''
      window.turnstile.reset(this.turnstileWidgetId)
    },

    getTurnstileErrorMessage(errorCode) {
      const code = String(errorCode || '')
      const hostname = window.location.hostname || 'current host'

      if (code === '110200') {
        return `${hostname} is not allow-listed on Cloudflare Turnstile for the configured site key.`
      }
      if (code.startsWith('110')) {
        return `Turnstile rejected (code ${code}). This is usually a domain / site-key misconfiguration.`
      }
      return code
        ? `Turnstile failed to load (code ${code}), please retry later.`
        : this.$t('login.turnstile_fail')
    },

    startCountdown(seconds = 60) {
      this.codeCountdown = seconds
      if (this.codeTimer) clearInterval(this.codeTimer)
      this.codeTimer = setInterval(() => {
        this.codeCountdown -= 1
        if (this.codeCountdown <= 0) {
          clearInterval(this.codeTimer)
          this.codeTimer = null
        }
      }, 1000)
    },

    /**
     * Normalize and validate an email address.
     *
     * Mobile IMEs sometimes inject full-width punctuation (e.g. `＠` `．`),
     * NBSP / zero-width characters, or uppercase variants that look correct
     * to the user but fail a strict ASCII regex. Normalize first so the
     * "I clearly typed an email" case stops failing silently as
     * `email_required`.
     */
    normalizeEmail(email) {
      let s = String(email == null ? '' : email)
      s = s.replace(/[\u200B-\u200D\uFEFF\u00A0]/g, '')
      s = s.replace(/＠/g, '@').replace(/[．。]/g, '.')
      return s.trim().toLowerCase()
    },
    validEmail(email) {
      return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(this.normalizeEmail(email))
    },

    async sendCode(type) {
      const rawEmail = type === 'register' ? this.registerForm.email : this.forgotForm.email
      const email = this.normalizeEmail(rawEmail)
      if (!email) {
        showToast({ message: this.$t('login.email_required'), type: 'fail' })
        return
      }
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        showToast({ message: this.$t('login.email_invalid'), type: 'fail' })
        return
      }
      if (type === 'register') {
        this.registerForm.email = email
      } else {
        this.forgotForm.email = email
      }
      if (this.securityConfig.turnstile_enabled && !this.turnstileToken) {
        showToast({ message: this.$t('login.turnstile_required'), type: 'fail' })
        return
      }

      this.sendingCode = true
      try {
        const res = await authApi.sendCode({
          email,
          type,
          turnstile_token: this.turnstileToken || undefined
        })
        if (res.code === 1) {
          showToast({ message: this.$t('login.code_sent'), type: 'success' })
          this.startCountdown(60)
          this.resetTurnstile()
        } else {
          showToast({ message: res.msg || this.$t('login.code_send_fail'), type: 'fail' })
          this.resetTurnstile()
        }
      } catch (err) {
        console.error('Send code error:', err)
        this.resetTurnstile()
      } finally {
        this.sendingCode = false
      }
    },

    handleSubmit() {
      if (this.loading || !this.canSubmit) return
      if (this.securityConfig.turnstile_enabled && !this.turnstileToken) {
        showToast({ message: this.$t('login.turnstile_required'), type: 'fail' })
        return
      }
      if (this.mode === 'login') this.handleLogin()
      else if (this.mode === 'register') this.handleRegister()
      else this.handleReset()
    },

    async finalizeLogin(token, userinfo) {
      const userStore = useUserStore()
      userStore.setToken(token)
      if (userinfo) {
        userStore.setUserInfo(userinfo)
      } else {
        try {
          const infoRes = await authApi.getInfo()
          if (infoRes.code === 1 && infoRes.data) {
            userStore.setUserInfo(infoRes.data)
          }
        } catch (e) {
          console.warn('Failed to get user info:', e)
        }
      }
      const redirect = this.$route.query.redirect || '/home'
      this.$router.replace(redirect)
    },

    async handleLogin() {
      if (!this.agreeTerms) {
        showToast({ message: this.$t('login.need_agree'), type: 'fail' })
        return
      }
      this.loading = true
      try {
        const res = await authApi.login({
          username: this.loginForm.username.trim(),
          password: this.loginForm.password,
          turnstile_token: this.turnstileToken || undefined
        })
        if (res.code === 1 && res.data?.token) {
          showToast({ message: this.$t('login.login_success'), type: 'success' })
          await this.finalizeLogin(res.data.token, res.data.userinfo)
        } else {
          showToast({ message: res.msg || this.$t('login.login_fail'), type: 'fail' })
          this.resetTurnstile()
        }
      } catch (err) {
        console.error('Login error:', err)
        this.resetTurnstile()
      } finally {
        this.loading = false
      }
    },

    async handleRegister() {
      if (!this.agreeTerms) {
        showToast({ message: this.$t('login.need_agree'), type: 'fail' })
        return
      }
      this.loading = true
      try {
        const res = await authApi.register({
          email: this.normalizeEmail(this.registerForm.email),
          code: this.registerForm.code.trim(),
          username: this.registerForm.username.trim(),
          password: this.registerForm.password,
          turnstile_token: this.turnstileToken || undefined,
          referral_code: this.registerForm.referralCode.trim() || undefined
        })
        if (res.code === 1 && res.data?.token) {
          showToast({ message: this.$t('login.register_success'), type: 'success' })
          await this.finalizeLogin(res.data.token, res.data.userinfo)
        } else {
          showToast({ message: res.msg || this.$t('login.register_fail'), type: 'fail' })
          this.resetTurnstile()
        }
      } catch (err) {
        console.error('Register error:', err)
        this.resetTurnstile()
      } finally {
        this.loading = false
      }
    },

    async handleReset() {
      if (this.forgotForm.newPassword !== this.forgotForm.confirmPassword) {
        showToast({ message: this.$t('login.password_mismatch'), type: 'fail' })
        return
      }
      this.loading = true
      try {
        const res = await authApi.resetPassword({
          email: this.normalizeEmail(this.forgotForm.email),
          code: this.forgotForm.code.trim(),
          new_password: this.forgotForm.newPassword,
          turnstile_token: this.turnstileToken || undefined
        })
        if (res.code === 1) {
          showToast({ message: this.$t('login.reset_success'), type: 'success' })
          this.loginForm.username = this.forgotForm.email
          this.loginForm.password = ''
          this.forgotForm = { email: '', code: '', newPassword: '', confirmPassword: '' }
          this.switchMode('login')
        } else {
          showToast({ message: res.msg || this.$t('login.reset_fail'), type: 'fail' })
          this.resetTurnstile()
        }
      } catch (err) {
        console.error('Reset error:', err)
        this.resetTurnstile()
      } finally {
        this.loading = false
      }
    }
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  padding: 26px 18px 24px;
  display: flex;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

.login-shell {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 420px;
  margin: 0 auto;
  padding: 26px 18px 20px;
  border-radius: 30px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-pop);
}

.login-toolbar {
  display: flex;
  justify-content: flex-end;
  margin: -6px -4px 4px;
}

.lang-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 44px;
  height: 36px;
  padding: 0 12px;
  border: none;
  border-radius: 12px;
  background: var(--surface-raised);
  border: 1px solid var(--border);
  color: var(--text-2);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.02em;
  cursor: pointer;
  transition: background 0.2s ease, color 0.2s ease;
}

.lang-btn:active {
  background: var(--surface-raised-2);
  color: var(--accent);
}

.login-header {
  text-align: center;
  margin-bottom: 20px;
}

.logo-wrap {
  width: 92px;
  height: 92px;
  margin: 0 auto 14px;
  border-radius: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface-raised);
  border: 1px solid var(--border);
}

.logo-image {
  width: 68px;
  height: 68px;
  object-fit: contain;
  filter: drop-shadow(0 12px 24px rgba(0, 0, 0, 0.18));
}

.subtitle {
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.55;
  padding: 0 8px;
}

.tab-bar {
  display: flex;
  padding: 4px;
  margin-bottom: 20px;
  border-radius: 16px;
  background: var(--surface-raised);
  border: 1px solid var(--border);
}

.tab {
  flex: 1;
  text-align: center;
  padding: 10px 0;
  font-size: 13px;
  color: var(--text-2);
  border-radius: 12px;
  transition: all 0.2s ease;
  cursor: pointer;
}

.tab.active {
  background: var(--accent);
  color: var(--text-on-accent);
  font-weight: 700;
}

.login-form {
  display: flex;
  flex-direction: column;
}

.form-item {
  margin-bottom: 14px;
}

.input-wrapper {
  display: flex;
  align-items: center;
  min-height: 54px;
  background: var(--surface-raised);
  border-radius: 16px;
  padding: 0 14px;
  border: 1px solid var(--border);
  transition: all 0.24s ease;
}

.input-wrapper:focus-within {
  border-color: var(--accent);
  background: var(--surface-raised-2);
  box-shadow: 0 0 0 4px var(--accent-soft);
}

.input-icon {
  color: var(--text-3);
  font-size: 18px;
  margin-right: 10px;
}

.input {
  flex: 1;
  min-width: 0;
  height: 52px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 15px;
  outline: none;
}

.input::placeholder {
  color: var(--text-3);
}

.eye-icon {
  color: var(--text-3);
  font-size: 18px;
  padding: 8px;
  margin-right: -6px;
}

.code-btn {
  flex-shrink: 0;
  height: 34px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid transparent;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.code-btn:disabled {
  color: var(--text-4);
  background: var(--surface-raised);
  border-color: var(--border);
  cursor: not-allowed;
}

.row-between {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 6px 2px 16px;
}

.row-agreement {
  margin: 4px 2px 16px;
}

.row-between :deep(.van-checkbox__label),
.row-agreement :deep(.van-checkbox__label) {
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.5;
}

.agree-line {
  white-space: normal;
}

.link-inline {
  color: var(--accent);
  text-decoration: underline;
  cursor: pointer;
  font-weight: 600;
}

.legal-popup-root :deep(.van-popup__content) {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.legal-popup {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 12px 14px 16px;
  box-sizing: border-box;
}

.legal-popup__head {
  display: flex;
  gap: 10px;
  margin-bottom: 10px;
  flex-shrink: 0;
}

.legal-tab {
  flex: 1;
  text-align: center;
  padding: 10px 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  border-radius: 12px;
  background: var(--surface-raised);
  border: 1px solid var(--border);
  cursor: pointer;
}

.legal-tab.active {
  color: var(--text-on-accent);
  background: var(--accent);
  border-color: transparent;
}

.legal-popup__body {
  flex: 1;
  overflow: auto;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-2);
  white-space: pre-wrap;
  padding: 4px 2px 12px;
}

.legal-popup__foot {
  flex-shrink: 0;
  padding-top: 4px;
}

.link {
  font-size: 13px;
  color: var(--accent);
  cursor: pointer;
}

.security-panel {
  margin-bottom: 16px;
}

.security-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
}

.security-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
}

.security-desc {
  font-size: 12px;
  color: var(--text-3);
}

.turnstile-card {
  padding: 14px 12px 10px;
  border-radius: 16px;
  background: var(--surface-raised);
  border: 1px solid var(--border);
}

.turnstile-mount {
  min-height: 70px;
}

.turnstile-error {
  margin-top: 8px;
  font-size: 12px;
  color: var(--down);
  line-height: 1.5;
}

.submit-btn {
  height: 54px;
  border-radius: 16px;
  font-size: 16px;
  font-weight: 700;
  margin-top: 2px;
}

.submit-btn:disabled {
  opacity: 0.45;
}

.alt-link {
  text-align: center;
  margin-top: 16px;
}

.footer {
  text-align: center;
  margin-top: 14px;
}

.footer p {
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
}
</style>
