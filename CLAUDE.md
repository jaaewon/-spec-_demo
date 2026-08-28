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
- **경제지표 피처 저장소 (as-of 조회)** — 상위 기획서 P1 "피처 저장소" 마일스톤의 선행 PoC.
  정적 seed 적재 + 시점 정합적 조회(§17)까지. 프롬프트 주입·시장온도 판정은 안 함
- **시스템 하드캡 (Validator 4계층)** — 기획서 4.1 의 4번째 계층(§18).
  **수치는 클램프(200), 구조적 위반은 반려(400).** 캡 값은 코드 상수가 아니라
  `hardcap_profile` 테이블에서 요청마다 읽는다. **LLM 프롬프트에는 넣지 않는다** (§18.2)
  - 실제 발동: `max_loss_pct` 상한 · 논리 모순 반려
  - 값만 저장하고 판정은 스텁 3종 — 아래 "제외" 참고

### 제외 (이번 데모에서 구현하지 않음)
- 하드캡 **스텁 3종** — `hardcap_profile` 에 값은 있으나 검증 로직은 "판정 불가"를 반환한다.
  (조용히 통과시키지 않는다. 통과와 판정 불가를 구분하는 게 중요하다)
  | 항목 | 미적용 사유 | 재검토 시점 |
  |---|---|---|
  | MDD 상한 | 데모에 백테스트 계층이 없어 MDD 를 **산출할 수 없다**. Spec 만 보고는 판정 불가 | P3 백테스트 |
  | 최소 리밸런싱 간격 | 로직은 살아 있으나 현행 `RebalanceFreq` 최소 단위가 `weekly`(7일)이고 캡도 7일이라 **어떤 설문 응답도 위반할 수 없다**. 캡을 올리면 '주 1회' 선택지가 항상 클램프돼 정상 응답이 매번 조정된다 | enum 에 더 짧은 주기 추가 시 자동 발동 |
  | 단일종목 상한 | **현행 `StrategySpec` 에 종목별 비중 필드가 없다.** ETF 라서 불필요한 게 아니다 — ETF 내부 분산과 포트폴리오 내 ETF 비중은 별개 문제이고, 반도체 ETF 100% 배분은 여전히 섹터 집중 위험이다. 스키마 변경은 이번 범위 밖 | P3 배분 계층 |
- Building Block Library RAG (FAISS)
- 경제지표의 **소비** — 프롬프트 주입, 임계값 기반 시장온도/국면 판정 (판단 계층 소속, §17)
- 경제지표 **실제 API 연동** (ECOS/FRED) — 교체 지점만 함수 경계로 끊어둠 (§17)
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
  4) Pydantic 검증 (스키마 + ETF 유니버스/레버리지 validator)   ← 1~2계층, 실패 시 1회 재시도
  4-1) 하드캡 적용 (§18) — 수치는 클램프, 구조적 위반은 400.    ← 4계층, **재시도 루프 밖**
       하드캡 값은 프롬프트에 안 들어간다 (LLM 은 상한을 모른다)
  5) version/snapshot/seed 박제
  5-1) snapshot_date 를 as-of 키로 경제지표 조회 (§17) — 프롬프트엔 안 넣음, 기록만
  6) PostgreSQL 저장 (requests + specs, 지표 스냅샷 + 조정 내역 동봉)
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

## 8. 서버측 검증 — Validator 계층 구조

기획서 4.1 의 4계층 중 이 데모가 구현한 건 **1·2·4계층**이다.

| # | 계층 | 무엇을 보나 | 어디 | 실패 시 |
|---|---|---|---|---|
| 1 | 스키마 | 형태(타입·enum·범위) | `schemas.py` + Ollama `format` 문법 제약 | 애초에 생성 불가 |
| 2 | 참조 | 종목명이 유니버스에 실재하는가 | `validators.validate_etfs` | **반려 400** (1회 재시도 후) |
| 3 | 논리 | 규칙끼리 모순되지 않는가 | (전용 계층 없음 — 4계층 진입부의 `find_logical_contradictions` 가 겸함) | **반려 400** |
| 4 | 하드캡 | 시스템 상한을 넘지 않는가 | `validators.enforce_hardcaps` (§18) | **클램프 200** / 구조적 위반만 반려 400 |

### 8.1 2계층 (참조) — 기존 그대로

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

### 8.2 4계층 (하드캡) — 클램프와 반려의 구분

**두 계층의 실패 처리가 다른 이유**가 이 설계의 핵심이다.

- **참조 계층은 전부 반려한다.** 유니버스 밖 종목은 고쳐 줄 방법이 없다 —
  어느 종목으로 바꿔야 사용자 의도에 맞는지 서버가 알 수 없다.
- **하드캡 계층은 수치만 조정한다.** "손실 50% 감수"는 의도가 명확하니 상한으로
  깎아 살려주는 게 맞다. 사용자는 원하는 걸 얻고(200), 시스템은 상한을 지킨다.

| 위반 유형 | 처리 | 예 |
|---|---|---|
| **수치 초과** | **클램프 → 200.** 상한값으로 조정하고 내역을 `clamps` 에 기록 | `max_loss_pct: 50 → 20` |
| **구조적 위반** | **반려 → 400 + 사유.** 깎을 수치가 없다 | 동일 조건에 buy/sell 동시 지정 |

구조적 위반을 클램프하지 않는 이유: 모순된 규칙 중 하나를 서버가 임의로 골라 지우면
사용자가 요청하지 않은 전략이 만들어진다. 그건 조정이 아니라 조작이다.

자세한 내용(캡 값 근거, 프로파일 테이블, 스텁 3종)은 **§18**.

## 9. API 설계 (FastAPI)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 설문 HTML 서빙 |
| POST | `/compile` | 설문 응답 → LLM → 검증 → 저장 → Spec JSON 반환 |
| GET | `/specs` | 저장된 Spec 목록 (검증/데모 확인용) |
| GET | `/indicators?as_of=YYYY-MM-DD` | 해당 시점에 **공개돼 있던** 최신 경제지표 (§17). 생략 시 오늘 |
| GET | `/health` | Ollama·DB·지표 테이블·하드캡 프로파일 헬스체크 |

`POST /compile` 요청/응답 예:
```jsonc
// req
{ "sector": "반도체", "risk": "neutral", "max_loss": 5,
  "style": "momentum", "rebalance": "monthly", "note": "" }
// res
{ "request_id": 12,
  // **하드캡 클램프가 적용된 최종본.** 조정이 있었으면 아래 clamps 와 대조해서 읽는다.
  "spec": { ...StrategySpec... },
  // spec.snapshot_date 기준 as-of 로 뜬 경제지표 스냅샷 (§17).
  // 프롬프트에는 안 들어간다. 지표를 못 읽으면 {} — /compile 은 그래도 200.
  "indicators": { "KR_CPI_YOY": { "value": 2.4, "observation_date": "2026-07-01",
                                  "release_date": "2026-08-21", ... } },
  // 하드캡 조정 내역 (§18). **조정이 없으면 빈 배열** — 정상 요청의 기본 상태다.
  // 이 배열이 비어 있지 않아도 요청은 성공(200)이다. 거부가 아니라 조정이다.
  "clamps": [ { "field": "max_loss_pct",       // 어떤 필드가
                "requested": 25.0,             // 어떤 값에서
                "applied": 20.0,               // 어떤 값으로
                "cap": "max_loss_pct_cap",     // 어떤 캡 때문에
                "limit": 20.0,
                "reason": "1회 손실 한도 상한 20% 초과 (25%) — 상한값으로 조정" } ] }
```

**상태 코드 정리**

| 코드 | 언제 | 예 |
|---|---|---|
| 200 | 정상 + **하드캡 수치 클램프** | `clamps` 가 비었거나 조정 내역이 담긴다 |
| 400 | 참조 계층 반려 / 하드캡 **구조적 위반** | 유니버스 밖 종목, 동일 조건 buy·sell 동시 |
| 422 | `SurveyRequest` 스키마 위반 (LLM 호출 전) | enum 밖 섹터 |
| 503 | Ollama 장애 / **하드캡 프로파일 조회 실패** | 아래 참고 |

> 하드캡 프로파일을 못 읽으면 **503 (fail closed)**. 지표 계층이 실패해도 `{}` 로 넘어가
> 200 을 내는 것(§17)과 의도적으로 반대다 — 지표는 부가정보지만 하드캡은 안전 계층이라,
> 조용히 사라진 채로 Spec 을 내보내는 게 에러보다 나쁘다.

## 10. DB 스키마 (PostgreSQL)

```sql
CREATE TABLE requests (
    id          SERIAL PRIMARY KEY,
    survey      JSONB NOT NULL,          -- 설문 응답 원본
    nl_text     TEXT,                    -- 자유 서술/조합된 자연어
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- 시스템 하드캡 프로파일 (§18). specs 가 FK 로 참조하므로 먼저 만든다.
CREATE TABLE hardcap_profile (
    version               INT PRIMARY KEY,   -- 활성 버전 = MAX(version)
    max_loss_pct_cap      NUMERIC NOT NULL,  -- 1회 손실 한도 상한 (%) — 클램프 발동
    mdd_pct_cap           NUMERIC NOT NULL,  -- MDD 상한 (%) — 스텁
    min_rebalance_days    INT     NOT NULL,  -- 리밸런싱 최소 간격 (일) — 현행 값으론 미발동
    single_etf_weight_cap NUMERIC NOT NULL,  -- 단일종목 비중 상한 (%) — 스텁
    note                  TEXT,
    created_at            TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE specs (
    id          SERIAL PRIMARY KEY,
    request_id  INT REFERENCES requests(id),
    spec        JSONB NOT NULL,          -- StrategySpec JSON (**클램프 적용된 최종본**)
    version     INT NOT NULL DEFAULT 1,
    model       TEXT,                    -- 사용 모델 태그
    indicators  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- snapshot_date 기준 지표 스냅샷 (§17)
    clamps      JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 하드캡 조정 내역 (§18), 없으면 []
    hardcap_version INT REFERENCES hardcap_profile(version),  -- 적용된 정책 버전
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- 경제지표 피처 저장소 ------------------------------------------------
CREATE TABLE indicators (                -- 메타: 지표당 1행, 거의 안 변함
    code        TEXT PRIMARY KEY,        -- 'KR_CPI_YOY'
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL,           -- '%', 'KRW/USD'
    source      TEXT NOT NULL,           -- 'ECOS' | 'FRED'
    frequency   TEXT NOT NULL            -- 'daily' | 'monthly' | 'irregular'
);

CREATE TABLE indicator_observations (    -- 관측치: 발표마다 늘어남 + 개정됨
    id               SERIAL PRIMARY KEY,
    indicator_code   TEXT NOT NULL REFERENCES indicators(code),
    observation_date DATE NOT NULL,      -- 지표가 가리키는 시점 (2026년 7월 CPI)
    release_date     DATE NOT NULL,      -- 그 값이 공개된 시점 (2026-08-04)
    value            NUMERIC NOT NULL,
    note             TEXT,               -- '속보치' / '개정치'
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (indicator_code, observation_date, release_date)
);

CREATE INDEX idx_obs_asof
    ON indicator_observations (indicator_code, release_date, observation_date DESC);
```

**메타/관측치를 나눈 이유.** 메타는 지표당 1행이고 거의 안 변하는 반면 관측치는 발표마다
늘어난다. 한 테이블에 합치면 발표 때마다 지표명·단위·출처가 통째로 복제되고, 출처 표기
하나 고치는 데 전체 이력을 UPDATE해야 한다. FK 덕에 메타에 없는 코드의 관측치는 적재
단계에서 거부된다 — ETF 유니버스 화이트리스트(§7)와 같은 발상.

**observation_date / release_date 를 나눈 이유.** 하나로 합치면 둘 중 하나를 잃는다.
- `observation_date`만 남기면 "2026-08-01의 나"가 7월 CPI를 이미 아는 게 된다. 실제 공개는
  8월 4일 → **미래 정보 누출**. 백테스트 성과가 조용히 뻥튀기된다.
- `release_date`만 남기면 관측 시점을 잃어 "가장 최신 지표"의 의미가 사라지고, 개정값이
  원본을 밀어내 개정 전 시점을 재현할 수 없다.

**개정(revision) 처리.** 유니크 제약이 3컬럼(`code, observation_date, release_date`)이라
같은 관측월의 수정값이 **새 행으로 공존**한다. UPDATE로 덮어쓰면 개정 전에 보였던 값을
영영 복원할 수 없다. append-only 라서 감사 추적이 그대로 남는다.

**`clamps` 를 별도 테이블이 아니라 JSONB 컬럼으로 둔 이유.** (a) 항상 소속 Spec 과 함께
읽히고 — `clamps` 만 따로 조회할 상황이 없다, (b) 항목 수가 캡 개수로 묶여 있고(현재 최대 4),
(c) 한 번 쓰이면 불변이다. 테이블로 빼면 `/specs` 조회마다 JOIN 이 하나 늘 뿐 얻는 질의
자유도가 없다. `indicator_observations` 를 테이블로 뺀 것과는 상황이 정반대다 — 그쪽은 행이
독립적으로 계속 쌓이고 시점으로 조회되지만 `clamps` 는 둘 다 아니다. 바로 위 `indicators`
컬럼과 같은 판단이다.

**`hardcap_version` 만 별도 스칼라 컬럼인 이유.** `clamps` 만 있으면 나중에 해석이 안 된다
("20 으로 잘렸다"는 알겠는데 그때 상한이 20 이었는지 알 수 없다). 그리고 "정책 v1 로
만들어진 Spec 전부" 같은 질의가 실제로 필요한데, 그건 스칼라 컬럼이라야 인덱스가 먹는다.
`requests.survey`(요청값) → `specs.clamps`(조정 내역) → `hardcap_profile`(정책 원본)
셋을 이으면 조정 과정 전체가 복원된다.

- ORM은 취향껏 (SQLAlchemy 또는 psycopg 직접). 데모 규모라 psycopg + 간단 쿼리로 충분.
- 테이블 생성은 `db/schema.sql` (docker-entrypoint-initdb.d, **볼륨 최초 생성 시 1회**).
  스키마를 고쳤으면 `docker compose down -v` 로 볼륨을 지워야 반영된다.

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
│   ├── db.py                 # Postgres 연결/저장/조회 (requests, specs)
│   ├── indicators.py         # 경제지표 수집·적재·as-of 조회 (§17)
│   └── validators.py         # 2계층 유니버스/레버리지 + 4계층 하드캡 (§8, §18)
├── db/
│   └── schema.sql            # 테이블 DDL (컨테이너 최초 기동 시 1회 실행)
├── data/
│   ├── etf_universe.json
│   ├── economic_indicators.json   # 경제지표 seed (실제 통계 아님, §17)
│   └── hardcap_profile.json       # 하드캡 v1 seed + 값별 근거 (전부 잠정치, §18)
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

**as-of 시연 (§17)** — 같은 질문을 다른 시점에 던지면 다른 답이 나온다는 걸 보인다.
2026년 7월 CPI는 `2026-08-04` 공개, `2026-08-21` 개정(2.3 → 2.4)으로 seed 돼 있다.

| # | 명령 | 기대 결과 |
|---|---|---|
| 1 | `curl 'localhost:8000/indicators?as_of=2026-08-03'` | CPI = **2026-06** 관측치 (7월치 아직 미공개) |
| 2 | `curl 'localhost:8000/indicators?as_of=2026-08-04'` | CPI = **2026-07** 관측치 2.3 (속보치) |
| 3 | `curl 'localhost:8000/indicators?as_of=2026-08-20'` | CPI = 2026-07 / **2.3** (개정 전) |
| 4 | `curl 'localhost:8000/indicators?as_of=2026-08-21'` | CPI = 2026-07 / **2.4** (개정 후) |

1↔2가 **미래 정보 차단**, 3↔4가 **개정 이력 보존**의 증거다. 관측 시점(2026-07)은 3과 4가
같은데 값만 다르다 — 두 날짜를 한 컬럼으로 합쳤다면 둘 중 하나는 표현할 수 없다.

- 안전성: 지표 테이블을 통째로 비워도 `POST /compile` 은 200 (`indicators: {}`).
  지표는 Spec 생성의 의존성이 아니라 부가정보다.
- 자동 검증: `docker compose exec api python -m app.indicators` — 위 성질을 assert 로 확인.

**하드캡 시연 (§18)** — 이 시연의 핵심은 **"거부가 아니라 조정"** 이라는 점이다.
사용자는 원하는 걸 받고(200), 시스템은 상한을 지키고, **무엇이 어떻게 바뀌었는지가
사용자에게 그대로 보인다**. 조정 내역을 숨기면 값이 조용히 바뀐 것처럼 보여서
"거부됐다"와 구분이 안 된다. 브라우저에서는 결과 Spec 위에 주황 배너로 뜬다.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | 설문 그대로 (`max_loss` 3/5/10) | **200, `clamps: []`.** 정상 요청은 하드캡에 걸리지 않는다 |
| 2 | `max_loss: 25` (설문 밖 값, curl) | **200**, `spec.max_loss_pct = 20`, `clamps` 1건 |
| 3 | `max_loss: 50` / `100` | 위와 동일하게 전부 **20 으로 수렴** (차단율 측정 대상) |
| 4 | note 로 "동일 조건에 buy 와 sell 을 동시에" 유도 | **400** + `하드캡 구조적 위반: 동일 조건(...)에 buy 와 sell 이 동시에 지정됨` |

```bash
# ② 클램프 — 거부가 아니라 200 이라는 점이 핵심
curl -s -X POST localhost:8000/compile -H 'Content-Type: application/json' \
  -d '{"sector":"반도체","risk":"aggressive","max_loss":25,
       "style":"추세추종(모멘텀)","rebalance":"monthly","note":"손실 25%까지 감수"}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["spec"]["max_loss_pct"],d["clamps"])'
# → 20.0 [{'field': 'max_loss_pct', 'requested': 25.0, 'applied': 20.0, ...}]
```

**정책 버전 전환 시연** — 캡 값이 코드가 아니라 DB 에서 온다는 증거.
새 버전을 INSERT 하면 **컨테이너 재시작 없이** 다음 요청부터 적용된다.

```bash
docker compose exec db psql -U demo -d demo -c \
  "INSERT INTO hardcap_profile VALUES (2, 15, 30, 7, 40, '상한 15로 조정');"
curl -s localhost:8000/health | python3 -c 'import sys,json;print(json.load(sys.stdin)["hardcap"])'
# → ok (v2 / max_loss<=15% ...)   ← 재시작 안 했는데 바뀐다
# 같은 요청을 다시 던지면 이번엔 15 로 클램프된다.
```
이전 버전은 **UPDATE 로 덮이지 않고 그대로 남는다.** 되돌릴 때도 DELETE 가 아니라
원래 값으로 새 버전을 INSERT 한다 — 그래야 과거 Spec 의 `hardcap_version` 이 가리키는
정책이 계속 복원 가능하다.

- 자동 검증: `docker compose exec api python -m app.validators`
  — 클램프·반려·스텁 판정 불가·프로파일 교체를 전부 assert 로 확인 (DB 없이 순수 함수로).

## 16. 확인 필요 사항 (Open Decisions)

1. ~~**하드웨어**~~ → **확정**: MacBook M4 16GB 통합메모리, 모델 `qwen3:8b`.
2. **signal 구조화 수준**: 구조화(`SignalRule` 리스트) 기본 vs 문자열 폴백 — 현재 구조화 기본.
3. **ETF 유니버스 규모**: 데모용 9종 스타터로 충분한지, 섹터별 확장 필요한지.
4. **Ollama 배치**: 호스트 실행(권장) vs 도커 내부(GPU 패스스루 필요).
5. ~~**경제지표 소비 범위**~~ → **확정**: 조회 계층만 구현(§17). 임계값 기반 시장온도
   판정은 (a) 임계값에 근거가 없어 검증 불가, (b) 프롬프트에 국면 라벨이 들어가면
   `temperature=0` 이어도 as_of 에 따라 Spec 출력이 흔들려 §15 재현성 시나리오를
   재검증해야 하고, (c) 판단 계층 소속이라 이번 범위 밖 — 세 이유로 제외.
6. **하드캡 값 (§18)** — **전부 팀 잠정 결정이며 개발 중 변경 가능.** 확정 절차 필요:
   - `max_loss_pct_cap = 20` — 설문 최대 선택지(10)의 2배라는 것 외의 근거 없음
   - `mdd_pct_cap = 30` — "1회 손실 한도 < MDD 상한" 관계만 근거. 절대값은 **P3 에서
     유니버스 실측 MDD 확인 후 확정**
   - `single_etf_weight_cap = 40` — 적용 자체가 불가(비중 필드 부재). **P3 배분 계층에서
     스키마 확장과 함께 재검토**
7. **3계층(논리)의 소속** — 현재 논리 모순 판정이 4계층 진입부에 붙어 있다.
   판정 항목이 늘어나면 별도 계층으로 분리할지 결정 필요.

---

## 17. 경제지표 피처 저장소 — as-of 조회

상위 기획서 P1 "피처 저장소" 마일스톤의 선행 PoC. **목적은 프롬프트 컨텍스트 보강이 아니라
시점 정합적 데이터 접근이 동작함을 입증하는 것**이다. 그래서 지표는 조회·기록만 되고
LLM 프롬프트에는 들어가지 않는다.

### 17.1 두 날짜 규약 (핵심)

모든 관측치는 날짜를 **두 개** 갖는다. 이걸 하나로 합치는 순간 이 계층의 존재 이유가 사라진다.

| 컬럼 | 뜻 | 예 |
|---|---|---|
| `observation_date` | 지표가 **가리키는** 시점 | 2026년 7월 CPI → `2026-07-01` |
| `release_date` | 그 값이 **공개된** 시점 | `2026-08-04` |

**as-of 조회 규칙** — `as_of=T` 는 다음 순서로 지표당 한 행을 고른다.

1. `release_date <= T` 인 행만 후보로 남긴다. → T 시점에 아직 세상에 없던 값은 애초에 제외
2. 그중 `observation_date` 가 **가장 최근**인 것. → "그때 기준 최신 지표"
3. 2번이 동률이면(= 같은 관측월에 개정본이 여럿) `release_date` 가 가장 늦은 것.
   → "그 시점 기준 최신 개정본"

```sql
SELECT DISTINCT ON (o.indicator_code) ...
  FROM indicator_observations o JOIN indicators i ON i.code = o.indicator_code
 WHERE o.release_date <= :as_of                 -- ① 미래 정보 차단
 ORDER BY o.indicator_code,
          o.observation_date DESC,              -- ② 최신 관측
          o.release_date DESC;                  -- ③ 최신 개정본
```

개정 전 시점으로 물으면 개정 행이 ①에서 이미 빠지므로 **자동으로 원본 값**이 나온다.
개정을 UPDATE 로 덮어쓰지 않고 새 행으로 쌓기 때문에 가능한 일이다 (§10).

### 17.2 파이프라인 연결

`POST /compile` 은 생성된 `StrategySpec.snapshot_date` 를 **그대로 as-of 키로** 써서 지표를
뜨고, 응답에 동봉하면서 `specs.indicators` 에 저장한다. `version` / `model` 을 박제하는 것과
같은 취지 — "이 Spec 이 어떤 데이터 세계에서 만들어졌는가"를 나중에 재현·감사할 수 있어야 한다.

**프롬프트에는 넣지 않는다.** 국면 라벨 같은 게 프롬프트에 들어가면 `temperature=0` 이어도
as_of 에 따라 Spec 출력이 달라져 §15 의 재현성·레버리지 차단 시나리오를 전부 재검증해야 하고,
로컬 8B 모델은 프롬프트가 길어질수록 품질이 떨어진다. 지표 소비(시장온도 판정 등)는
판단 계층 소속이라 이번 범위 밖이다 (§2, §16-5).

**지표는 의존성이 아니라 부가정보다.** 조회가 어떤 이유로 실패하든(테이블 없음/빈 테이블/DB
장애) `_as_of_snapshot()` 이 `{}` 를 돌려주고 `/compile` 은 200 을 유지한다.

### 17.3 데이터 출처와 교체 지점

현재 데이터는 `data/economic_indicators.json` **정적 seed** 다. 지표 5종:

| code | 지표 | 출처 | 발표 지연 |
|---|---|---|---|
| `KR_BASE_RATE` | 한국은행 기준금리 | ECOS | 없음 (금통위 당일) |
| `USD_KRW` | 원/달러 매매기준율 | ECOS | 없음 (당일 고시) |
| `KTB_3Y` | 국고채 3년물 수익률 | ECOS | +1영업일 |
| `KR_CPI_YOY` | 소비자물가 전년동월비 | ECOS / 원자료 통계청 | **+약 1개월** ← as-of 시연용 |
| `US_FED_FUNDS` | 미국 연방기금금리 상단 | FRED | 없음 (FOMC 당일) |

> ⚠️ **seed 값은 실제 통계가 아니다.** 실제 최근값에 가깝게 채운 근사치이며 정확성을
> 보장하지 않는다. 투자 판단·백테스트에 쓰면 안 된다. `source` 표기는 "어디서 오게 될
> 것인가"를 뜻하며, 지금 값들이 해당 API 에서 받아온 것이라는 의미가 아니다.

**실제 API 연동 시 교체 지점은 `app/indicators.py` 의 `fetch_indicator_data()` 하나뿐이다.**

```python
def fetch_indicator_data() -> tuple[list[dict], list[dict]]:
    """<<교체 지점>> (메타 리스트, 관측치 리스트) 를 돌려준다.
    지금은 seed JSON 을 읽는다. 나중에 ECOS / fredapi 어댑터로 갈아끼운다."""
```

반환 형태만 지키면 아래(적재 `seed_indicators` / 조회 `get_indicators_as_of`)는 데이터가
어디서 왔는지 모르므로 손댈 필요가 없다. 네트워크·인증키·스케줄러는 전부 이 함수 안쪽
사정이고, **이번 데모에는 그중 어느 것도 들어 있지 않다.** 어댑터를 붙일 때 주의할 것:
ECOS/FRED 응답에서 `release_date` 를 어떻게 얻느냐가 관건이다. 대부분의 API 는 관측 시점만
주므로, FRED 는 `realtime_start`(ALFRED 계열), ECOS 는 공표 일정표를 별도로 매핑해야 한다.
그 매핑을 생략하고 `release_date = observation_date` 로 채우면 이 계층 전체가 무의미해진다.

### 17.4 적재와 멱등성

`app/main.py` 의 lifespan 이 기동 시 `seed_indicators()` 를 1회 호출한다. 메타는
`ON CONFLICT DO UPDATE`, 관측치는 3컬럼 유니크에 `ON CONFLICT DO NOTHING` 이라 `--reload` 로
몇 번을 다시 떠도 중복이 쌓이지 않는다. 적재 실패는 삼킨다 — 지표가 없어도 `/compile` 은
돌아가야 하므로 서버 기동 자체를 막을 이유가 없다. 상태는 `/health` 의 `indicators` 에 뜬다
(`ok (5종 / 관측치 16건)` / `empty: 지표 미적재` / `error: ...`).

---

## 18. 시스템 하드캡 — Validator 4계층

기획서 4.1 Validator 4계층의 **4번째 계층**. 원칙 한 줄:

> **수치는 클램프, 구조적 위반은 반려.**

### 18.1 하드캡 값과 근거

> ⚠️ **아래 값은 전부 팀 잠정 결정(2026-08-28)이며 개발 중 변경 가능하다.**
> 코드에 상수로 박혀 있지 않고 `hardcap_profile` 테이블 → `data/hardcap_profile.json` seed
> 에만 있다. 값을 바꾸려면 새 버전을 INSERT 한다 (§18.3).

| 항목 | 초기값 | 근거 | 확정 여부 | 데모 동작 |
|---|---|---|---|---|
| **1회 손실 한도** `max_loss_pct_cap` | **20%** | 설문 선택지 최대값(10%)의 **2배**. 정상 요청(3/5/10)은 안 걸리고 적대적 입력(30·50·100)만 걸리게 해서 "하드캡 차단율" 지표가 정상 요청에 희석되지 않게 한다. **하한은 두지 않는다** — 낮게 잡는 건 보수적 선택이라 막을 이유가 없다 | **잠정** | ✅ **실제 발동 (클램프)** |
| **MDD 상한** `mdd_pct_cap` | **30%** | `1회 손실 한도(20) < MDD 상한(30)` 관계 유지, 그것 하나가 유일한 근거. 절대값은 **P3 에서 유니버스 실측 MDD 확인 후 확정** | **잠정 (절대값 근거 없음)** | ⛔ 스텁 — 백테스트 계층이 없어 **판정 불가** |
| **최소 리밸런싱 간격** `min_rebalance_days` | **7일** | 현행 `RebalanceFreq` 최소 단위 `weekly`(7일)와 **일부러 같게** 잡았다. 더 크게 잡으면 설문의 '주 1회' 선택지가 **항상** 클램프돼 정상 응답이 매번 조정된다 | **잠정** | ⛔ 미발동 — 로직은 살아 있고, enum 에 더 짧은 주기가 추가되거나 캡을 8↑ 로 올리면 즉시 발동 |
| **단일종목 상한** `single_etf_weight_cap` | **40%** | 적용 자체가 불가능해 값에 실질적 근거가 없다 | **잠정 (적용 불가)** | ⛔ 스텁 — **`StrategySpec` 에 종목별 비중 필드가 없어 판정 불가** |

**단일종목 상한의 미적용 사유를 정확히 해 둔다.** 사유는 **"종목별 비중 필드 부재"** 하나다.
"ETF 라서 이미 분산돼 있으니 불필요"가 **아니다.** ETF 내부 분산과 포트폴리오 내 ETF 비중은
별개 문제다 — `KODEX 반도체` 하나에 100% 를 배분하면 그 ETF 가 내부적으로 수십 종목에
분산돼 있어도 포트폴리오는 반도체 섹터에 100% 노출된다. 이 캡이 막으려는 섹터 집중 위험은
실재하며, 지금 스키마로 표현할 수 없을 뿐이다. **P3 배분 계층에서 스키마 확장과 함께 재검토.**

**스텁은 조용히 통과시키지 않는다.** 세 항목 모두 호출하면 `status: "undecidable"` 과
사유를 돌려준다. `ok`(검사했고 통과)를 돌려주면 캡이 작동 중인 것처럼 보이는데, 그게
값이 없는 것보다 위험하다. `/health` 의 `hardcap` 과 `hardcap_report()` 에서 확인할 수 있다.

### 18.2 하드캡 값을 LLM 프롬프트에 넣지 않는 이유

**`app/prompt.py` 는 하드캡을 전혀 모른다. 앞으로도 그래야 한다.**

LLM 은 상한의 존재도 값도 모르는 채로 Spec 을 생성하고, 서버가 **사후에** 깎는다.
값을 알려주면 모델이 경계에 맞춰(19.9 같은 값으로) 생성하기 시작해서 클램프 이벤트가
아예 발생하지 않고, 향후 측정할 **"적대적 입력에 대한 하드캡 차단율"이 0 으로 수렴해
지표가 무의미**해진다. 차단율 측정은 LLM 이 하드캡을 모른다는 전제 위에 서 있다.

여기서 파생되는 구조적 제약이 하나 있다:

> **하드캡은 `llm.py` 의 재시도 루프 밖에서 적용해야 한다.**
> `llm.py` 는 검증 실패 사유를 `build_user(retry_reason=...)` 로 **프롬프트에 덧붙여**
> 재시도한다. 하드캡 위반을 그 경로로 흘리면 `"max_loss_pct 상한 20 초과"` 같은 문자열이
> 그대로 모델에게 전달된다 — 우회로로 값이 새는 것이다.
> 그래서 `enforce_hardcaps()` 호출은 `compile_spec()` **밖**, `main.py` 에 있다.

### 18.3 `hardcap_profile` — 값은 코드가 아니라 DB 에서

**왜 코드 상수가 아닌가.** 하드캡은 운영 중 조정되는 정책값이다. 코드에 박으면 값을 바꿀
때마다 배포가 필요하고, "언제 무슨 값이었는지"가 git log 에만 남아 Spec 행과 대조가 안 된다.

**왜 UPDATE 가 아니라 새 버전 INSERT 인가.** 덮어쓰면 과거 Spec 에 적용됐던 캡을 영영
복원할 수 없다. `indicator_observations` 가 개정을 새 행으로 쌓는 것과 같은 발상(§17).

**활성 버전 = `MAX(version)`.** 검토한 대안과 탈락 이유:

| 방식 | 판단 |
|---|---|
| `is_active BOOLEAN` | ❌ 새 버전을 켜려면 **이전 행을 UPDATE** 해서 꺼야 한다 — 이 테이블이 금지하는 바로 그 변경이다. 게다가 활성 행이 0개나 2개인 불법 상태를 스키마가 못 막는다 |
| `effective_from` as-of 조회 (§17 방식) | ❌ 예약 적용은 가능해지지만, 하드캡은 *관측된 데이터*가 아니라 *요청 시점에 적용되는 정책*이라 발표 지연 개념이 없다. 적용된 정책은 `specs.hardcap_version` 에 이미 박제되므로 사후 감사에 as-of 조회가 불필요 |
| **`MAX(version)`** | ✅ **채택.** 활성 여부가 데이터에서 파생돼 따로 관리할 상태가 없고 불일치가 원천적으로 불가능하다. `INSERT ... version = 2` 한 줄이 곧 전환 |

**재시작 없이 반영되는 이유.** `llm.py` 의 `_UNIVERSE` 와 달리 모듈 로드 시 캐시하지 않고
**요청마다 조회**한다. 30~60초짜리 LLM 호출 옆에서 단일 행 SELECT 하나는 무시할 수 있다.

**적재.** lifespan 이 `seed_hardcap_profile()` 을 1회 호출한다. 지표 메타(`DO UPDATE`)와 달리
**`ON CONFLICT (version) DO NOTHING`** 이다 — 이미 적재된 버전을 seed 파일 수정으로 바꿀 수
있으면 그 버전으로 만들어진 과거 Spec 의 근거가 조용히 달라진다. 값을 바꾸려면 seed 에
새 `version` 을 추가해야 한다 (= 추가만 가능한 원장).

### 18.4 계층 구현 (`app/validators.py`)

**이 파일에는 캡 값이 하나도 없다.** 전부 `profile` 인자로 들어오고, 파일은 DB 도
import 하지 않는다. 그래서 (a) "값을 코드에 하드코딩하지 않는다"가 구조로 보장되고
(어떤 함수도 인자 없이는 캡 값을 알 수 없다), (b) 셀프체크가 DB 없이 순수 함수로 돈다.

```python
def enforce_hardcaps(spec: dict, profile: dict) -> tuple[dict, list[dict]]:
    problems = find_logical_contradictions(spec)   # ① 구조적 위반이면 먼저 반려
    if problems:
        raise ValueError(" / ".join(problems))     #    → main.py 가 400 으로 변환

    clamped, clamps = dict(spec), []               # ② 수치는 깎아서 살린다
    for check in (check_max_loss_pct, check_min_rebalance_interval):
        verdict = check(clamped, profile)
        if verdict["status"] == "clamped":
            clamped[verdict["field"]] = verdict["applied"]
            clamps.append({...})                   #    조정 내역을 남긴다 (200 으로 응답)
    return clamped, clamps
```

판정 상태는 셋이고, **"조용히 통과"가 없다**:
`ok`(검사 후 위반 없음) / `clamped`(조정함) / `undecidable`(**판정 불가 — 통과가 아니다**).

**구조적 위반 (반려) 판정 항목**

| 검사 | 발동 |
|---|---|
| `signals` 가 비어 있음 — 매매 조건 없는 Spec 은 실행 불가 | ✅ |
| 동일 조건(`indicator`·`operator`·`threshold` 셋 다 같음)에 buy 와 sell 동시 지정 | ✅ 데모에서 실제 발동 |
| 안정형(`conservative`)인데 레버리지 종목 지정 | ⛔ **현행 파이프라인으로는 도달 불가** |

> 마지막 항목의 한계 — 이유가 둘 겹쳐 있다: (a) `data/etf_universe.json` 에 레버리지/인버스
> 종목이 아예 없고, (b) 설령 있어도 **2계층 `validate_etfs()` 가 하드캡보다 먼저 돌아 반려**한다.
> 그래도 남겨 둔 이유: 유니버스가 KRX 전체로 확장되고 레버리지가 '성향 무관 허용'으로
> 바뀌는 순간 이 검사만이 "안정형인데 레버리지"를 잡는다. 코드 주석에도 같은 내용이 있다.

**같은 `indicator` 에 buy/sell 이 함께 있는 것 자체는 모순이 아니다** —
`momentum_20d > 0 → buy` / `momentum_20d < 0 → sell` 은 평범한 추세추종 전략이다.
모순은 `indicator`·`operator`·`threshold` 가 **셋 다 같은데 action 만 반대**일 때, 즉
조건이 참인 순간 buy 와 sell 이 동시에 성립할 때다. 이 구분을 놓치면 정상 전략이
전부 반려된다 (셀프체크에 두 경우가 모두 들어 있다).

### 18.5 실패 처리 — fail closed

하드캡 프로파일을 못 읽으면 `/compile` 은 **503** 이다. 지표(§17)가 실패해도 `{}` 로
넘어가 200 을 내는 것과 **의도적으로 반대**다:

| 계층 | 못 읽었을 때 | 왜 |
|---|---|---|
| 경제지표 (§17) | `{}` 로 넘어가고 **200** | 부가정보다. Spec 생성의 의존성이 아니다 |
| 하드캡 (§18) | **503** (fail closed) | 안전 계층이다. 조용히 사라진 채로 Spec 을 내보내는 게 에러보다 나쁘다 |

`/health` 의 `hardcap` 에 활성 버전과 캡 값이 그대로 찍힌다:
`ok (v1 / max_loss<=20% mdd<=30% rebalance>=7d single_etf<=40%)`.
지표와 달리 `empty` 라는 정상 상태가 없다 — 프로파일이 없으면 그건 `error` 다.

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