export const EXCHANGE_BRANDS = {
  kis: {
    name: '한국투자증권',
    short: 'KIS',
    brandBg: 'rgba(0, 100, 255, 0.18)',
    brandColor: '#0064ff'
  },
  kiwoom: {
    name: '키움증권',
    short: 'KW',
    brandBg: 'rgba(255, 50, 50, 0.18)',
    brandColor: '#ff3232'
  }
}

export const EXCHANGE_OPTIONS = Object.keys(EXCHANGE_BRANDS).map((id) => ({
  value: id,
  label: EXCHANGE_BRANDS[id].name
}))

export const EXCHANGE_SIGNUP_CARDS = []
