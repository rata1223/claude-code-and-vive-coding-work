import { defineStore } from 'pinia'

export const useNotificationStore = defineStore('notification', {
  state: () => ({
    notifications: [],
    unreadCount: 0
  }),

  actions: {
    setNotifications(list) {
      this.notifications = Array.isArray(list) ? list : []
      this.unreadCount = this.notifications.filter((item) => !item.is_read && !item.read).length
    },

    setUnreadCount(count) {
      this.unreadCount = Number(count || 0)
    },

    markAsRead(id) {
      const notification = this.notifications.find((item) => item.id === id)
      if (notification && !notification.is_read && !notification.read) {
        notification.is_read = 1
        notification.read = true
        this.unreadCount = Math.max(0, this.unreadCount - 1)
      }
    },

    markAllAsRead() {
      this.notifications.forEach((item) => {
        item.is_read = 1
        item.read = true
      })
      this.unreadCount = 0
    }
  }
})
