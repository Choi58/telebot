# Telebot + n8n 필수 설정

## 1) Python 의존성
```bash
pip install -r requirements.txt
```

## 2) n8n 설치 및 컨테이너 실행

### 2-1) Docker/Compose 설치 확인
```bash
docker --version
docker compose version
```

### 2-2) n8n 단독 컨테이너 실행 (로컬 테스트용)
```bash
docker run -d \
  --name n8n \
  -p 5678:5678 \
  -e WEBHOOK_URL=https://<ngrok-domain>/ \
  -e N8N_EDITOR_BASE_URL=https://<ngrok-domain>/ \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n
```

접속:
```bash
http://localhost:5678
```

중지/재시작:
```bash
docker stop n8n
docker start n8n
```

## 3) `.env` 필수값
```env
TELEGRAM_BOT_TOKEN=
LM_STUDIO_BASE_URL=http://host.docker.internal:1234/v1
LM_STUDIO_MODEL=google/gemma-3-4b
LM_STUDIO_EMBEDDING_MODEL=nomic-embed-text-v1.5
PDF_DIR=./Papers
```

## 4) 로컬 API 실행 (n8n이 호출)
```bash
python -m uvicorn telebot_api:app --host 0.0.0.0 --port 8000
```

헬스체크:
```bash
curl http://127.0.0.1:8000/health
```

## 5) GROBID 컨테이너 실행
```bash
docker pull lfoppiano/grobid:latest-crf
docker run -d --name grobid -p 8070:8070 lfoppiano/grobid:latest-crf
curl http://127.0.0.1:8070/api/isalive
```

## 6) n8n 워크플로우 핵심값
`Call Daily Briefing API` 노드 Body:
```json
{
  "chat_id": 8623134647,
  "session_id": "8623134647",
  "max_papers": 1
}
```

- `max_papers: 1`은 하루 1개 논문 브리핑 설정
- `session_id`는 사용자 단위 상태/캐시 키

## 7) Notion 연결 (Credentials 방식)
1. n8n Credentials에서 `HTTP Header Auth` 생성
2. Header Name: `Authorization`
3. Header Value: `Bearer <NOTION_PRIVATE_API_SECRET>`
4. Notion DB 2개 생성
   - `Paper Summaries`
   - `QA Logs`
5. 각 DB 페이지 `Share`에서 Integration `노션` 추가

워크플로우 내 DB ID:
- Summary DB ID: `343eaf7ffa74807ab5bce732e2fe23d3`
- QA DB ID: `343eaf7ffa7480808c17e398f070dfe4`

Notion 노드 헤더(키-값으로 추가):
- `Notion-Version: 2022-06-28`
- `Content-Type: application/json`

## 8) Telegram Trigger 주의
`Telegram Trigger`는 웹훅 기반이라 n8n이 `HTTPS`로 외부 노출되어야 동작합니다.

옵션:
- 정식: `deploy/n8n`의 Caddy + 도메인 구성
- 임시: ngrok/Cloudflare Tunnel 사용

### 8-1) ngrok로 HTTPS 임시 주소 만들기 (빠른 테스트)
1. ngrok 설치:
```bash
brew install ngrok
```

2. authtoken 등록:
```bash
ngrok config add-authtoken <YOUR_NGROK_AUTHTOKEN>
```

3. n8n 포트 터널 오픈:
```bash
ngrok http 5678
```

4. 출력된 `https://...` 주소를 n8n 웹훅 베이스 URL로 사용:
- `WEBHOOK_URL=https://<ngrok-domain>/`
- `N8N_EDITOR_BASE_URL=https://<ngrok-domain>/`
- n8n 재시작 후 Telegram Trigger를 다시 활성화

이미 실행 중인 n8n 컨테이너가 있으면:
```bash
docker rm -f n8n
docker run -d \
  --name n8n \
  -p 5678:5678 \
  -e WEBHOOK_URL=https://<ngrok-domain>/ \
  -e N8N_EDITOR_BASE_URL=https://<ngrok-domain>/ \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n
```

정식 배포:
```bash
cd deploy/n8n
cp .env.example .env
# .env에서 N8N_DOMAIN 설정
docker compose up -d
```

## 9) 자주 발생하는 오류
- `ECONNREFUSED ...:8000`
  - `telebot_api` 미실행 상태. 4번 먼저 실행 필요
- `Could not find database with ID`
  - DB ID 오류 또는 DB를 Integration과 Share 안 함
- `Notion-Version header ... undefined`
  - Notion 노드 헤더 미설정
- `bad webhook: An HTTPS URL must be provided`
  - Telegram Trigger에 HTTPS 미구성
- `http://localhost:5678/webhook-test/...`로만 보임
  - test URL은 에디터 테스트 전용. 워크플로우 `Activate` 후 production `/webhook/...` 사용
