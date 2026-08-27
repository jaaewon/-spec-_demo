-- docker-entrypoint-initdb.d 로 자동 실행 (최초 볼륨 생성 시 1회)
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
