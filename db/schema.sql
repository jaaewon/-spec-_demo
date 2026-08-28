-- docker-entrypoint-initdb.d 로 자동 실행 (최초 볼륨 생성 시 1회)
-- 스키마를 고쳤으면 `docker compose down -v` 로 볼륨을 지워야 다시 반영된다.
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
    -- 이 Spec 을 만들 때 snapshot_date 기준으로 보였던 경제지표 스냅샷.
    -- version/model 을 박제하는 것과 같은 취지 — "어떤 세계에서 만들어진 Spec 인가"를
    -- 나중에 재현·감사할 수 있어야 한다. 지표를 못 읽었으면 {} 가 들어간다.
    indicators  JSONB NOT NULL DEFAULT '{}'::jsonb,
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
