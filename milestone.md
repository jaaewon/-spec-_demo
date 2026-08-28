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
MDD(백테스트 계층 부재) · 최소 리밸런싱 간격(현행 enum 최소 단위가 캡과 동일해 미발동) ·
단일종목 상한(**종목별 비중 필드 부재** — ETF 라서 불필요한 게 아니다). 뒤 둘은 P3 재검토.

---

---

## M9. 자유 입력 → Spec 컴파일 경로 (M8 이후 추가)

상위 기획서 U-1 "자연어 전략 요청"의 API 계층. CLAUDE.md §2 에서 설문만 있던 입력
경로에 자연어 한 단락을 추가한다. 규약은 CLAUDE.md §19. **UI 는 이번 범위 밖.**

**설계 판단 — 왜 설문(SurveyRequest)을 경유하지 않는가 (경로 2)**
- 표현력: `SurveyRequest.sector` 가 단수 Enum 이라 "반도체 + 배당" 같은 복합 의도를
  경유시키면 하나를 버려야 한다. 상위 기획서가 기존 로보어드바이저의 **첫 번째 한계로
  지목한 '의도 표현력의 병목'** 을, 그걸 해소하려는 기능의 입력단에 다시 세우게 된다.
- **결정적 사유**: 경로 1 의 슬롯 추출도 구조화 출력이어야 하는데,
  `format=SurveyRequest.model_json_schema()` 는 "밈주식"에 대해 enum 7종 중 하나를
  **반드시** 고르게 만든다 — **거부할 수단이 스키마에 없고 조용히 치환된다.**
  요구사항이 금지하는 게 정확히 그것이라, 경로 1 은 요구사항을 스키마 수준에서 위반한다.
- 검증 경로 "단일화"는 경로 1 의 이점이 아니다. 검증은 전부 StrategySpec 에 걸리므로
  경유 여부와 무관하게 같은 함수를 통과한다. 경유는 검증면을 하나 **늘릴** 뿐이다.

**할 일**
- `data/intent_lexicon.json`: 사전 스캔 어휘 + **항목별 근거(reason 필수)**.
  섹터 positive 는 여기 없다(etf_universe.json 의 theme 에서 파생, 키 정합성 로드 시 검증).
  레버리지 키워드도 없다(validators.LEVERAGE_KEYWORDS import). 텍스트 전용 추가분만
  별도 키에 두고 **2계층으로 역류시키지 않는다**
- `app/intent.py`: `scan_free_text()` → `{slots, rejections, notices}` /
  `classify_intent()`(요구 vs 언급) / `describe_slots()`(값별 출처 + 대조). **LLM 아님**
- `describe_slots()` 는 **두 질문에 두 필드로** 답한다 (CLAUDE.md §19.3.1):
  `source`(언급했는가) / `check`(매치 표현이 최종 값과 맞는가). `source` 에
  `explicit_conflict` 같은 값을 **추가하지 않는다** — 그러면 "언급했는가"의 답이
  대조 결과에 오염된다. `evidence` 는 `matched_term` 으로 **개명**한다(값은 남긴다 —
  지우면 스캔이 무엇에 걸렸는지가 사라져 감사가 안 된다). `implies` 는 `check` 계산의 입력
- `app/schemas.py`: `FreeInputRequest` 추가 (`text`, 1~2000자). 기존 모델 무수정
- `app/prompt.py`: `build_free_system()` / `build_free_user()` / `sanitize_free_text()`.
  격리 규칙은 **자유 입력에만** 덧붙인다 — `SYSTEM_TEMPLATE` 을 고치면 설문 출력이
  달라져 M5 시나리오를 재검증해야 한다
- `app/llm.py`: `compile_spec_from_text()`. `compile_spec()` 관측 동작 무변경
- `app/main.py`: `POST /compile/free`, 설문 `note` 에도 같은 스캔 연결,
  하드캡을 `_apply_hardcaps()` 공용 헬퍼로 통합.
  `describe_slots(scan, spec_json, clamps)` 는 **하드캡 적용 뒤**에 호출한다 —
  클램프에 기인한 차이를 `conflict` 로 오판하지 않으려면 조정 내역이 필요하다

**하지 말 것 (제약)**
- `StrategySpec` 변경 금지 / 새 라이브러리 금지 / `static/index.html` 수정 금지
- 하드캡 값·경제지표를 프롬프트에 넣지 말 것 (§18.2 / §17.2).
  **주입 방어가 하드캡 값을 노출하는 방식이 되어서는 안 된다** — 셀프체크가 assert 한다
- `enforce_hardcaps()` 를 `compile_spec_from_text()` 안에 넣지 말 것.
  재시도가 실패 사유를 프롬프트에 덧붙이므로 그 경로로 상한값이 샌다

**완료 기준**
- `python -m app.intent` — 요구/언급 양쪽 케이스, **주입 시도**, 오탐 방지, 렉시콘
  정합성 assert 통과 (DB·Ollama 없이)
- 슬롯 대조(§19.3.1) assert: **부정어 케이스**("너무 공격적이진 않게" → `check: conflict`),
  클램프 3경우(요구를 깎음=consistent / 클램프 없이 다름=conflict / 클램프가 있어도
  조정 전 값이 이미 다름=conflict), `style` = `unverifiable`,
  `note` 의 **자립성**(약칭 금지·매치 표현과 Spec 필드명 포함)과 **원인 불단정**
  (부정어 유무와 무관하게 note 가 동일해야 한다 — 다르면 원인을 단정하는 것)
- `python -m app.prompt` — 설문 프롬프트 무회귀 + 하드캡·지표 미노출 + 태그 탈출 무력화
- 자유 입력 한 단락 → 유효한 Spec. **복합 의도**("반도체 + 배당주도 섞어")가 한 Spec 에
- 언급 안 한 슬롯이 `slots[*].source == "inferred"` 로 구분된다 (`check` 는 `null`)
- 클램프가 걸린 요청에서 `slots.max_loss.check` 가 **`consistent`** (conflict 아님)
- 유니버스 밖 섹터 **요구** → 400 + 사유 / 레버리지 **요구** → 400 + 사유
- 맥락 **언급**만("예전에 코인으로 물려서") → **200 + `notices`** (거부 아님)
- 주입 시도 → 200. 1층 뚫려도 2층이 형태 유지, 3층이 클램프 (`clamps` 1건)
- 2001자 → 422 (LLM 호출 전)
- 회귀: 설문 경로 `/compile` 응답 키 4개 그대로(`notices` 는 비었을 때 안 붙는다),
  `/health` `/indicators`(as-of 4종) `/specs` 기존과 동일. **DDL 변경 없음**

**범위 밖으로 남긴 것**
- UI (다음 작업). `static/index.html` 무수정
- 되묻기 다중턴 — 미언급 슬롯은 `slots` 로 드러내고 정정은 U-5 Spec 확인 단계가 맡는다.
  `/compile` 이 무상태라 세션 계층이 필요하다 (CLAUDE.md §2 제외 유지, 근거만 갱신)
- **종목별 비중**("반도체 비중은 줄이되") — `StrategySpec` 에 비중 필드가 없어서다.
  M8 의 단일종목 상한 스텁과 **같은 gap** 이고 P3 배분 계층에서 스키마 확장과 함께 재검토.
  데모 범위의 단순화이며 본 시스템에서는 해소되어야 한다
- 요구/언급의 **완전한** 구별 — 어휘 매칭으로는 불가능하다. 경계는 통과 쪽으로
  실패시키고(fail open) 감지 사실을 `notices` 에 남긴다. 근거는 CLAUDE.md §19.3
- `check == "conflict"` 의 **원인 구별** — ①사용자가 부정 표현을 썼고 LLM 이 옳게 읽음,
  ②LLM 이 사용자를 무시함. 둘이 같은 관측을 낳아 구별 불가다(§19.3 요구/언급과 같은
  계열의 한계). ②는 **현재 어느 계층도 잡지 못하는 결함**이고 `check` 는 그것을
  고치지 않고 **보이게만** 한다 — 지금까지 완전히 보이지 않던 것이라 그것만으로도
  이전보다 낫다. 판정은 U-5 확인 단계에서 사람이 한다. 근거는 CLAUDE.md §19.3.1
- `style` 슬롯의 대조 — 스타일→지표 매핑이 프롬프트의 산문 규칙이라 기계가 읽는 계약이
  아니다. `consistent` 로 뭉개지 않고 **`unverifiable`** 이라고 말한다(M8 하드캡 스텁이
  `ok` 대신 `undecidable` 을 돌려주는 것과 같은 방침)

## 범위 밖 (이번 데모에서 안 함)

RAG · 백테스팅 · 승인 게이트 · 다중턴 되묻기. — CLAUDE.md §2 제외 항목 그대로.
- 자유 입력은 M9 로 편입 — 단 **API 계층까지**. UI 는 다음 작업이고 되묻기는 여전히 범위 밖
  (근거가 "설문이 슬롯을 채우므로"에서 "출처 기록 + U-5 확인 단계"로 갱신됐다).
- 경제지표 DB 는 M7 로 편입 — 단 **조회 계층까지만**, 소비/실 API 연동은 여전히 범위 밖.
- 하드캡은 M8 로 편입 — 단 **실제 발동은 `max_loss_pct` 클램프와 논리 모순 반려까지**.
  MDD·최소간격·단일종목은 값만 저장하는 스텁이다.
