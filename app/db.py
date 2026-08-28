"""PostgreSQL 저장/조회.

원문(설문)과 생성된 Spec 을 **함께** 남기는 것이 데모 성공 기준 3 (CLAUDE.md §1).
"어떤 입력이 어떤 Spec 이 됐는가"를 나중에 추적할 수 있어야 하기 때문에
테이블을 requests / specs 둘로 나누고 FK 로 연결한다. (스키마는 db/schema.sql)

ORM(SQLAlchemy) 을 안 쓰는 이유: 테이블 몇 개, 쿼리 몇 개짜리 데모라
ORM 을 얹으면 코드가 오히려 늘어난다. psycopg 로 직접 쓴다.

경제지표 테이블(indicators / indicator_observations) 관련 쿼리는 여기가 아니라
app/indicators.py 에 모아 뒀다. 나중에 실제 API 어댑터로 바꿀 때 그 파일 하나만 보면
되도록 하기 위해서다. 연결 생성(_connect)만 이 파일에서 가져다 쓴다.

반면 hardcap_profile 쿼리는 **여기에 둔다** (CLAUDE.md §18). 지표와 달리 외부 소스로
교체될 일이 없는 내부 정책 테이블이고, 무엇보다 app/validators.py 를 DB 로부터
떼어놓기 위해서다 — validator 가 캡 값을 인자로만 받으면 "값을 코드에 박지 않았다"가
구조로 보장되고, 셀프체크도 DB 없이 돈다.
"""

import json
import os
from pathlib import Path

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


def save_spec(request_id: int, spec: dict, model: str, indicators: dict | None = None,
              clamps: list[dict] | None = None, hardcap_version: int | None = None) -> int:
    """생성된 Spec 저장. 어떤 요청(request_id)에서, 어떤 모델로 나왔는지 함께 기록.

    indicators : spec.snapshot_date 기준 as-of 로 조회된 경제지표 스냅샷 (CLAUDE.md §17).
                 "이 Spec 이 만들어질 때 보였던 세계"를 version/model 과 같이 박제한다.
                 지표를 못 읽었으면 None → {} 로 저장된다 (지표는 선택적 부가정보이지 의존성이 아니다).

    clamps / hardcap_version : 하드캡 계층이 무엇을 어떻게 바꿨는지 + 그때 적용된 정책 버전
                 (CLAUDE.md §18). 저장되는 spec 은 **클램프가 적용된 최종본**이라,
                 이 두 값이 없으면 나중에 "왜 20 인가"를 되짚을 수가 없다.
                 요청값은 requests.survey 에, 조정 내역은 여기에, 정책 원본은
                 hardcap_profile 에 — 셋을 이으면 조정 과정이 전부 복원된다.
    """
    with _connect() as conn:
        row = conn.execute(
            """INSERT INTO specs
                   (request_id, spec, version, model, indicators, clamps, hardcap_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (request_id, Json(spec), spec.get("version", 1), model, Json(indicators or {}),
             Json(clamps or []), hardcap_version),
        ).fetchone()
        return row["id"]


def list_specs(limit: int = 20) -> list[dict]:
    """저장 이력. 원문 설문과 Spec 를 조인해 함께 반환한다.

    JOIN 이라 specs 가 없는 requests(=실패한 시도)는 여기 안 나온다.
    성공한 변환만 보여주는 목록.
    """
    with _connect() as conn:
        return conn.execute(
            """SELECT s.id, s.request_id, s.spec, s.model, s.indicators,
                      s.clamps, s.hardcap_version, s.created_at,
                      r.survey, r.nl_text
               FROM specs s JOIN requests r ON r.id = s.request_id
               ORDER BY s.id DESC LIMIT %s""",   # 최신순
            (limit,),
        ).fetchall()


# ==========================================================================
# 하드캡 프로파일 (CLAUDE.md §18)
#
# 캡 **값**은 여기(DB)에서만 나온다. app/validators.py 는 값을 인자로 받을 뿐이라
# DB 도 이 파일도 import 하지 않는다 — 값을 코드에 박지 않았다는 게 구조로 강제된다.
# ==========================================================================

HARDCAP_SEED_PATH = Path(__file__).parent.parent / "data" / "hardcap_profile.json"

# 프로파일의 캡 컬럼들. NUMERIC 은 psycopg 가 Decimal 로 주는데, Decimal 은
# json.dumps 가 못 다루고 float 과의 비교도 성가시다. 경계에서 float 으로 바꾼다.
_HARDCAP_NUMERIC = ("max_loss_pct_cap", "mdd_pct_cap", "single_etf_weight_cap")


def seed_hardcap_profile() -> dict:
    """seed JSON 의 프로파일을 적재한다. **여러 번 실행해도 안전하다(멱등).**

    ON CONFLICT (version) DO NOTHING 인 게 핵심이다. indicators 메타처럼
    DO UPDATE 로 덮어쓰지 않는다 — 이미 적재된 버전을 seed 파일 수정으로 바꿀 수 있으면
    그 버전으로 만들어진 과거 Spec 의 근거가 조용히 달라진다.
    값을 바꾸려면 seed 에 새 version 을 추가해야 한다 (= 추가만 가능한 원장).
    """
    raw = json.loads(HARDCAP_SEED_PATH.read_text(encoding="utf-8"))
    profiles = raw["profiles"]          # "_readme" / "_rationale" 같은 메모 키는 자연히 무시된다

    with _connect() as conn:
        conn.cursor().executemany(
            """INSERT INTO hardcap_profile
                   (version, max_loss_pct_cap, mdd_pct_cap,
                    min_rebalance_days, single_etf_weight_cap, note)
               VALUES (%(version)s, %(max_loss_pct_cap)s, %(mdd_pct_cap)s,
                       %(min_rebalance_days)s, %(single_etf_weight_cap)s, %(note)s)
               ON CONFLICT (version) DO NOTHING""",
            profiles,
        )
    return {"seeded_versions": [p["version"] for p in profiles]}


def load_active_hardcap_profile() -> dict:
    """현재 활성 하드캡 프로파일. **요청마다 새로 읽는다.**

    llm.py 의 _UNIVERSE 처럼 모듈 로드 시 캐시하지 않는 이유:
        하드캡은 운영 중 조정되는 정책값이다. 캐시하면 새 버전을 INSERT 해도
        컨테이너를 재시작해야 반영된다. 30~60초짜리 LLM 호출 옆에서
        단일 행 SELECT 하나는 무시할 수 있는 비용이다.

    활성 버전 = MAX(version). is_active 플래그를 쓰면 새 버전을 켤 때 이전 행을
    UPDATE 해야 하는데, 그게 바로 이 테이블이 금지하는 변경이다.

    행이 없거나 조회에 실패하면 **예외를 던진다(fail closed).** 지표 계층이
    실패해도 {} 로 넘어가는 것(§17)과 의도적으로 반대다 — 지표는 부가정보지만
    하드캡은 안전 계층이라, 조용히 사라진 채로 Spec 을 내보내는 게 에러보다 나쁘다.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT version, max_loss_pct_cap, mdd_pct_cap,
                      min_rebalance_days, single_etf_weight_cap, note
                 FROM hardcap_profile
                ORDER BY version DESC LIMIT 1"""
        ).fetchone()

    if row is None:
        raise LookupError("hardcap_profile 이 비어 있음 — 적용할 하드캡 정책이 없다")
    return {**row, **{k: float(row[k]) for k in _HARDCAP_NUMERIC}}


def hardcap_status() -> str:
    """/health 용. 활성 버전과 캡 값을 한 줄로.

    지표(indicators_status)와 달리 'empty' 라는 정상 상태가 없다 — 프로파일이
    없으면 /compile 이 503 이므로 그건 error 다.
    """
    try:
        p = load_active_hardcap_profile()
    except Exception as e:
        return f"error: {e}"
    return (f"ok (v{p['version']} / max_loss<={p['max_loss_pct_cap']:g}% "
            f"mdd<={p['mdd_pct_cap']:g}% rebalance>={p['min_rebalance_days']}d "
            f"single_etf<={p['single_etf_weight_cap']:g}%)")


def get_spec(spec_id: int) -> dict | None:
    """Spec 하나 조회. 없으면 None (main.py 에서 404 로 변환)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, request_id, spec, model, created_at FROM specs WHERE id = %s",
            (spec_id,),
        ).fetchone()
