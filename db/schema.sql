-- docker-entrypoint-initdb.d 로 자동 실행 (최초 볼륨 생성 시 1회)
-- 스키마를 고쳤으면 `docker compose down -v` 로 볼륨을 지워야 다시 반영된다.
CREATE TABLE requests (
    id          SERIAL PRIMARY KEY,
    survey      JSONB NOT NULL,          -- 설문 응답 원본
    nl_text     TEXT,                    -- 자유 서술/조합된 자연어
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- --------------------------------------------------------------------------
-- 시스템 하드캡 프로파일 (CLAUDE.md §18) — Validator 4계층 중 4번째 계층의 '값'.
--
-- specs 가 이 테이블을 FK 로 참조하므로 specs 보다 먼저 만든다.
--
-- 왜 코드 상수가 아니라 테이블인가:
--   하드캡은 운영 중 조정되는 정책값이다. 코드에 박으면 값을 바꿀 때마다
--   배포가 필요하고, "언제 무슨 값이었는지"가 git log 에만 남아 Spec 행과 대조가 안 된다.
--   테이블에 두면 specs.hardcap_version 으로 "이 Spec 은 어떤 정책 아래 나왔는가"가
--   데이터로 이어진다.
--
-- 왜 UPDATE 가 아니라 새 버전 INSERT 인가:
--   덮어쓰면 과거 Spec 에 적용됐던 캡을 영영 복원할 수 없다.
--   indicator_observations 가 개정을 새 행으로 쌓는 것과 같은 발상 (§17).
--
-- 활성 버전 = MAX(version). is_active BOOLEAN 을 안 쓴 이유:
--   새 버전을 켜려면 이전 행을 UPDATE 해서 꺼야 하는데, 그게 바로 여기서 금지하는
--   변경이다. 게다가 활성 행이 0개이거나 2개인 불법 상태를 스키마가 못 막는다.
--   MAX(version) 은 활성 여부가 데이터에서 파생되므로 불일치가 원천적으로 불가능하다.
-- --------------------------------------------------------------------------
CREATE TABLE hardcap_profile (
    -- SERIAL 이 아니라 명시 INT: 버전을 사람이 의도적으로 정해 넣는 값으로 두려는 것.
    -- 새 정책 = INSERT ... version = 2. 그 순간부터 새 요청에 적용된다.
    version               INT PRIMARY KEY,

    max_loss_pct_cap      NUMERIC NOT NULL,  -- 1회 손실 한도 상한 (%) — 초과 시 클램프
    mdd_pct_cap           NUMERIC NOT NULL,  -- MDD 상한 (%) — 데모에선 판정 불가(스텁)
    min_rebalance_days    INT     NOT NULL,  -- 리밸런싱 최소 간격 (일)
    single_etf_weight_cap NUMERIC NOT NULL,  -- 단일종목 비중 상한 (%) — 판정 불가(스텁)

    note                  TEXT,              -- 이 버전을 왜 이렇게 잡았는지
    created_at            TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE specs (
    id          SERIAL PRIMARY KEY,
    request_id  INT REFERENCES requests(id),
    spec        JSONB NOT NULL,          -- StrategySpec JSON (**클램프가 적용된 최종본**)
    version     INT NOT NULL DEFAULT 1,
    model       TEXT,                    -- 사용 모델 태그
    -- 이 Spec 을 만들 때 snapshot_date 기준으로 보였던 경제지표 스냅샷.
    -- version/model 을 박제하는 것과 같은 취지 — "어떤 세계에서 만들어진 Spec 인가"를
    -- 나중에 재현·감사할 수 있어야 한다. 지표를 못 읽었으면 {} 가 들어간다.
    indicators  JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- 하드캡이 무엇을 어떻게 바꿨는지 (CLAUDE.md §18).
    -- [{"field","requested","applied","cap","limit","reason"}, ...] / 조정 없으면 []
    --
    -- 별도 테이블이 아니라 JSONB 컬럼인 이유:
    --   (a) 항상 소속 Spec 과 함께 읽힌다 — clamps 만 따로 조회할 상황이 없다.
    --   (b) 항목 수가 캡 개수로 묶여 있다 (현재 최대 4).
    --   (c) 한 번 쓰이면 불변이다.
    --   테이블로 빼면 /specs 조회마다 JOIN 이 하나 늘 뿐 얻는 질의 자유도가 없다.
    --   indicator_observations 를 테이블로 뺀 것과는 상황이 반대다 — 그쪽은 행이
    --   독립적으로 계속 쌓이고 시점으로 조회되지만, clamps 는 둘 다 아니다.
    --   바로 위 indicators 컬럼과 같은 판단이다.
    clamps      JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- 어느 하드캡 정책 아래에서 만들어졌는지. clamps 만 있으면 나중에 해석이 안 된다
    -- ("20 으로 잘렸다"는 알겠는데 그때 상한이 20 이었는지 알 수 없으므로).
    -- JSONB 안이 아니라 별도 컬럼인 이유: "정책 v1 로 만들어진 Spec 전부" 같은
    -- 질의가 실제로 필요하고, 그건 스칼라 컬럼이라야 인덱스가 먹는다.
    hardcap_version INT REFERENCES hardcap_profile(version),

    created_at  TIMESTAMPTZ DEFAULT now()
);

-- --------------------------------------------------------------------------
-- 경제지표 피처 저장소 (CLAUDE.md §17)
--
-- 메타(indicators)와 관측치(indicator_observations)를 나눈 이유:
--   메타는 지표당 1행이고 거의 안 변한다. 관측치는 발표할 때마다 늘어난다.
--   한 테이블에 합치면 발표 때마다 지표명·단위·출처가 통째로 복제되고,
--   출처 표기 하나 고치는 데 전체 이력을 UPDATE 해야 한다.
-- --------------------------------------------------------------------------
CREATE TABLE indicators (
    code        TEXT PRIMARY KEY,        -- 'KR_CPI_YOY'
    name        TEXT NOT NULL,           -- '소비자물가지수 전년동월비'
    unit        TEXT NOT NULL,           -- '%', 'KRW/USD'
    source      TEXT NOT NULL,           -- 'ECOS' | 'FRED' — 출처 표기 필수
    frequency   TEXT NOT NULL            -- 'daily' | 'monthly' | 'irregular'
);

CREATE TABLE indicator_observations (
    id               SERIAL PRIMARY KEY,
    -- FK: 메타에 없는 지표코드의 관측치는 적재 단계에서 거부된다.
    -- ETF 유니버스 화이트리스트와 같은 발상.
    indicator_code   TEXT NOT NULL REFERENCES indicators(code),

    -- 두 날짜를 반드시 분리해서 갖는다 (이 테이블의 존재 이유).
    observation_date DATE NOT NULL,      -- 지표가 가리키는 시점 (예: 2026년 7월 CPI)
    release_date     DATE NOT NULL,      -- 그 값이 실제로 공개된 시점 (예: 2026-08-04)

    value            NUMERIC NOT NULL,
    note             TEXT,               -- '속보치' / '개정치' 같은 사람용 메모
    created_at       TIMESTAMPTZ DEFAULT now(),

    -- 개정(revision) 을 담기 위한 3컬럼 유니크.
    -- 같은 observation_date 라도 release_date 가 다르면 별도 행으로 공존한다.
    -- UPDATE 로 덮어쓰면 "개정 전 시점에 보였던 값"을 영영 복원할 수 없다.
    UNIQUE (indicator_code, observation_date, release_date)
);

-- as-of 쿼리 전용 인덱스. WHERE release_date <= T 로 자르고
-- observation_date DESC 로 정렬하는 접근 패턴을 그대로 따라간다.
CREATE INDEX idx_obs_asof
    ON indicator_observations (indicator_code, release_date, observation_date DESC);
