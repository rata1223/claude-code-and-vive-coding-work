# KIS 자동매매 — 모바일 앱

<p align="right"><a href="README_CN.md">简体中文</a></p>

한국투자증권(KIS) 자동매매 플랫폼의 모바일·경량 웹 클라이언트다. **Vue 3 + Vite + Capacitor 6**으로 만들었고,
같은 웹 앱을 Android/iOS 네이티브 셸로 감싸거나 H5로 그대로 배포할 수 있다. 웹 앱(`../frontend`)과 화면·스토어
코드를 같이 쓴다(두 앱의 공유 파일은 정적 가드가 동일함을 확인한다).

플랫폼 전체 구조와 운영 방법은 저장소 루트의 `CLAUDE.md`를 본다.

## 서버 주소

앱은 **서버 기본 주소**(경로 없이 origin만)를 저장하고 모든 REST 호출을 `{base}/api/...`로 보낸다.
기본값은 `src/config/index.js`의 `DEFAULT_SERVER_URL`(빈 값)이고, 사용자가 설정 화면에서 바꾼다
(로컬에 저장된다). **설정 → 연결 테스트**는 `{base}/api/health`를 호출한다.

운영 전략 화면의 실시간 피드는 같은 서버의 `/socket.io`로 붙는다. 그 경로를 kis-ws로 프록시하는
곳은 웹 서버이므로, 피드를 쓰려면 서버 주소를 웹 서버로 둔다.

## 개발

```bash
npm install
npm run dev            # 개발 서버. /api는 VITE_API_TARGET(기본 http://localhost:8000)으로 프록시
npm run build          # dist/
npm run preview
```

## 네이티브 빌드

```bash
npm run build:android  # vite build + npx cap sync android
npm run build:ios      # vite build + npx cap sync ios
npm run cap:android    # Android Studio 열기
npm run cap:ios        # Xcode 열기
```

앱 식별자는 `capacitor.config.json`의 `com.kistrade.mobile`이다. 릴리스 서명은 `signing/README.txt` 참고
(키스토어와 비밀번호는 절대 커밋하지 않는다).

## 출처와 라이선스

이 앱은 QuantDinger-Mobile(소스 공개 라이선스, [`LICENSE`](LICENSE) 참고)을 기반으로 한다.
