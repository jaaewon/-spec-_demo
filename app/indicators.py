"""경제지표 피처 저장소 — **as-of 조회**(시점 정합적 데이터 접근) 계층.

이 파일이 지키는 규약 하나:

    모든 관측치는 날짜를 두 개 갖는다.
      observation_date : 지표가 가리키는 시점   (예: 2026년 7월 CPI)
      release_date     : 그 값이 공개된 시점    (예: 2026-08-04)

    as_of=T 조회는 release_date <= T 인 행만 후보로 삼고,
    그중 observation_date 가 가장 최근인 값을 돌려준다.

왜 날짜를 합치면 안 되는가:
    observation_date 만 남기면 "2026-08-01 의 나"가 7월 CPI 를 이미 아는 게 된다.
    실제로는 8월 4일에야 공개된 값이다 → **미래 정보 누출**. 백테스트가 조용히 뻥튀기된다.
    반대로 release_date 만 남기면 관측 시점을 잃어 "가장 최신 지표"의 의미가 사라지고,
    개정값이 원본을 밀어내 개정 전 시점을 재현할 수 없게 된다.

--------------------------------------------------------------------------
실제 API 연동 시 교체 지점: fetch_indicator_data() **하나뿐이다.**
그 아래(적재·조회)는 데이터가 어디서 왔는지 모른다.
--------------------------------------------------------------------------
"""

import json
from datetime import date
from pathlib import Path

from app.db import _connect

# __file__ = /srv/app/indicators.py → .parent.parent = /srv
SEED_PATH = Path(__file__).parent.parent / "data" / "economic_indicators.json"


# ==========================================================================
# 1) 수집 — 여기가 어댑터 경계
# ==========================================================================

def fetch_indicator_data(path: Path = SEED_PATH) -> tuple[list[dict], list[dict]]:
    """지표 메타와 관측치를 가져온다. **지금은 정적 seed JSON 을 읽는다.**

    <<교체 지점>>
        실제 연동 시 이 함수만 ECOS / fredapi 어댑터로 갈아끼운다.
        반환 형태(메타 리스트, 관측치 리스트)만 지키면 아래 코드는 손댈 필요가 없다.
        네트워크·인증키·스케줄러는 전부 이 함수 안쪽 사정이며,
        이번 데모에는 그중 어느 것도 들어 있지 않다.

    반환값을 (메타, 관측치) 두 리스트로 평탄화하는 이유:
        seed JSON 은 읽기 좋으라고 지표 밑에 관측치를 중첩시켜 놨지만,
        DB 는 테이블 두 개로 나뉘어 있다. 그 변환을 여기서 끝내면
        seed_indicators() 는 JSON 구조를 몰라도 된다.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))

    metas: list[dict] = []
    observations: list[dict] = []
    for item in raw["indicators"]:          # "_readme" 같은 최상위 메모 키는 자연히 무시된다
        metas.append({
            "code": item["code"],
            "name": item["name"],
            "unit": item["unit"],
            "source": item["source"],
            "frequency": item["frequency"],
        })
        for obs in item["observations"]:
            observations.append({
                "indicator_code": item["code"],   # 중첩 구조에서 부모 코드를 각 행에 펼쳐 넣는다
                "observation_date": obs["observation_date"],
                "release_date": obs["release_date"],
                "value": obs["value"],
                "note": obs.get("note"),
            })
    return metas, observations


# ==========================================================================
# 2) 적재
# ==========================================================================

def seed_indicators() -> dict:
    """seed 를 DB 에 적재한다. **여러 번 실행해도 안전하다(멱등).**

    기동할 때마다 호출되므로 멱등성이 필수다:
      - 메타는 ON CONFLICT DO UPDATE — seed 파일에서 이름/출처를 고치면 반영된다.
      - 관측치는 ON CONFLICT DO NOTHING — 3컬럼 유니크에 걸려 중복 삽입이 조용히 무시된다.
        (개정값은 release_date 가 달라 충돌하지 않으므로 정상적으로 새 행이 된다)
    """
    metas, observations = fetch_indicator_data()

    with _connect() as conn:
        conn.cursor().executemany(
            """INSERT INTO indicators (code, name, unit, source, frequency)
               VALUES (%(code)s, %(name)s, %(unit)s, %(source)s, %(frequency)s)
               ON CONFLICT (code) DO UPDATE
                   SET name = EXCLUDED.name, unit = EXCLUDED.unit,
                       source = EXCLUDED.source, frequency = EXCLUDED.frequency""",
            metas,
        )
        conn.cursor().executemany(
            """INSERT INTO indicator_observations
                   (indicator_code, observation_date, release_date, value, note)
               VALUES (%(indicator_code)s, %(observation_date)s, %(release_date)s,
                       %(value)s, %(note)s)
               ON CONFLICT (indicator_code, observation_date, release_date) DO NOTHING""",
            observations,
        )
    return {"indicators": len(metas), "observations": len(observations)}


# ==========================================================================
# 3) 조회 — 이 파일의 핵심
# ==========================================================================

# DISTINCT ON (indicator_code) : 지표별로 정렬 결과의 **첫 행 하나씩만** 남긴다 (Postgres 전용).
# ORDER BY 세 줄이 "그 하나"를 고르는 규칙 전부다.
_AS_OF_SQL = """
SELECT DISTINCT ON (o.indicator_code)
       o.indicator_code, o.observation_date, o.release_date, o.value, o.note,
       i.name, i.unit, i.source
  FROM indicator_observations o
  JOIN indicators i ON i.code = o.indicator_code
 WHERE o.release_date <= %(as_of)s
   {code_filter}
 ORDER BY o.indicator_code,
          o.observation_date DESC,
          o.release_date DESC
"""
#        ↑ WHERE  : as_of 시점에 아직 공개 안 된 값을 후보에서 통째로 뺀다 (미래 정보 차단)
#        ↑ 2번째 줄: 그중 가장 최근 관측 시점
#        ↑ 3번째 줄: 같은 관측월이 둘 이상이면(=개정) 그 시점 기준 최신 개정본
#                    as_of 가 개정 전이면 개정 행은 WHERE 에서 이미 빠져 원본이 선택된다.


def get_indicators_as_of(as_of: date, codes: list[str] | None = None) -> dict:
    """as_of 시점에 '보였던' 최신 지표들. {코드: {값, 두 날짜, 메타}} 형태.

    codes=None 이면 전체 지표. 테이블이 비어 있으면 예외가 아니라 빈 dict 를 돌려준다
    — 호출부(/compile)가 지표 없이도 굴러가야 하기 때문.
    """
    sql = _AS_OF_SQL.format(
        # 코드 필터는 조건부로 끼워 넣는다. %(codes)s 는 아래에서 값으로 바인딩되므로
        # 여기 format 으로 들어가는 건 사용자 입력이 아닌 고정 문자열뿐이다 (인젝션 안전).
        code_filter="AND o.indicator_code = ANY(%(codes)s)" if codes else ""
    )
    with _connect() as conn:
        rows = conn.execute(sql, {"as_of": as_of, "codes": codes}).fetchall()

    return {
        r["indicator_code"]: {
            "name": r["name"],
            "unit": r["unit"],
            "source": r["source"],
            # NUMERIC 은 psycopg 가 Decimal 로 준다. Decimal 은 json.dumps 가 못 다루므로
            # (specs.indicators JSONB 에 그대로 저장해야 한다) 경계에서 float 으로 바꾼다.
            # 지표값은 소수 1~2자리라 float 정밀도로 충분하다.
            "value": float(r["value"]),
            # 날짜도 같은 이유로 문자열화 — 이 dict 는 그대로 JSON 응답이자 JSONB 저장값이 된다.
            "observation_date": r["observation_date"].isoformat(),
            "release_date": r["release_date"].isoformat(),
            "note": r["note"],
        }
        for r in rows
    }


def indicators_status() -> str:
    """/health 용. 테이블 유무와 적재량을 한 줄로."""
    try:
        with _connect() as conn:
            row = conn.execute(
                """SELECT (SELECT count(*) FROM indicators) AS meta,
                          (SELECT count(*) FROM indicator_observations) AS obs"""
            ).fetchone()
        if row["meta"] == 0:
            # 테이블은 있는데 비어 있는 상태. 오류는 아니다(/compile 은 정상 동작한다).
            return "empty: 지표 미적재"
        return f"ok ({row['meta']}종 / 관측치 {row['obs']}건)"
    except Exception as e:
        return f"error: {e}"


# --------------------------------------------------------------------------
# 셀프체크: `docker compose exec api python -m app.indicators`
# as-of 의 핵심 성질 3개를 직접 검증한다.
#   ① 발표 전에는 안 보이고 ② 발표 후에는 보이고 ③ 개정 전/후 값이 다르다
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print(seed_indicators())

    # ── ① 발표 직전: 2026-07 CPI 는 2026-08-04 공개 → 08-03 에는 보이면 안 된다
    before = get_indicators_as_of(date(2026, 8, 3), ["KR_CPI_YOY"])["KR_CPI_YOY"]
    assert before["observation_date"] == "2026-06-01", before
    assert before["value"] == 2.1, before

    # ── ② 발표 직후: 같은 지표를 하루 뒤에 물으면 7월치가 나온다
    after = get_indicators_as_of(date(2026, 8, 4), ["KR_CPI_YOY"])["KR_CPI_YOY"]
    assert after["observation_date"] == "2026-07-01", after
    assert after["value"] == 2.3, after

    # ── ③ 개정: seed 에 2026-08-21 개정치(2.4)가 들어 있다.
    #        관측 시점은 똑같이 2026-07-01 인데 값만 달라진다.
    revised = get_indicators_as_of(date(2026, 8, 25), ["KR_CPI_YOY"])["KR_CPI_YOY"]
    assert revised["observation_date"] == "2026-07-01", revised
    assert revised["value"] == 2.4, revised

    # ── ③-b 개정을 '지금' INSERT 해도 같은 성질이 성립하는지 (seed 에 의존하지 않는 확인).
    #        검사 후 지운다 — 셀프체크가 DB 를 오염시키면 안 된다.
    with _connect() as conn:
        conn.execute(
            """INSERT INTO indicator_observations
                   (indicator_code, observation_date, release_date, value, note)
               VALUES ('KR_CPI_YOY', '2026-07-01', '2026-08-27', 2.45, '셀프체크용 임시 개정치')""")
    try:
        assert get_indicators_as_of(date(2026, 8, 26), ["KR_CPI_YOY"])["KR_CPI_YOY"]["value"] == 2.4
        assert get_indicators_as_of(date(2026, 8, 27), ["KR_CPI_YOY"])["KR_CPI_YOY"]["value"] == 2.45
    finally:
        with _connect() as conn:
            conn.execute(
                """DELETE FROM indicator_observations
                    WHERE indicator_code = 'KR_CPI_YOY' AND release_date = '2026-08-27'""")

    # ── 전체 조회 + 코드 필터
    everything = get_indicators_as_of(date(2026, 8, 28))
    assert len(everything) == 5, everything.keys()
    assert set(get_indicators_as_of(date(2026, 8, 28), ["USD_KRW"])) == {"USD_KRW"}

    # ── 아주 이른 as_of: 아무것도 공개되지 않은 시점 → 빈 dict (예외가 아니라)
    assert get_indicators_as_of(date(2020, 1, 1)) == {}

    print("ok — as-of 조회 검증 통과 (발표 전/후, 개정 전/후, 빈 결과)")
    print(json.dumps(everything, ensure_ascii=False, indent=2))
