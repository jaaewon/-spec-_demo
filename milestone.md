# milestone.md — 자연어 → Strategy Spec 변환 데모

타임박스 **2시간**, 5개 마일스톤(M1~M5). 각 단계는 앞 단계 완료 기준(DoD)을 만족해야
다음으로 넘어간다. M6~M8 은 그 뒤에 추가된 것이라 타임박스 밖이다.

---

## M1. 스캐폴딩 & 기동 확인 (20m)

**할 일**
- `docker-compose.yml`, `Dockerfile`, `requirements.txt` (fastapi, uvicorn, pydantic, ollama, psycopg[binary])
- Postgres `requests` / `specs` 테이블 생성 (CLAUDE.md §10)
- 호스트에서 `ollama serve` + `ollama pull qwen3:8b` (M4 16GB 확정 태그, ~5.2GB)
- `app/main.py`: `GET /health` — Ollama·DB 양쪽 핑

**완료 기준**
- `docker compose up` 후 `curl localhost:8000/health` → `{"db": "ok", "ollama": "ok"}`

**막히면**: Ollama는 호스트 실행 고정(Mac은 도커에서 Metal 못 씀). 메모리 빠듯하면 Docker Desktop 리소스를 4GB로 제한.

---

## M2. 스키마 & 유니버스 확정 (20m)

**할 일**
- `app/schemas.py`: `StrategySpec`, `SignalRule`, `RebalanceFreq`, `RiskProfile` (§4 그대로)
- `data/etf_universe.csv`: 비레버리지 KRX ETF 스타터 (§7, 현재 20종, `name,code,theme`).
  종목코드는 KRX에서 검증 후 확정 — **아직 미검증** (CLAUDE.md §16-11)
- `app/validators.py`: 유니버스 밖 / 레버리지 키워드 차단 (§8)

**완료 기준**
- `StrategySpec.model_json_schema()` 가 에러 없이 출력된다
- validator 자체 체크: 정상 종목 통과 / `"KODEX 레버리지"` → `ValueError`

**결정 필요**: signal 구조화 수준 — 기본은 `list[SignalRule]`. M3에서 모델이 계속 깨지면 `signal: str` 폴백.

---

## M3. LLM 파이프라인 (30m) — **핵심 마일스톤**

**할 일**
- `app/prompt.py`: 시스템 프롬프트 + 유니버스 종목명 주입 (§6)
- `app/llm.py`: `ollama.chat(model="qwen3:8b", format=StrategySpec.model_json_schema(), think=False, options={"temperature": 0})` → `model_validate_json`
- 검증 실패 시 **1회만** 재시도 (사유를 프롬프트에 덧붙임). 무한 루프 금지

**완료 기준**
- 스크립트로 설문 dict 하나 넣으면 스키마를 만족하는 Spec JSON이 나온다 (성공 기준 1)
- 같은 입력 2회 → 둘 다 유효 (temperature=0 재현성)

**막히면**: `think=False` 확인 후 `exaone3.5:7.8b`(로컬 보유, 한국어 특화)로 폴백. 30분 넘기면 signal을 문자열로 축소.

---

## M4. `/compile` 엔드포인트 + 저장 (20m)

**할 일**
- `app/db.py`: `requests`(설문 원본 + 자연어) / `specs`(Spec JSON + 모델 태그) insert, `/specs` 조회
- `POST /compile`: 설문 → 프롬프트 → LLM → validator → 저장 → `{request_id, spec}` 반환
- 검증 실패 → 400 + 사유

**완료 기준**
- `curl -X POST /compile` (§9 요청 예시) → Spec 반환, `GET /specs`에 원문+Spec 같이 조회됨 (성공 기준 3)

---

## M5. 설문 UI + 시연 검증 (30m)

**할 일**
- `static/index.html`: 설문 폼 6항목 (§5) + `fetch('/compile')` + 결과 `<pre>` JSON 렌더
- `GET /` 로 서빙

**완료 기준** — §15 시나리오 4개 전부 통과
1. 정상: 반도체 + 공격형 + 모멘텀 + 월1회 → 해당 Spec 생성
2. 경계: 자유서술 "레버리지 반도체 담아줘" → 400 거부 (성공 기준 2)
3. 경계: 유니버스 밖 종목 유도 → 거부 또는 유니버스 내 대체
4. 무결성: 동일 설문 2회 → 항상 유효 Spec

---

## M6. Spec 백테스트 · 리포트 (M1~M5 이후 추가)

**⚠️ 범위 승인 미확인.** CLAUDE.md §2 는 여전히 "백테스팅/리포트 생성"을 제외로 적고
있는데 구현은 들어와 있다. 편입 여부와 그에 따른 결정(MDD 하드캡 활성화 등)은
CLAUDE.md §16-10 의 미결 항목이다. 아래는 **이미 구현된 것을 기록한 것**이다.

**한 일**
- `app/tickers.py`: 종목명 → KRX 티커. 매핑은 `data/etf_universe.csv` 의 `code`
  컬럼에서 유도한다 (하드코딩 표를 두지 않는다 — 두 벌이 되면 어긋난다).
  시세는 `get_market_ohlcv_by_date` 로 받는다 (ETF 전용 API 가 빈 값을 준다)
- `app/backtest.py`: Spec → vectorbt 인자 변환, 텍스트 리포트, plotly figure.
  Spec 에 없어 백테스트가 정한 해석은 파일 상단 docstring 에 적혀 있다
  (신호 OR 결합 · 리밸런싱을 매매 가능일 제한으로 · `max_loss_pct` 를 손절선으로 ·
   종목 균등 배분). **이 해석들도 승인 대상이다**
- `app/main.py`: `POST /backtest/{spec_id}`, `GET /plotly.js`,
  `/compile` 응답에 `spec_id` 추가
- `static/index.html`: Spec 아래에 차트와 리포트

**완료 기준**
- `python -m app.tickers` — 매핑이 유니버스 20종과 일치, 시세 로드 확인
- `python -m app.backtest` — 신호 조립(네트워크 없이) + 실제 백테스트
- 브라우저에서 설문 제출 → Spec → 차트·리포트까지 자동으로 이어짐

**주의 (2026-08-29 사후 감사에서 확인)**
- `/compile` 응답의 `spec_id` 는 프론트가 의존한다. 이 키를 빼면 화면 아래 절반이
  통째로 깨진다. 실제로 머지 충돌 해결에서 `main.py` 한쪽을 통째로 채택하면서
  이 키와 엔드포인트 2개가 사라진 적이 있다 (`backtest.py` 는 남아 죽은 코드가 됐다)
- 유니버스를 바꾸면 티커 매핑이 자동으로 따라오지만, `csv` 의 `code` 가 그 `name` 의
  실제 종목코드인지는 레포 안에서 검증할 수 없다 (§16-11)

---

## M7. 경제지표 피처 저장소 — as-of 조회 (M1~M5 이후 추가)

상위 기획서 P1 "피처 저장소" 마일스톤의 선행 PoC. **입증 대상은 프롬프트 보강이 아니라
시점 정합적 데이터 접근(as-of)** 이다. 자세한 규약은 CLAUDE.md §17.

**할 일**
- `db/schema.sql`: `indicators`(메타) / `indicator_observations`(관측치) 분리.
  유니크 `(indicator_code, observation_date, release_date)` — 개정을 새 행으로 쌓기 위함.
  `specs.indicators JSONB` 컬럼 추가 (as-of 스냅샷 박제)
- `data/economic_indicators.json`: 지표 5종 seed (기준금리·환율·국고채3년·CPI·FFR).
  **실제 통계 아님**, 출처 표기 필수. CPI 는 발표 지연(+약 1개월) 사례로 반드시 포함
- `app/indicators.py`: `fetch_indicator_data()`(← **교체 지점**, 지금은 seed JSON) /
  `seed_indicators()`(멱등) / `get_indicators_as_of(as_of, codes)` / `indicators_status()`
- `app/main.py`: lifespan 에서 seed 1회 적재, `GET /indicators?as_of=`,
  `/health` 에 지표 상태, `/compile` 이 `spec.snapshot_date` 로 조회해 응답·저장

**완료 기준**
- `docker compose down -v && up` → 테이블 생성 + seed 자동 적재, `/health` 에 `ok (5종 / 관측치 16건)`
- `as_of=2026-08-03` → 2026-07 CPI 안 나옴 / `as_of=2026-08-04` → 나옴 (미래 정보 차단)
- `as_of=2026-08-20` → 2.3 / `as_of=2026-08-21` → 2.4, 관측월은 둘 다 2026-07 (개정 이력 보존)
- 지표 테이블을 통째로 비워도 `POST /compile` 이 200 (`indicators: {}`)
- `docker compose exec api python -m app.indicators` — 위 성질 assert 통과

**범위 밖으로 남긴 것**: 실제 ECOS/FRED 호출, 프롬프트 주입, 임계값 기반 시장온도 판정
(판단 계층 소속). 판정 임계값은 근거가 없어 검증 불가이고, 국면 라벨이 프롬프트에 들어가면
`temperature=0` 이어도 as_of 에 따라 Spec 이 흔들려 M5 시나리오를 재검증해야 한다.

---

## M8. 시스템 하드캡 — Validator 4계층 (M7 이후 추가)

기획서 4.1 Validator 4계층 중 **4번째 계층**. CLAUDE.md §2 에서 "제외"였던 항목을
구현 범위로 승격. 규약은 CLAUDE.md §18.

원칙 한 줄: **수치는 클램프(200), 구조적 위반은 반려(400).** 전부 400 으로 막지 않는다.

**할 일**
- `db/schema.sql`: `hardcap_profile` 테이블(버전 컬럼, **활성 버전 = MAX(version)**).
  `specs` 에 `clamps JSONB` / `hardcap_version INT` 2컬럼 추가
- `data/hardcap_profile.json`: v1 seed 4항목 + 값별 근거. **전부 팀 잠정치**
- `app/db.py`: `seed_hardcap_profile()`(멱등, `ON CONFLICT DO NOTHING`) /
  `load_active_hardcap_profile()`(**요청마다 조회 — 캐시 금지**) / `hardcap_status()`
- `app/validators.py`: 기존 참조 계층 **무수정**, 4계층 섹션 추가.
  `enforce_hardcaps()` / `find_logical_contradictions()` / 항목별 체크 4종.
  **캡 값은 인자로만 받는다** (이 파일은 DB 를 import 하지 않는다)
- `app/main.py`: lifespan seed, `/compile` 에 4계층 적용 + `clamps` 응답,
  `/health` 에 하드캡 상태
- `static/index.html`: 조정 내역 배너 (거부와 시각적으로 구분)

**하지 말 것 (제약)**
- `app/prompt.py` 수정 금지 — 하드캡 값이 프롬프트에 들어가면 모델이 경계에 맞춰
  생성해 클램프가 안 일어나고, "적대적 입력 차단율" 지표가 무의미해진다 (§18.2)
- 같은 이유로 `enforce_hardcaps()` 를 `llm.py` 재시도 루프 **안**에 넣지 말 것 —
  재시도는 실패 사유를 프롬프트에 덧붙이므로 그 경로로 값이 샌다
- `StrategySpec` 기존 필드 변경 금지 / 새 라이브러리 추가 금지

**완료 기준**
- `python -m app.validators` — 클램프·반려·스텁 판정 불가·프로파일 교체 assert 통과
- `max_loss: 25` → **200**, `spec.max_loss_pct = 20`, `clamps` 에 조정 내역
  (필드명·요청값·조정값·사유)
- 구조적 위반(동일 조건 buy/sell 동시) → **400** + 사유
- `hardcap_profile` 에 v2 INSERT → **컨테이너 재시작 없이** 다음 요청부터 새 값 적용
- 설문 선택지(3/5/10)는 전부 `clamps: []` — 정상 요청은 하드캡에 안 걸린다
- 스텁 3종은 값은 있되 호출 시 `undecidable` + 사유 반환 (조용히 통과 금지)
- 회귀: `GET /indicators` `/specs` `/health` 기존과 동일

**범위 밖으로 남긴 것 (스텁 3종)** — 사유는 CLAUDE.md §2 표와 §18.1 참고.
MDD(**사유 갱신**: 백테스트 계층은 M6 으로 들어왔다. 캡 절대값 근거와 범위 편입이
미확정이라 스텁 유지 — CLAUDE.md §16-10) · 최소 리밸런싱 간격(현행 enum 최소 단위가 캡과 동일해 미발동) ·
단일종목 상한(**종목별 비중 필드 부재** — ETF 라서 불필요한 게 아니다). 뒤 둘은 P3 재검토.

---

## 범위 밖 (이번 데모에서 안 함)

RAG · 승인 게이트 · 다중턴 되묻기. — CLAUDE.md §2 제외 항목 그대로.
- 백테스팅은 M6 으로 **구현은 들어왔으나 범위 편입 승인은 미확인** (CLAUDE.md §16-10).
- 경제지표 DB 는 M7 로 편입 — 단 **조회 계층까지만**, 소비/실 API 연동은 여전히 범위 밖.
- 하드캡은 M8 로 편입 — 단 **실제 발동은 `max_loss_pct` 클램프와 논리 모순 반려까지**.
  MDD·최소간격·단일종목은 값만 저장하는 스텁이다.
