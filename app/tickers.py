"""ETF 종목명 → KRX 티커 매핑 및 시세 로드.

이 파일이 존재하는 이유:
    Spec 의 etfs 는 "KODEX 반도체" 같은 **사람이 읽는 이름**이다 (유니버스가 그렇게 정의돼 있으므로).
    반면 pykrx 는 "091160" 같은 6자리 티커로만 시세를 준다. 그 사이를 잇는 계층.

매핑을 유니버스 CSV 에서 유도하는 이유:
    pykrx 의 종목명 조회 API(get_market_ticker_name / get_etf_ticker_list)가
    현재 KRX 사이트와 맞지 않아 빈 값을 돌려준다. 이름으로 티커를 자동 조회할 방법이 없어
    표가 필요한데, 그 표를 **여기에 또 쓰지는 않는다.**

    data/etf_universe.csv 의 code 컬럼이 종목코드의 유일한 출처다. 여기에 표를 하나 더 두면
    같은 정보의 두 번째 사본이 되고, 유니버스가 바뀔 때마다 한쪽만 갱신돼 조용히 어긋난다.
    실제로 유니버스가 etf_universe.json(13종) → etf_universe.csv(20종) 로 교체됐을 때
    이 파일의 하드코딩 표만 남아 10종이 매핑 누락, 3종이 고아가 됐다.
    (구 etf_universe.json 에는 code 컬럼이 아예 없어 표를 손으로 들고 있어야 했다.
     csv 가 code 를 갖게 되면서 그 이유가 사라졌다.)

    <<미검증 사항>> csv 의 code 가 그 name 의 실제 종목코드인지는 **레포 안에서 검증할 수 없다.**
    위 pykrx 이름 조회가 죽어 있어 code → 실제 종목명 대조가 불가능하다. 시세 조회가
    성공한다는 건 "그 코드가 상장 종목"이라는 뜻이지 "이름이 맞다"는 뜻이 아니다.
    현재 알려진 의심 사례: 211900 (구 매핑은 'TIGER 배당성장', csv 는 'KODEX 코리아배당성장').
    csv 를 따르되 KRX 확인은 별도 과제로 남긴다 (CLAUDE.md §16).

시세 조회에 get_market_ohlcv_by_date 를 쓰는 이유:
    ETF 전용 API(get_etf_ohlcv_by_date)는 현재 빈 DataFrame 을 반환한다.
    ETF 도 상장 종목이라 일반 종목 API 로 정상 조회된다.
"""

from __future__ import annotations

import pandas as pd

from app.validators import load_universe

# 종목명 → KRX 티커. data/etf_universe.csv 의 (name, code) 를 그대로 옮긴 것이다.
# 여기에 값을 직접 적지 말 것 — 종목을 추가/변경하려면 csv 를 고친다.
#
# load_universe() 를 쓰므로 레버리지/인버스는 이미 걸러진 상태로 들어온다.
# code 는 csv 모듈이 str 로 읽어 "069500" 의 앞자리 0 이 살아 있다 (셀프체크가 확인한다).
NAME_TO_TICKER: dict[str, str] = {it["name"]: it["code"] for it in load_universe()}


def to_ticker(name: str) -> str:
    """종목명 → 티커. 매핑에 없으면 ValueError.

    validate_etfs() 를 이미 통과한 이름만 여기 들어오는 게 정상이므로,
    여기서 터진다는 건 유니버스 CSV 를 읽은 뒤에 종목명이 바뀌었거나
    (--reload 전이라) 모듈이 들고 있는 유니버스가 낡았다는 뜻이다.
    """
    try:
        return NAME_TO_TICKER[name]
    except KeyError:
        raise ValueError(
            f"티커 매핑 없음: {name} — data/etf_universe.csv 에 해당 종목이 있는지 확인 필요"
        ) from None


def load_close(names: list[str], start: str, end: str) -> pd.DataFrame:
    """종목명 리스트 → 종가 DataFrame (컬럼 = 종목명, 인덱스 = 거래일).

    start/end 는 'YYYYMMDD' 문자열 (pykrx 규격).
    컬럼을 티커가 아니라 **종목명**으로 두는 이유: 리포트에 그대로 찍히기 때문.

    거래일이 종목마다 미세하게 다를 수 있어 concat 후 ffill 한다.
    (신규 상장 등으로 앞쪽이 비는 경우는 dropna 로 잘라낸다 — 백테스트 구간이
     모든 종목이 존재하는 구간으로 자동 정렬된다)
    """
    from pykrx import stock  # 임포트가 느려서 함수 안에서 (앱 기동 시간에 영향 없도록)

    series = {}
    for name in names:
        df = stock.get_market_ohlcv_by_date(start, end, to_ticker(name))
        if df.empty:
            raise ValueError(f"시세 없음: {name} ({to_ticker(name)}) {start}~{end}")
        series[name] = df["종가"]

    close = pd.concat(series, axis=1).ffill().dropna()
    if close.empty:
        raise ValueError(f"공통 거래일 없음: {names}")
    return close


# --------------------------------------------------------------------------
# 셀프체크: `docker compose exec api python -m app.tickers`
# 유니버스와 매핑이 어긋나는 걸 잡는 게 목적. 네트워크도 한 번 태운다.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    from app.validators import load_universe

    universe = {it["name"] for it in load_universe()}
    # 유도 방식이라 아래 두 검사는 원리적으로 깨질 수 없다. 그래도 남겨 두는 이유:
    # 누군가 다시 하드코딩 표를 들고 오면 그 순간 여기서 걸린다.
    missing = universe - NAME_TO_TICKER.keys()
    extra = NAME_TO_TICKER.keys() - universe
    assert not missing, f"티커 매핑 누락: {missing}"
    assert not extra, f"유니버스에 없는 매핑: {extra}"

    # 앞자리 0 이 살아있는 6자리 문자열인지. 숫자로 파싱된 코드가 섞이면
    # pykrx 조회가 조용히 빈 결과를 준다 (에러가 아니라 빈 DataFrame 이라 더 위험하다).
    for name, code in NAME_TO_TICKER.items():
        assert isinstance(code, str) and len(code) == 6 and code.isdigit(), (name, code)

    close = load_close(["KODEX 200", "TIGER 200"], "20250101", "20250331")
    assert list(close.columns) == ["KODEX 200", "TIGER 200"]
    assert len(close) > 50, len(close)

    print(f"ok — 매핑 {len(NAME_TO_TICKER)}종, 유니버스와 일치 (csv 에서 유도)")
    print(f"     시세 로드 확인: {len(close)}거래일 "
          f"({close.index.min().date()} ~ {close.index.max().date()})")
    # 코드↔이름 정합성은 여기서 검증할 수 없다 (모듈 상단 <<미검증 사항>> 참고).
    print("     주의: code 가 그 name 의 실제 종목코드인지는 이 셀프체크로 확인되지 않는다")
