# 전역 작업 규칙

## 현재 프로젝트: HIRA 비급여진료비 조회 앱

### 프로젝트 개요
- **목적**: 전국 병원 비급여진료비 조회 서비스 (의료인 전용)
- **경로**: `/Users/youn/hira-app`
- **GitHub**: `git@github.com:gamza1671-ops/youn.git`
- **서버**: Python Flask, 포트 8080
- **주요 파일**:
  - `app.py` — Flask 라우트, 캐시, 검색, 챗봇 API
  - `templates/index.html` — 메인 검색 UI
  - `templates/login.html` — 로그인 페이지
  - `templates/change_password.html` — 비밀번호 변경
  - `.env` — API 키·시크릿 (git 제외)
  - `cache.pkl` — 전국 259,447건 캐시 (git 제외)

### 기술 스택
| 항목 | 내용 |
|------|------|
| 백엔드 | Python 3.9, Flask, xml.etree.ElementTree |
| 프론트엔드 | 순수 JS + 인라인 CSS (프레임워크 없음) |
| HIRA API | `https://apis.data.go.kr/B551182/nonPaymentDamtInfoService/getNonPaymentItemHospDtlList` |
| 응답 포맷 | XML → Python dict 파싱 |
| 캐시 | pickle (cache.pkl, 67MB, 24시간 TTL) |
| 챗봇 | Ollama `http://121.138.151.6:11500`, 모델 `qwen2.5:14b-instruct-q4_K_M` |
| 인증 | Flask session (`login_required` 데코레이터) |
| 환경변수 | `.env` 직접 파싱 (python-dotenv 미설치 대응) |

### 환경변수 (.env)
```
HIRA_API_KEY=<HIRA 공공데이터 API 키>
SECRET_KEY=<Flask 세션 시크릿>
SITE_PASSWORD=<사이트 접속 비밀번호>
OLLAMA_URL=http://121.138.151.6:11500  (선택)
```

### 서버 운영 명령어
```bash
# 서버 재시작
kill $(lsof -ti :8080) 2>/dev/null; cd /Users/youn/hira-app && nohup python3 app.py > /tmp/hira_server.log 2>&1 &

# 로그 확인
tail -f /tmp/hira_server.log

# 캐시 상태 확인
curl -s http://localhost:8080/api/cache-status | python3 -m json.tool
```

### 주요 API 엔드포인트
| 경로 | 메서드 | 역할 |
|------|--------|------|
| `/` | GET | 메인 검색 화면 (login_required) |
| `/login` | GET/POST | 로그인 |
| `/logout` | GET | 로그아웃 |
| `/change-password` | GET/POST | 비밀번호 변경 |
| `/api/search` | GET | 비급여 검색 (캐시→API→mock 순) |
| `/api/cache-status` | GET | 캐시 로딩 상태 + 데이터 기준 시각 |
| `/api/chat` | POST | Ollama 챗봇 스트리밍 (ndjson) |
| `/api/models` | GET | Ollama 모델 목록 |
| `/api/items` | GET | 자동완성용 항목 목록 |

### 검색 우선순위 로직
1. 캐시 ready → `search_cache()` (초고속, 전국 검색)
2. 캐시 로딩 중 → 직접 HIRA API 호출 (제한적)
3. API 키 없음 / API 오류 → `search_mock()` (샘플 데이터)

### 코딩 규칙
- 주석 최소화, 필요한 경우만 한국어로 작성
- 프론트엔드: 순수 JS, 인라인 CSS (jQuery/React 사용 금지)
- 보안: XSS 방지, SQL 인젝션 없음 (DB 미사용), 세션 검증 필수
- 캐시 파일(cache.pkl)과 .env는 절대 git에 포함하지 않음

### 히스토리 문서
- 경로: `/Users/youn/hira-app/history/YYYY-MM-DD_{주제-슬러그}.md`
- 포함 내용: 작업 목표·수행 내용·산출물 경로·이슈·다음 단계
- 세션이 바뀌어도 이 문서만 보면 이어서 작업할 수 있게 작성

## 실행 방식
- 모든 작업은 plan 모드로 시작 (실행 전 계획 먼저 제시)
- 팀 에이전트 구성으로 실행, tmux split pane 병렬 처리

## 스킬
- 모든 작업 시작 전 `using-superpowers` 스킬 적용

## 팀 구조
| 팀 | 역할 |
|----|------|
| hira-dev | Flask 백엔드, 검색 로직, 챗봇 연동, 인증 |
| hira-frontend | HTML/CSS/JS UI, 검색 필터, 결과 테이블 |
| hira-data | HIRA API 파싱, 캐시 관리, 데이터 정합성 |
| hira-ops | GitHub, 배포, 서버 운영, 바로가기 앱 |
| qa-team | 코드 리뷰, 보안 점검, 기능 테스트 |
