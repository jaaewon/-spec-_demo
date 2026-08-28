# milestone.md — 자연어 → Strategy Spec 변환 데모

타임박스 **2시간**, 5개 마일스톤. 각 단계는 앞 단계 완료 기준(DoD)을 만족해야 다음으로 넘어간다.

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
- `data/etf_universe.json`: 비레버리지 KRX ETF 9종 스타터 (§7). 종목코드는 KRX에서 검증 후 확정
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

## 범위 밖 (이번 데모에서 안 함)

하드캡 조건문 · RAG · 백테스팅 · 승인 게이트 · 다중턴 되묻기. — CLAUDE.md §2 제외 항목 그대로.
(경제지표 DB 는 M7 로 범위에 편입됨 — 단, **조회 계층까지만**이고 소비/실 API 연동은 여전히 범위 밖)
