<template>
  <div class="page">
    <van-nav-bar :title="$t('about.title')" left-arrow @click-left="$router.back()" />

    <div class="card intro">
      <h2 class="h">{{ $t('about.title') }}</h2>
      <p class="p">{{ $t('about.intro') }}</p>
    </div>

    <van-cell-group inset class="group">
      <van-cell :title="$t('about.app_version_label')" :value="localVersion" />
      <van-cell
        v-if="serverLatestVersion"
        :title="$t('about.server_version_label')"
        :value="serverLatestVersion"
      />
    </van-cell-group>

    <div class="actions">
      <van-button type="primary" block round :loading="checking" @click="checkUpdate">
        {{ checking ? $t('about.checking') : $t('about.check_update') }}
      </van-button>
    </div>

    <van-dialog
      v-model:show="updateDialog"
      :title="$t('about.update_available', { version: serverLatestVersion || '' })"
      show-cancel-button
      :confirm-button-text="$t('about.update_now')"
      :cancel-button-text="$t('common.cancel')"
      @confirm="onConfirmUpdate"
    >
      <p class="dialog-body">{{ $t('about.update_hint') }}</p>
    </van-dialog>
  </div>
</template>

<script>
import { showToast } from 'vant'
import { Capacitor } from '@capacitor/core'
import { Browser } from '@capacitor/browser'
import { authApi } from '@/api'
import { APP_BUILD_VERSION, isRemoteVersionNewer } from '@/constants/appVersion'

/** Only an https download address the server provides is ever opened — no
 *  built-in fallback (the upstream project's APK used to be one). */
const safeDownloadUrl = (value) => {
  const url = String(value ?? '').trim()
  return /^https:\/\/[^\s]+$/i.test(url) ? url : ''
}

export default {
  name: 'About',

  data() {
    return {
      localVersion: APP_BUILD_VERSION,
      serverLatestVersion: '',
      downloadUrl: '',
      checking: false,
      updateDialog: false
    }
  },

  mounted() {
    this.prefetchVersion()
  },

  methods: {
    async prefetchVersion() {
      try {
        const res = await authApi.getSecurityConfig()
        if (res.code === 1 && res.data) {
          const d = res.data
          this.serverLatestVersion = String(
            d.mobile_app_latest_version ?? d.mobileAppLatestVersion ?? ''
          ).trim()
          this.downloadUrl = safeDownloadUrl(d.mobile_app_download_url ?? d.mobileAppDownloadUrl)
        }
      } catch (e) {
        console.warn('About prefetch version:', e)
      }
    },

    async checkUpdate() {
      this.checking = true
      try {
        const res = await authApi.getSecurityConfig()
        if (res.code !== 1 || !res.data) {
          showToast({ message: this.$t('about.fetch_config_fail'), type: 'fail' })
          return
        }
        const d = res.data
        const latest = String(d.mobile_app_latest_version ?? d.mobileAppLatestVersion ?? '').trim()
        this.downloadUrl = safeDownloadUrl(d.mobile_app_download_url ?? d.mobileAppDownloadUrl)
        this.serverLatestVersion = latest

        if (!latest) {
          showToast({ message: this.$t('about.up_to_date'), type: 'success' })
          return
        }
        if (isRemoteVersionNewer(latest, this.localVersion)) {
          if (this.downloadUrl) {
            this.updateDialog = true
          } else {
            showToast({ message: this.$t('about.update_no_url'), type: 'fail' })
          }
        } else {
          showToast({ message: this.$t('about.up_to_date'), type: 'success' })
        }
      } catch (e) {
        console.error(e)
        showToast({ message: this.$t('about.fetch_config_fail'), type: 'fail' })
      } finally {
        this.checking = false
      }
    },

    async onConfirmUpdate() {
      const url = safeDownloadUrl(this.downloadUrl)
      if (!url) return
      try {
        if (Capacitor.isNativePlatform()) {
          await Browser.open({ url, presentationStyle: 'fullscreen' })
        } else {
          window.open(url, '_blank', 'noopener,noreferrer')
        }
      } catch (e) {
        console.error(e)
        window.open(url, '_blank', 'noopener,noreferrer')
      }
    }
  }
}
</script>

<style scoped>
.page {
  min-height: 100vh;
  padding-bottom: 32px;
}

.card.intro {
  margin: 16px;
  padding: 18px 16px;
  border-radius: 16px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
}

.h {
  margin: 0 0 10px;
  font-size: 17px;
  font-weight: 700;
  color: var(--text);
}

.p {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-2);
}

.group {
  margin-top: 8px;
}

.actions {
  margin: 20px 16px 0;
}

.dialog-body {
  margin: 12px 16px 16px;
  font-size: 13px;
  line-height: 1.55;
  color: var(--text-2);
}

:deep(.van-nav-bar) {
  background: transparent;
}
:deep(.van-nav-bar .van-nav-bar__title),
:deep(.van-nav-bar .van-icon) {
  color: var(--text);
}
:deep(.van-cell-group--inset) {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
}
:deep(.van-cell) {
  background: transparent;
  color: var(--text);
}
:deep(.van-cell__title) {
  color: var(--text);
}
:deep(.van-cell__value) {
  color: var(--text-2);
}
</style>
