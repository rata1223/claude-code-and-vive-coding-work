# KIS + QuantDinger 자동매매 시스템

## 프로젝트 개요
한국투자증권(KIS) Open API를 사용해 미국·한국주식 ETF/대형주를 자동매매하는 시스템.
QuantDinger 웹 대시보드 위에 KIS 브로커 어댑터와 전략 엔진을 추가했다.

## 실행 방법
```bash
cp .env.example .env  # .env 파일 만들고 값 채우기
docker-compose up -d  # 전체 스택 실행
docker-compose logs -f kis-bot  # 봇 로그 확인
```

## 디렉토리 구조
- `kis_adapter/` — KIS Open API 인증·주문·시세·잔고
- `strategy/` — pandas-ta 신호, PyPortfolioOpt 비중, 리스크 관리
- `bot/` — 스케줄러, 텔레그램 알림, 메인 진입점
- `scripts/` — Oracle Cloud 최초 설치 스크립트

## 환경변수 (.env)
`.env.example` 참고. KIS_ENV=paper (모의) 또는 real (실전).

## 모의→실전 전환
4주 모의 운영 후 `.env`에서 `KIS_ENV=paper` → `KIS_ENV=real` 로만 변경.
절대 모의 검증 전 실전 전환 금지.

## KIS API 엔드포인트
- 모의: https://openapivts.koreainvestment.com:9443
- 실전: https://openapi.koreainvestment.com:9443

## Rate Limit
- 모의: 초당 5건 (client.py에서 자동 관리)
- 실전: 초당 15건 (보수적 사용)
