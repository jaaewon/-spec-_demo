"""ETF 종목명 → KRX 티커 매핑 및 시세 로드.

이 파일이 존재하는 이유:
    Spec 의 etfs 는 "KODEX 반도체" 같은 **사람이 읽는 이름**이다 (유니버스가 그렇게 정의돼 있으므로).
    반면 pykrx 는 "091160" 같은 6자리 티커로만 시세를 준다. 그 사이를 잇는 계층.

매핑을 코드에 박아둔 이유:
    pykrx 의 종목명 조회 API(get_market_ticker_name / get_etf_ticker_list)가
    현재 KRX 사이트와 맞지 않아 빈 값을 돌려준다. 이름으로 티커를 자동 조회할 방법이 없다.
    유니버스가 13종 고정 화이트리스트(data/etf_universe.json)라 박아두는 편이 안전하다.
    → 유니버스에 종목을 추가하면 여기도 함께 추가해야 한다. 셀프체크가 그 누락을 잡는다.

시세 조회에 get_market_ohlcv_by_date 를 쓰는 이유:
    ETF 전용 API(get_etf_ohlcv_by_date)는 현재 빈 DataFrame 을 반환한다.
    ETF 도 상장 종목이라 일반 종목 API 로 정상 조회된다.
"""

from __future__ import annotations

import pandas as pd

# 종목명 → KRX 티커. data/etf_universe.json 의 name 과 키가 정확히 일치해야 한다.
NAME_TO_TICKER: dict[str, str] = {
    "KODEX 200":          "069500",
    "TIGER 200":          "102110",
    "KODEX 반도체":        "091160",
    "TIGER 반도체":        "091230",
    "KODEX 2차전지산업":    "305720",
    "TIGER 2차전지테마":    "305540",
    "KODEX 코스닥150":      "229200",
    "TIGER 코스닥150":      "232080",
    "KODEX 배당가치":       "325020",
    "TIGER 배당성장":       "211900",
    "TIGER 미국S&P500":     "360750",
    "TIGER 미국나스닥100":   "133690",
    "KODEX 종합채권":       "273130",
}


def to_ticker(name: str) -> str:
    """종목명 → 티커. 매핑에 없으면 ValueError.

    validate_etfs() 를 이미 통과한 이름만 여기 들어오는 게 정상이므로,
    여기서 터진다는 건 유니버스에는 추가했는데 매핑을 빠뜨렸다는 뜻이다.
    """
    try:
        return NAME_TO_TICKER[name]
    except KeyError:
        raise ValueError(
            f"티커 매핑 없음: {name} — app/tickers.py 의 NAME_TO_TICKER 에 추가 필요"
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
    missing = universe - NAME_TO_TICKER.keys()
    extra = NAME_TO_TICKER.keys() - universe
    assert not missing, f"티커 매핑 누락: {missing}"
    assert not extra, f"유니버스에 없는 매핑: {extra}"

    close = load_close(["KODEX 200", "TIGER 200"], "20250101", "20250331")
    assert list(close.columns) == ["KODEX 200", "TIGER 200"]
    assert len(close) > 50, len(close)

    print(f"ok — 매핑 {len(NAME_TO_TICKER)}종, 유니버스와 일치")
    print(f"     시세 로드 확인: {len(close)}거래일 "
          f"({close.index.min().date()} ~ {close.index.max().date()})")
