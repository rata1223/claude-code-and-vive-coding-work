export const DEFAULT_SERVER_URL = ''

export const PUBLIC_WEB_BASE_URL =
  (typeof import.meta !== 'undefined' && import.meta.env?.VITE_PUBLIC_WEB_BASE_URL) ||
  ''

export const DEFAULT_THEME = 'dark'
