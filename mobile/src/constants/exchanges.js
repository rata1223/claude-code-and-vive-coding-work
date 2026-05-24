// 한국 증권사 브로커 정의 (암호화폐 거래소 제거)

export const EXCHANGE_BRANDS = {
  kis: {
    name: '한국투자증권',
    short: 'KIS',
    brandBg: 'rgba(0, 102, 204, 0.18)',
    brandColor: '#0066cc'
  },
  kiwoom: {
    name: '키움증권',
    short: 'KW',
    brandBg: 'rgba(220, 38, 38, 0.18)',
    brandColor: '#dc2626'
  }
}

export const EXCHANGE_OPTIONS = Object.keys(EXCHANGE_BRANDS).map((id) => ({
  value: id,
  label: EXCHANGE_BRANDS[id].name
}))

// 브로커 연결 안내 카드
export const EXCHANGE_SIGNUP_CARDS = [
  {
    id: 'kis',
    name: '한국투자증권',
    short: 'KIS',
    brandBg: 'rgba(0, 102, 204, 0.18)',
    brandColor: '#0066cc',
    signupUrl: 'https://www.truefriend.com'
  },
  {
    id: 'kiwoom',
    name: '키움증권',
    short: 'KW',
    brandBg: 'rgba(220, 38, 38, 0.18)',
    brandColor: '#dc2626',
    signupUrl: 'https://www.kiwoom.com'
  }
]
