import { io } from 'socket.io-client'
import { getBaseUrl } from '@/api'

/**
 * The operator live feed from kis-ws (backend/websocket/server.py): order
 * updates and alerts for the worker's .env account, relayed from Redis.
 * kis-ws accepts only OPERATOR_USER_IDS; anyone else is refused at connect.
 *
 * The socket goes to the same server as the API, path /socket.io: the web
 * dev server proxies it to kis-ws (vite.config.js). The token is sent in the
 * handshake `auth` payload, never in the URL, and read again on every
 * reconnect so a fresh login is picked up.
 *
 * Status: connecting → connected; reconnecting while the transport retries;
 * refused when kis-ws rejects the token (not retried — log in again or check
 * the operator list); closed when kis-ws ends the socket (its token expired).
 */
export function createOperatorFeed({ onEvent, onStatus }) {
  const socket = io(getBaseUrl() || undefined, {
    path: '/socket.io',
    transports: ['websocket', 'polling'],
    autoConnect: false,
    reconnectionDelayMax: 30000,
    auth: (cb) => cb({ token: localStorage.getItem('token') || '' })
  })
  const status = (s) => onStatus && onStatus(s)

  socket.on('connect', () => status('connected'))
  socket.on('disconnect', (reason) => {
    if (reason === 'io client disconnect') return
    status(reason === 'io server disconnect' ? 'closed' : 'reconnecting')
  })
  // A rejected handshake leaves the socket inactive (no automatic retry);
  // a network error keeps it active and the client retries.
  socket.on('connect_error', () => status(socket.active ? 'reconnecting' : 'refused'))
  socket.on('order:update', (data) => onEvent && onEvent({ type: 'order', data, at: Date.now() }))
  socket.on('alert', (data) => onEvent && onEvent({ type: 'alert', data, at: Date.now() }))

  status('connecting')
  socket.connect()
  return {
    close() {
      socket.removeAllListeners()
      socket.disconnect()
    }
  }
}
