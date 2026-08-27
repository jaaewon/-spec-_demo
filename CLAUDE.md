# CLAUDE.md — 자연어 → Strategy Spec 변환 데모

> 전략 컴파일러 전체 시스템 중 **"자연어 → Spec(JSON) 변환"** 파이프라인만 떼어낸 **구현 가능성 입증(PoC) 데모**.
> 목표: 설문형 사용자 입력을 로컬 LLM이 판단하여 **정해진 JSON 스키마**로 변환·저장할 수 있음을 보인다.

---

## 1. 데모 목표 & 성공 기준

- **중점**: 자연어/설문 입력이 **특정 JSON(Strategy Spec) 형식**으로 안정적으로 변환되는가.
- **성공 기준**:
  1. 설문 제출 → 스키마를 100% 만족하는 Spec JSON이 반환된다 (구조화 출력 강제).
  2. ETF는 지정된 **KRX 비레버리지 유니버스** 안에서만 선정된다 (환각·레버리지 차단).
  3. 원문(설문/자연어)과 생성된 Spec가 PostgreSQL에 함께 저장된다.
- **타임박스**: 약 2시간.

## 2. 범위 (Scope)

### 포함
- 사용자 요청 방식: **설문형** UI + 핸들러 (직접 구현)
- 로컬 LLM 자체 판단 → Spec 생성 (구조화 출력 강제)
- Spec 스키마 검증 (Pydantic 스키마 수준까지만)
- 원문 + Spec 저장 (PostgreSQL)

### 제외 (이번 데모에서 구현하지 않음)
- 시스템 하드캡 (max_loss 상한/MDD/리밸런싱 최소간격 등 코드 조건문) — 스키마 검증까지만
- Building Block Library RAG (FAISS)
- 경제지표 DB 조회 (피처 저장소)
- 백테스팅 / 실전 거래 / 승인 게이트 / 리포트 생성
- 되묻기(clarification) 다중턴 루프 — 설문이 슬롯을 채우므로 **1-shot**으로 처리 (필요 시 확장)

### 특징 / 제약
- **한국거래소(KRX) 상장 ETF만** 대상.
- **레버리지/인버스 종목 제외** (`2X`, `레버리지`, `인버스`, `곱버스` 명칭 필터 + 유니버스 화이트리스트).

## 3. 아키텍처

```
[Browser: 설문 HTML]
        │  POST /compile (설문 응답 JSON)
        ▼
[FastAPI]
  1) 설문 응답 → 자연어/슬롯 프롬프트 구성
  2) etf_universe.json 주입
  3) Ollama 호출 (format = StrategySpec JSON Schema, temperature=0)
        │
        ▼
[Ollama: Qwen 3.6]  ── 문법 제약으로 스키마 밖 출력 생성 불가
        │  JSON 문자열
        ▼
  4) Pydantic 검증 (스키마 + ETF 유니버스/레버리지 validator)
  5) version/snapshot/seed 박제
  6) PostgreSQL 저장 (requests + specs)
        │
        ▼
[Browser: Spec JSON 렌더링]
```

- **Ollama는 컨테이너 대신 호스트 실행 권장** (GPU 패스스루가 도커에서 번거로움). compose에서는 `host.docker.internal:11434`로 접근.

## 4. Strategy Spec 스키마 (핵심)

`app/schemas.py` — Pydantic v2. **이 스키마가 Ollama `format` 파라미터로 그대로 주입되어 출력이 강제된다.**

```python
from datetime import date
from enum import Enum
from pydantic import BaseModel, Field, field_validator

class RebalanceFreq(str, Enum):
    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"

class RiskProfile(str, Enum):
    conservative = "conservative"
    neutral = "neutral"
    aggressive = "aggressive"

class SignalRule(BaseModel):
    indicator: str = Field(description="예: momentum_20d, rsi_14, ma_cross_5_20")
    operator: str = Field(description="'>', '<', '>=', '<=', '==' 중 하나")
    threshold: float
    action: str = Field(description="'buy' 또는 'sell'")

class StrategySpec(BaseModel):
    version: int = 1
    etfs: list[str] = Field(description="반드시 제공된 유니버스 내 종목명만")
    signals: list[SignalRule]
    rebalance: RebalanceFreq
    max_loss_pct: float = Field(ge=0, le=100)
    risk_profile: RiskProfile
    snapshot_date: date
    rationale: str = Field(description="선정 근거 1~2문장 (한국어)")

    # 유니버스 밖 / 레버리지 차단은 서버측 validator에서 (아래 §8)
```

> **폴백(더 단순)**: 시간이 부족하면 `signals: list[SignalRule]`를 예시처럼 `signal: str`("20일 모멘텀 > 0 이면 매수") 문자열 하나로 축소. 단, 구조화 버전이 "JSON 형식 변환 가능성 입증"에는 더 설득력 있으므로 기본값으로 둔다.

**예시 출력**
```json
{
  "version": 1,
  "etfs": ["KODEX 반도체"],
  "signals": [{"indicator": "momentum_20d", "operator": ">", "threshold": 0, "action": "buy"}],
  "rebalance": "monthly",
  "max_loss_pct": 5,
  "risk_profile": "neutral",
  "snapshot_date": "2026-08-27",
  "rationale": "반도체 강세 국면에서 20일 모멘텀 추세추종, 중립 위험성향에 맞춘 월 리밸런싱."
}
```

## 5. 설문 설계 (질문 → 슬롯 매핑)

프론트 설문 응답을 그대로 프롬프트 슬롯으로 사용. 각 항목이 Spec 필드로 매핑된다.

| 설문 질문 | 선택지(예) | → Spec 매핑 |
|---|---|---|
| 관심 섹터/테마 | 반도체 / 2차전지 / 배당 / 대형주(200) / 코스닥 / 미국주식 | `etfs` 후보 필터 |
| 투자 성향 | 안정형 / 중립형 / 공격형 | `risk_profile`, `max_loss_pct` 힌트 |
| 감내 가능 손실 | 3% / 5% / 10% | `max_loss_pct` |
| 매매 스타일 | 추세추종(모멘텀) / 역추세 / 이평선 교차 | `signals.indicator` |
| 리밸런싱 주기 | 주 1회 / 월 1회 / 분기 1회 | `rebalance` |
| 자유 서술(선택) | 텍스트 | 프롬프트에 원문 그대로 첨부 |

> 설문은 슬롯을 대부분 채우므로 LLM은 "섹터→ETF 선정, 스타일→signal 구체화, 성향→임계값 판단"을 수행한다. 이것이 **로컬 LLM 자체 판단** 지점.

## 6. LLM 통합

- **모델**: `qwen3:8b` **확정** (MacBook M4 16GB 통합메모리 기준). 폴백: `exaone3.5:7.8b` (한국어 특화, 이미 로컬 보유).
- **주의**: qwen3는 하이브리드 thinking 모델. `format` 문법 제약과 충돌하지 않도록 **`think=False`** 명시.
- **온도 0**, structured output 강제.
- **구조화 출력 강제 (권장 · 로컬 최강)**: Ollama 네이티브 `format`에 Pydantic JSON Schema 주입.
  ```python
  import ollama
  from app.schemas import StrategySpec

  resp = ollama.chat(
      model="qwen3:8b",
      think=False,               # qwen3 thinking 비활성 (format과 충돌 방지)
      messages=[{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}],
      format=StrategySpec.model_json_schema(),
      options={"temperature": 0},
  )
  spec = StrategySpec.model_validate_json(resp["message"]["content"])
  ```
- **대안(전체 시스템 스택과 일관성 원하면)**: Instructor. Instructor도 Ollama(OpenAI 호환 엔드포인트)를 지원하나, 로컬 모델에서는 위 네이티브 `format`이 문법 제약으로 더 안정적. 데모에선 네이티브 방식 권장.

**시스템 프롬프트 골자** (`app/prompt.py`)
```
너는 '전략 컴파일러'다. 아래 설문 응답을 StrategySpec JSON으로 변환한다.
규칙:
- etfs는 <UNIVERSE>에 나열된 종목명 중에서만 선택. 목록에 없는 종목 생성 금지.
- 레버리지/인버스 종목은 절대 선택 금지.
- snapshot_date는 오늘({today}).
- version은 1.
- 반드시 스키마에 맞는 JSON만 출력. 설명·마크다운 금지.
<UNIVERSE>
{universe_names}
</UNIVERSE>
```

## 7. ETF 유니버스 처리

- **`data/etf_universe.json`** (정적 화이트리스트). 프롬프트에 종목명 주입 + validator 근거로 사용.
- 스타터(대표 KRX 비레버리지 ETF — **종목코드는 KRX/데이터 제공처에서 검증 후 확정**):

```json
[
  {"name": "KODEX 200",        "code": "069500", "theme": "대형주"},
  {"name": "TIGER 200",        "code": "102110", "theme": "대형주"},
  {"name": "KODEX 반도체",      "code": "091160", "theme": "반도체"},
  {"name": "TIGER 반도체",      "code": "091230", "theme": "반도체"},
  {"name": "KODEX 2차전지산업",  "code": "305720", "theme": "2차전지"},
  {"name": "KODEX 코스닥150",    "code": "229200", "theme": "코스닥"},
  {"name": "KODEX 배당가치",     "code": "325020", "theme": "배당"},
  {"name": "TIGER 미국S&P500",   "code": "360750", "theme": "미국주식"},
  {"name": "KODEX 종합채권",     "code": "273130", "theme": "채권"}
]
```
> 데모용 9~30종목이면 충분. 확장 시 명칭에 `레버리지/인버스/2X/곱버스` 포함 종목은 로드 단계에서 제외.

## 8. 서버측 검증 (Pydantic validator)

스키마 통과 후에도 아래를 강제 (하드캡은 제외, **유니버스/레버리지 무결성만**):

```python
LEVERAGE_KEYWORDS = ("레버리지", "인버스", "2X", "곱버스")

def validate_etfs(etfs: list[str], universe: set[str]) -> list[str]:
    for name in etfs:
        if name not in universe:
            raise ValueError(f"유니버스 밖 종목: {name}")
        if any(k in name for k in LEVERAGE_KEYWORDS):
            raise ValueError(f"레버리지/인버스 금지: {name}")
    return etfs
```
- 실패 시: 400 + 사유 반환 (데모에선 1회 재시도 정도만, 무한 루프 금지).

## 9. API 설계 (FastAPI)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 설문 HTML 서빙 |
| POST | `/compile` | 설문 응답 → LLM → 검증 → 저장 → Spec JSON 반환 |
| GET | `/specs` | 저장된 Spec 목록 (검증/데모 확인용) |
| GET | `/health` | Ollama·DB 헬스체크 |

`POST /compile` 요청/응답 예:
```jsonc
// req
{ "sector": "반도체", "risk": "neutral", "max_loss": 5,
  "style": "momentum", "rebalance": "monthly", "note": "" }
// res
{ "request_id": 12, "spec": { ...StrategySpec... } }
```

## 10. DB 스키마 (PostgreSQL)

```sql
CREATE TABLE requests (
    id          SERIAL PRIMARY KEY,
    survey      JSONB NOT NULL,          -- 설문 응답 원본
    nl_text     TEXT,                    -- 자유 서술/조합된 자연어
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE specs (
    id          SERIAL PRIMARY KEY,
    request_id  INT REFERENCES requests(id),
    spec        JSONB NOT NULL,          -- StrategySpec JSON
    version     INT NOT NULL DEFAULT 1,
    model       TEXT,                    -- 사용 모델 태그
    created_at  TIMESTAMPTZ DEFAULT now()
);
```
- ORM은 취향껏 (SQLAlchemy 또는 psycopg 직접). 데모 규모라 psycopg + 간단 쿼리로 충분.

## 11. 프론트엔드 (HTML)

- 단일 `index.html` (별도 빌드 없음). 폼 + `fetch('/compile')` + 결과 `<pre>` JSON 렌더.
- 스타일은 최소한. 섹션: ① 설문 폼 ② "컴파일" 버튼 ③ 결과 Spec JSON ④ (선택) 저장 이력.

## 12. Docker 구성

`docker-compose.yml` (개요):
```yaml
services:
  api:
    build: .
    ports: ["8000:8000"]
    environment:
      - DATABASE_URL=postgresql://demo:demo@db:5432/demo
      - OLLAMA_HOST=http://host.docker.internal:11434
      - OLLAMA_MODEL=qwen3:8b
    depends_on: [db]
    extra_hosts: ["host.docker.internal:host-gateway"]  # 리눅스에서 호스트 Ollama 접근
  db:
    image: postgres:16
    environment:
      - POSTGRES_USER=demo
      - POSTGRES_PASSWORD=demo
      - POSTGRES_DB=demo
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
volumes: { pgdata: {} }
```
> **Ollama는 호스트에서 실행**(`ollama serve` + `ollama pull qwen3:8b`). GPU 있는 로컬이면 이게 가장 단순. 도커 내부 GPU 패스스루로 가려면 시간이 더 든다.

## 13. 디렉토리 구조

```
.
├── CLAUDE.md
├── docker-compose.yml
├── Dockerfile
├── requirements.txt          # fastapi, uvicorn, pydantic, ollama, psycopg[binary]
├── app/
│   ├── main.py               # FastAPI 엔드포인트
│   ├── schemas.py            # StrategySpec 등 Pydantic
│   ├── prompt.py             # 시스템 프롬프트 빌더
│   ├── llm.py                # Ollama 호출 + 검증/재시도
│   ├── db.py                 # Postgres 연결/저장/조회
│   └── validators.py         # 유니버스/레버리지 검증
├── data/
│   └── etf_universe.json
└── static/
    └── index.html
```

## 14. 구현 순서 (2시간 타임박스)

1. **(15m)** 스캐폴딩: compose 기동, Postgres 테이블, `/health` 확인.
2. **(20m)** `schemas.py` + `etf_universe.json` 작성.
3. **(30m)** `llm.py`: Ollama `format` 호출 → Pydantic 파싱, temperature 0.
4. **(20m)** `validators.py` + `/compile` 엔드포인트 (검증·저장 연결).
5. **(20m)** `index.html` 설문 폼 + 결과 렌더.
6. **(15m)** 다양한 설문 조합 테스트, 유니버스 밖/레버리지 거부 확인, `/specs`로 저장 검증.

## 15. 검증 시나리오 (데모 시연용)

- 정상: "반도체 + 공격형 + 모멘텀 + 월1회" → 반도체 ETF, momentum signal, monthly Spec 생성.
- 경계: 자유서술로 "레버리지 반도체 담아줘" → validator가 거부 (레버리지 차단 입증).
- 경계: 유니버스에 없는 종목 유도 → 거부 or 유니버스 내 대체.
- 무결성: 같은 설문 2회 → 스키마 항상 유효(온도 0으로 재현성↑).

## 16. 확인 필요 사항 (Open Decisions)

1. ~~**하드웨어**~~ → **확정**: MacBook M4 16GB 통합메모리, 모델 `qwen3:8b`.
2. **signal 구조화 수준**: 구조화(`SignalRule` 리스트) 기본 vs 문자열 폴백 — 현재 구조화 기본.
3. **ETF 유니버스 규모**: 데모용 9종 스타터로 충분한지, 섹터별 확장 필요한지.
4. **Ollama 배치**: 호스트 실행(권장) vs 도커 내부(GPU 패스스루 필요).

---

## 부록 A. 하드웨어별 모델 매핑

| 환경 | 모델 | 메모 |
|---|---|---|
| **Mac M4 16GB (현 환경)** | **`qwen3:8b`** | ~5.2GB. Docker(Postgres+API)와 동거 가능. `think=False` 필수 |
| GPU 24GB | `qwen3.6:32b` (Q4) | 멀티링구얼·툴 작업 강세 |
| 한국어 폴백 | `exaone3.5:7.8b` | 로컬 보유. LG 한국어 특화, structured output 사전 확인 권장 |
| 폴백 | `qwen2.5:7b` | structured output 검증됨. 14B(Q4≈9GB)는 16GB에서 스왑 위험 |

- 한국어 특화가 더 필요하면 **EXAONE** 계열 테스트 가능(로컬 structured-output 안정성은 사전 확인 권장).
- 공통: **temperature=0**, Ollama `format`에 스키마 주입.