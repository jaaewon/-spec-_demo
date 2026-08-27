"""ETF 유니버스 / 레버리지 무결성 검증.

이 파일이 존재하는 이유:
    schemas.py 의 문법 제약은 "형태"만 강제한다. etfs 가 문자열 배열이라는 건 보장되지만,
    그 문자열이 **실재하는 종목명인지**는 스키마로 표현할 수 없다.
    (유니버스는 JSON 파일에서 읽는 데이터라 코드에 박을 수 없기 때문)
    그래서 스키마 통과 후 한 겹 더 검사한다.

방어는 2단계다:
    1) load_universe()  — 로드 시점에 레버리지 종목을 아예 빼버린다.
                          → 프롬프트에 노출되지 않으므로 모델이 볼 수조차 없다 (예방)
    2) validate_etfs()  — 그래도 모델이 만들어낸 경우를 잡는다 (사후 검증)

CLAUDE.md §2 에 따라 하드캡(최대손실 상한, MDD 등)은 이번 데모 범위 밖이다.
여기서는 '유니버스/레버리지 무결성'만 본다.
"""

import json
from pathlib import Path

# __file__ = /srv/app/validators.py → .parent = /srv/app → .parent.parent = /srv
# 이렇게 파일 위치 기준으로 경로를 잡으면 어느 디렉토리에서 실행하든 동작한다.
UNIVERSE_PATH = Path(__file__).parent.parent / "data" / "etf_universe.json"

LEVERAGE_KEYWORDS = ("레버리지", "인버스", "2X", "곱버스")


def load_universe(path: Path = UNIVERSE_PATH) -> list[dict]:
    """화이트리스트 로드. 레버리지/인버스는 로드 단계에서 걸러 프롬프트에 아예 노출하지 않는다.

    지금 etf_universe.json 에는 레버리지 종목이 없으므로 이 필터는 아무것도 안 거른다.
    유니버스를 나중에 확장할 때(예: KRX 전체 목록을 긁어올 때) 자동으로 막히도록
    미리 걸어둔 안전장치.
    """
    items = json.loads(path.read_text(encoding="utf-8"))
    return [it for it in items if not _is_leveraged(it["name"])]


def validate_etfs(etfs: list[str], universe: set[str]) -> list[str]:
    """LLM 이 고른 종목명 검증. 유니버스 밖이거나 레버리지면 ValueError.

    ValueError 는 main.py 에서 잡아 HTTP 400 으로 변환된다.
    """
    if not etfs:
        # 스키마상 빈 배열도 유효한 list[str] 이므로 여기서 따로 막는다.
        raise ValueError("etfs가 비어 있음")

    for name in etfs:
        # 레버리지 검사를 먼저 한다: 에러 메시지가 "유니버스 밖"보다
        # "레버리지 금지"인 편이 원인 파악에 도움이 되기 때문.
        if _is_leveraged(name):
            raise ValueError(f"레버리지/인버스 금지: {name}")
        if name not in universe:
            raise ValueError(f"유니버스 밖 종목: {name}")
    return etfs


def _is_leveraged(name: str) -> bool:
    """이름에 레버리지 키워드가 들어있는지. 대소문자 무시."""
    # "2X" 와 "2x" 를 모두 잡기 위해 양쪽을 대문자로 맞춰 비교한다.
    # 한글 키워드는 대소문자 개념이 없어 upper() 해도 그대로다.
    upper = name.upper()
    return any(k.upper() in upper for k in LEVERAGE_KEYWORDS)


# --------------------------------------------------------------------------
# 셀프체크: `docker compose exec api python -m app.validators`
# 통과 케이스 1개 + 거부돼야 하는 케이스 6개.
# "거부돼야 하는데 통과해버리는" 실수를 잡는 게 목적이라
# try/except/else 구조로 '예외가 안 났으면 실패' 처리한다.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    uni = load_universe()
    names = {it["name"] for it in uni}
    assert len(uni) == 13, len(uni)
    assert "KODEX 반도체" in names

    assert validate_etfs(["KODEX 반도체", "TIGER 200"], names)

    for bad, why in [
        (["KODEX 레버리지"], "레버리지"),
        (["KODEX 인버스"], "인버스"),
        (["KODEX 코스닥150레버리지"], "레버리지"),
        (["TIGER 미국나스닥100 2x"], "소문자 2x"),
        (["KODEX 은행"], "유니버스 밖 (실재하지만 화이트리스트에 없음)"),
        ([], "빈 리스트"),
    ]:
        try:
            validate_etfs(bad, names)
        except ValueError:
            pass          # 예상대로 거부됨
        else:
            raise AssertionError(f"거부됐어야 함: {bad} ({why})")

    print(f"ok — 유니버스 {len(uni)}종, 검증 통과")
