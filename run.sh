#!/bin/bash
# 전국 병원 비급여진료비 조회 앱 실행 스크립트

cd "$(dirname "$0")"

# .env 파일에서 API 키 로드
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "$HIRA_API_KEY" ]; then
  echo ""
  echo "⚠️  API 키가 설정되지 않았습니다."
  echo "    .env 파일을 만들고 아래 내용을 입력하세요:"
  echo ""
  echo "    HIRA_API_KEY=발급받은키"
  echo ""
  echo "    샘플 데이터 없이 실행합니다 (검색 불가)."
  echo ""
fi

echo "✅  http://localhost:5000 으로 접속하세요"
echo "    (종료: Ctrl+C)"
echo ""

python3 app.py
