"""PostgreSQL 저장/조회.

원문(설문)과 생성된 Spec 을 **함께** 남기는 것이 데모 성공 기준 3 (CLAUDE.md §1).
"어떤 입력이 어떤 Spec 이 됐는가"를 나중에 추적할 수 있어야 하기 때문에
테이블을 requests / specs 둘로 나누고 FK 로 연결한다. (스키마는 db/schema.sql)

ORM(SQLAlchemy) 을 안 쓰는 이유: 테이블 2개, 쿼리 3개짜리 데모라
ORM 을 얹으면 코드가 오히려 늘어난다. psycopg 로 직접 쓴다.
"""

import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

DATABASE_URL = os.environ["DATABASE_URL"]


def _connect():
    """DB 연결 생성.

    row_factory=dict_row : 조회 결과를 튜플이 아니라 dict 로 받는다.
                           row["id"] 처럼 이름으로 접근할 수 있고,
                           FastAPI 가 그대로 JSON 으로 직렬화해 준다.
    """
    # ponytail: 요청마다 새 연결. 데모 트래픽엔 충분, 부하 생기면 psycopg_pool로.
    return psycopg.connect(DATABASE_URL, connect_timeout=5, row_factory=dict_row)


def save_request(survey: dict, nl_text: str) -> int:
    """설문 원문 저장 → 생성된 id 반환.

    with 블록을 벗어날 때 커밋되고 연결이 닫힌다.
    (예외가 나면 자동 롤백)
    """
    with _connect() as conn:
        row = conn.execute(
            # %s 자리표시자로 값을 넘긴다 = SQL 인젝션 방지.
            # 문자열 f-string 으로 SQL 을 만들면 안 되는 이유.
            "INSERT INTO requests (survey, nl_text) VALUES (%s, %s) RETURNING id",
            # Json(...) 으로 감싸야 dict 가 JSONB 컬럼에 들어간다.
            # 안 감싸면 psycopg 가 dict 를 어떻게 변환할지 몰라 에러.
            (Json(survey), nl_text),
        ).fetchone()
        # RETURNING id : INSERT 하면서 방금 만들어진 PK 를 같이 받아온다.
        # (INSERT 후 별도 SELECT 를 날릴 필요가 없다)
        return row["id"]


def save_spec(request_id: int, spec: dict, model: str) -> int:
    """생성된 Spec 저장. 어떤 요청(request_id)에서, 어떤 모델로 나왔는지 함께 기록."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO specs (request_id, spec, version, model) VALUES (%s, %s, %s, %s) RETURNING id",
            (request_id, Json(spec), spec.get("version", 1), model),
        ).fetchone()
        return row["id"]


def list_specs(limit: int = 20) -> list[dict]:
    """저장 이력. 원문 설문과 Spec 를 조인해 함께 반환한다.

    JOIN 이라 specs 가 없는 requests(=실패한 시도)는 여기 안 나온다.
    성공한 변환만 보여주는 목록.
    """
    with _connect() as conn:
        return conn.execute(
            """SELECT s.id, s.request_id, s.spec, s.model, s.created_at,
                      r.survey, r.nl_text
               FROM specs s JOIN requests r ON r.id = s.request_id
               ORDER BY s.id DESC LIMIT %s""",   # 최신순
            (limit,),
        ).fetchall()
