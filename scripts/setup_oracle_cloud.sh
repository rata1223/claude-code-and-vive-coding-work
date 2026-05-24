#!/bin/bash
# Oracle Cloud Ubuntu 22.04 최초 1회 실행 스크립트
# 사용법: bash scripts/setup_oracle_cloud.sh

set -e

echo "=== Oracle Cloud 서버 초기 설정 시작 ==="

# 패키지 업데이트
sudo apt-get update && sudo apt-get upgrade -y

# Docker 설치
if ! command -v docker &> /dev/null; then
  echo "Docker 설치 중..."
  curl -fsSL https://get.docker.com | sudo bash
  sudo usermod -aG docker $USER
  echo "Docker 설치 완료"
fi

# Docker Compose V2 확인 (Docker에 기본 포함)
docker compose version || (echo "Docker Compose 없음. Docker 재설치 필요" && exit 1)

# 방화벽 설정 (Oracle Cloud는 iptables 사용)
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

# 앱 디렉토리
APP_DIR="$HOME/kis-trading"
mkdir -p "$APP_DIR"

# 저장소 클론 (이미 있으면 pull)
REPO_URL="https://github.com/rata1223/claude-code-and-vive-coding-work.git"
if [ -d "$APP_DIR/.git" ]; then
  echo "저장소 업데이트 중..."
  git -C "$APP_DIR" pull
else
  echo "저장소 클론 중..."
  git clone "$REPO_URL" "$APP_DIR"
fi

# .env 파일 생성 안내
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo ""
  echo "========================================================"
  echo "⚠️  중요: $APP_DIR/.env 파일을 열어 실제 값을 입력하세요"
  echo "   nano $APP_DIR/.env"
  echo "========================================================"
fi

echo ""
echo "=== 설정 완료 ==="
echo "다음 단계:"
echo "  1. nano $APP_DIR/.env  # 자격증명 입력"
echo "  2. cd $APP_DIR && docker compose up -d  # 서비스 시작"
echo "  3. docker compose logs -f kis-bot  # 봇 로그 확인"
