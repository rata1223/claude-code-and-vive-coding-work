#!/bin/bash
# 서비스 상태 확인
echo "=== 서비스 상태 ==="
docker compose ps

echo ""
echo "=== 최근 봇 로그 (50줄) ==="
docker compose logs --tail=50 kis-bot
