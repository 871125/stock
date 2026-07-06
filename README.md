# 코인 자동매매 백테스팅 시스템

변곡점(Swing High/Low) 기반 시장 구조 분석과 MTF(4h/1h) 추세 추종 + RSI 박스권
전략을 백테스트하고, 결과를 차트/테이블로 확인할 수 있는 시스템입니다. 매매 로직
전체 명세는 [`docs/spec.md`](docs/spec.md)를 참고하세요.

## 구조

```
backend/    FastAPI 백엔드 (백테스트 엔진, BingX 데이터 연동)
frontend/   React + Vite 프론트엔드 (백테스트 UI)
docs/       매매 로직 명세서
```

## 사전 준비물

- Python 3.12 이상
- Node.js 20 이상

## 백엔드 실행 (backend)

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

환경 변수가 필요하면 `.env.example`을 복사해 `.env`로 만들고 값을 채워주세요
(시세 조회만 사용할 경우 BingX API 키는 없어도 동작합니다).

```bash
cp .env.example .env
```

개발 서버 실행 (`--host 0.0.0.0`으로 띄워야 같은 네트워크의 다른 기기에서도
접근할 수 있습니다. `127.0.0.1`로만 띄우면 로컬에서는 되지만 LAN의 다른
기기에서는 연결 자체가 거부되어 "네트워크 에러"가 나고 백엔드 콘솔에는
아무 로그도 남지 않습니다):

```bash
uvicorn app.main:app --reload --port 8000 --host 0.0.0.0
```

- 서버: http://127.0.0.1:8000 (로컬) / `http://<LAN IP>:8000` (같은 네트워크)
- 헬스체크: `GET /health`
- 백테스트 실행: `POST /backtest`

테스트 및 린트:

```bash
pytest
ruff check .
black . --check
```

## 프론트엔드 실행 (frontend)

```bash
cd frontend
npm install
npm run dev
```

- 개발 서버: http://localhost:5173 (백엔드가 http://127.0.0.1:8000 에서 실행 중이어야 합니다)
- `vite.config.ts`에서 `server.host: true`로 설정되어 있어 기본적으로 `0.0.0.0`에
  바인딩됩니다 (LAN의 다른 기기에서도 접근 가능).

빌드 / 린트:

```bash
npm run build
npm run lint
```

## 사용 방법

1. 위 순서대로 백엔드와 프론트엔드를 각각 실행합니다.
2. 브라우저에서 http://localhost:5173 접속.
3. 코인 심볼(예: `BTC-USDT`)과 조회 기간을 입력하고 "Run Backtest"를 클릭합니다.
4. HTF(4h)/LTF(1h) 캔들 차트에 변곡점과 진입(EP)/손절(SL)/익절(TP)이 번호와 함께
   표시되고, 아래 거래 테이블과 요약 통계에서 결과를 확인할 수 있습니다.

## 같은 네트워크(LAN)의 다른 기기에서 접근하기

위 명령대로 백엔드/프론트엔드를 실행하면 둘 다 이미 `0.0.0.0`에 바인딩되어
있어 별도 설정이 필요 없습니다.

1. 백엔드를 실행한 PC의 LAN IP를 확인합니다 (`ipconfig`의 IPv4 주소, 예: `192.168.0.10`).
2. 다른 기기의 브라우저에서 `http://192.168.0.10:5173` 으로 접속합니다.
   - 프론트엔드는 접속한 주소(hostname)를 기준으로 백엔드 API 주소
     (`http://192.168.0.10:8000`)를 자동으로 사용하므로 별도 설정이 필요 없습니다.
   - 백엔드 CORS는 `localhost` / `127.0.0.1` / 사설 IP 대역의 `:5173` 출처만
     허용하도록 되어 있습니다 (`backend/app/core/config.py`의 `cors_origin_regex`).

> ⚠️ 이 설정은 **같은 LAN 안에서의 접근**을 위한 것입니다. 라우터 포트포워딩 등으로
> 인터넷에 직접 노출하는 것은 별개의 문제입니다 — 이 API는 별도 인증이 없으므로
> 실제로 외부 인터넷에 공개하려면 인증/방화벽 등 보안 조치를 먼저 추가하세요.

## 참고

- 시세 데이터는 BingX 공개 API(`/openApi/swap/v3/quote/klines`)에서 가져오며,
  거래소 쪽 레이트리밋(1req/s)에 맞춰 자동으로 페이징합니다.
- Bot(실매매) 개발은 백테스트 결과가 검증된 이후 진행합니다 (명세서 6장 참고).
