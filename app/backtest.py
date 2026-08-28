"""StrategySpec → vectorbt 백테스트 → 리포트.

schemas.py 의 StrategySpec 이 'LLM 의 출력 계약'이라면, 이 파일은 그 계약을
**실제 매매 시뮬레이션으로 번역하는 계층**이다. 하는 일은 세 가지다.

  1) spec.etfs        → pykrx 시세 (tickers.py)
  2) spec.signals     → 진입/청산 불리언 시계열   ← 여기가 번역의 핵심
  3) spec.rebalance / max_loss_pct → Portfolio 인자

번역 규칙 (spec 에 명시돼 있지 않아 여기서 정한 해석):
  - 같은 action 의 규칙이 여러 개면 OR 로 묶는다. (하나라도 만족하면 매매)
  - 리밸런싱 주기는 '매매 가능일'을 제한하는 것으로 해석한다.
    월간이면 신호가 월중에 떠도 그 달 마지막 거래일에만 반영된다.
  - max_loss_pct 는 종목별 손절선(stop loss)으로 쓴다.
  - 비중은 종목 균등. spec 에 비중 필드가 없으므로 자의적 배분을 피한다.

지표를 dict 레지스트리로 둔 이유:
    schemas.py 가 indicator 만 Enum 이 아니라 str 로 남겨둔 것과 같은 이유다.
    지표는 앞으로 늘어날 값이라, 추가할 때 이 dict 에 한 줄만 넣으면 되게 한다.
"""

from __future__ import annotations

import json
import operator as op
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.schemas import Action, Operator, RebalanceFreq, SignalRule, StrategySpec
from app.tickers import load_close

# 초기 자본 1천만원. 리포트 수치가 전부 여기 기준이라 상수로 고정한다.
INIT_CASH = 10_000_000

# 백테스트 기본 구간: **오늘로부터** 5년.
# spec.snapshot_date 가 아니라 오늘을 끝점으로 쓰는 이유: 과거에 만들어 둔 Spec 도
# 항상 최신 시세까지 포함해 평가하기 위해서다. (snapshot_date 는 리포트에 참고로만 남는다)
DEFAULT_YEARS = 5

# ETF 는 증권거래세가 면제라 위탁수수료만 잡는다. 슬리피지는 보수적으로 0.1%.
FEES = 0.00015
SLIPPAGE = 0.001


# --------------------------------------------------------------------------
# 1) 지표: 이름(str) → 시계열. spec 의 threshold 와 단위가 같아야 한다.
#    모멘텀/이평선 괴리율은 %, RSI 는 0~100.
# --------------------------------------------------------------------------

def _momentum_20d(close: pd.Series) -> pd.Series:
    """20거래일 수익률(%). '추세추종' 스타일이 이걸로 번역된다."""
    return close.pct_change(20) * 100


def _rsi_14(close: pd.Series) -> pd.Series:
    """RSI(14). '역추세' 스타일. 0~100 이라 threshold 도 그 범위로 온다."""
    delta = close.diff()
    # ewm(alpha=1/14) 는 Wilder 평활법과 같다.
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    # loss 가 0이면 RSI 100. 0 나눗셈을 피하려고 미세값을 더한다.
    rs = gain / (loss + 1e-12)
    return 100 - 100 / (1 + rs)


def _ma_cross_5_20(close: pd.Series) -> pd.Series:
    """단기(5)/장기(20) 이평선 괴리율(%).

    '교차'를 0 기준 부호로 표현한 것. 양수면 골든크로스 상태.
    괴리율로 두면 threshold 로 '얼마나 벌어졌을 때'까지 표현할 수 있다.
    """
    ma5, ma20 = close.rolling(5).mean(), close.rolling(20).mean()
    return (ma5 - ma20) / ma20 * 100


INDICATORS = {
    "momentum_20d": _momentum_20d,
    "rsi_14": _rsi_14,
    "ma_cross_5_20": _ma_cross_5_20,
}

# Operator Enum(">" 등) → 실제 비교 함수
OPS = {
    Operator.gt: op.gt,
    Operator.lt: op.lt,
    Operator.gte: op.ge,
    Operator.lte: op.le,
    Operator.eq: op.eq,
}

# 차트 라벨 한글화 표. vectorbt 가 붙이는 영어 라벨을 그대로 두면
# 시연할 때마다 축과 범례를 말로 설명해야 한다.
# Benchmark 만 'Buy & Hold' 로 둔다 — 번역하면 오히려 무슨 뜻인지 흐려진다.
CHART_LABELS = {
    "Cumulative Returns": "누적수익",
    "Cumulative returns": "누적수익 (원금 대비)",
    "Underwater": "낙폭 (전고점 대비)",
    "Drawdown": "낙폭",
    "Value": "전략",
    "Benchmark": "Buy & Hold",
    "Index": "날짜",
}

# 축 눈금 포맷. 키는 vectorbt 가 붙인 **원래 영어 축 제목**이라
# 서브플롯 구성이 바뀌어 축 번호(yaxis2 등)가 밀려도 엉뚱한 축에 적용되지 않는다.
AXIS_FORMAT = {
    # 누적수익은 원금 대비 배수라 "2" 보다 "2배" 가 바로 읽힌다.
    "Cumulative returns": {"ticksuffix": "배"},
    # vectorbt 기본값이 tickformat="%" 인데, 정밀도를 안 주면 -10.000000% 로 찍힌다.
    "Drawdown": {"tickformat": ".0%"},
}

# 리밸런싱 주기 → pandas resample 규칙 (각 구간의 마지막 거래일을 뽑는 데 쓴다)
REBALANCE_RULE = {
    RebalanceFreq.weekly: "W",
    RebalanceFreq.monthly: "ME",
    RebalanceFreq.quarterly: "QE",
}


# --------------------------------------------------------------------------
# 2) 신호 조립
# --------------------------------------------------------------------------

def _rule_signal(close: pd.Series, rule: SignalRule) -> pd.Series:
    """규칙 한 줄 → 불리언 시계열."""
    try:
        indicator = INDICATORS[rule.indicator]
    except KeyError:
        raise ValueError(
            f"지원하지 않는 지표: {rule.indicator} "
            f"(가능: {', '.join(INDICATORS)}) — app/backtest.py 의 INDICATORS 에 추가 필요"
        ) from None
    # NaN(워밍업 구간)은 False 로 둔다. 비교 결과의 NaN 이 그대로 흐르지 않게 명시.
    return OPS[rule.operator](indicator(close), rule.threshold).fillna(False)


def _localize(fig: dict) -> dict:
    """figure 의 영어 라벨을 한글로 바꾸고 축 눈금 포맷을 다듬는다.

    plotly 객체가 아니라 직렬화된 dict 를 훑는 이유: 라벨이 트레이스 이름 ·
    서브플롯 주석 · 축 제목 세 군데에 흩어져 있는데, dict 로 한 번에 도는 편이 짧다.
    범례 이름을 바꾸면 호버 툴팁의 이름도 따라 바뀐다.
    """
    for trace in fig.get("data", []):
        if trace.get("name") in CHART_LABELS:
            trace["name"] = CHART_LABELS[trace["name"]]

    layout = fig.get("layout", {})
    for ann in layout.get("annotations", []):        # 서브플롯 제목
        if ann.get("text") in CHART_LABELS:
            ann["text"] = CHART_LABELS[ann["text"]]

    for key, axis in layout.items():                 # xaxis, yaxis, xaxis2, ...
        if key.startswith(("xaxis", "yaxis")) and isinstance(axis, dict):
            title = axis.get("title") or {}
            text = title.get("text")
            # 포맷을 먼저 적용한다 — 키가 '번역 전' 영어 제목이기 때문.
            axis.update(AXIS_FORMAT.get(text, {}))
            if text in CHART_LABELS:
                title["text"] = CHART_LABELS[text]
    return fig


def _add_trade_markers(fig, pf) -> None:
    """누적수익 패널에 매수·매도 시점을 삼각형으로 찍는다 (fig 를 제자리에서 수정).

    vectorbt 의 orders/trades 서브플롯을 안 쓰는 이유: group_by=True 인 포트폴리오에서는
    그 패널이 비어서 나온다. 그래서 거래 기록으로 직접 만든다.

    주의: 아직 청산하지 않은 포지션(Status="Open")은 Exit Timestamp 가 '마지막 거래일'로
    채워져 있다. 그대로 찍으면 팔지도 않은 걸 판 것처럼 보이므로 매도 표시에서 뺀다.
    """
    trades = pf.trades.records_readable
    if trades.empty:
        return

    # 마커의 기준 y 는 누적수익 곡선 위의 값.
    cum = pf.value() / INIT_CASH

    # 곡선에서 살짝 띄운다 (매수는 아래, 매도는 위).
    # 정확히 선 위에 찍으면 서로 다른 두 마커가 겹쳐 뒤에 그린 쪽만 보인다.
    # 실제로 매도 이틀 뒤 매수 같은 경우 ▼ 가 ▲ 를 완전히 가렸다.
    offset = float(cum.max() - cum.min()) * 0.03

    def add(rows, time_col, name, color, symbol, sign, label):
        # 같은 날 여러 종목이 동시에 매매되면 y 도 같아 마커가 정확히 포개진다.
        # 날짜별로 묶어 하나만 찍고, 호버 문구에 그날의 매매를 모두 나열한다.
        by_date: dict = {}
        for _, r in rows.iterrows():
            by_date.setdefault(r[time_col], []).append(label(r))
        if not by_date:
            return

        times = list(by_date)
        fig.add_scatter(
            x=times,
            y=[float(cum.get(t, float("nan"))) + sign * offset for t in times],
            mode="markers",
            name=name,
            marker=dict(symbol=symbol, size=11, color=color,
                        line=dict(width=1, color="#fff")),
            text=[f"{t:%Y-%m-%d}<br>" + "<br>".join(by_date[t]) for t in times],
            hovertemplate="%{text}<extra></extra>",   # 기본 x/y 표시를 끄고 문구만
            row=1, col=1,                             # 1번 패널 = 누적수익
        )

    # 국내 시세 관행대로 매수는 빨강, 매도는 파랑.
    add(trades, "Entry Timestamp", "매수", "#ef4444", "triangle-up", -1,
        lambda r: f"{r['Column']} 매수 · {r['Avg Entry Price']:,.0f}원")

    add(trades[trades["Status"] == "Closed"], "Exit Timestamp",
        "매도", "#3b82f6", "triangle-down", +1,
        lambda r: f"{r['Column']} 매도 · {r['Avg Exit Price']:,.0f}원 ({r['Return']:+.1%})")


def _rebalance_mask(index: pd.DatetimeIndex, freq: RebalanceFreq) -> pd.Series:
    """리밸런싱 주기의 마지막 거래일만 True 인 마스크.

    resample 은 달력 기준이라 말일이 휴장일 수 있다. 그래서 '구간 내 실제 거래일 중
    최대값'을 뽑아 휴장일에 신호가 죽는 걸 막는다.
    """
    days = pd.Series(index, index=index)
    last_days = days.resample(REBALANCE_RULE[freq]).max().dropna()
    return pd.Series(index.isin(last_days.values), index=index)


def build_signals(
    close: pd.DataFrame, spec: StrategySpec
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """spec.signals → (entries, exits). close 와 같은 shape 의 불리언 DataFrame."""
    buys = [r for r in spec.signals if r.action is Action.buy]
    sells = [r for r in spec.signals if r.action is Action.sell]

    def combine(rules: list[SignalRule], series: pd.Series) -> pd.Series:
        if not rules:
            return pd.Series(False, index=series.index)
        out = _rule_signal(series, rules[0])
        for r in rules[1:]:
            out |= _rule_signal(series, r)   # 같은 action 끼리는 OR
        return out

    entries = pd.DataFrame({c: combine(buys, close[c]) for c in close.columns})
    exits = pd.DataFrame({c: combine(sells, close[c]) for c in close.columns})

    # 리밸런싱 주기 밖의 신호를 죽인다.
    mask = _rebalance_mask(close.index, spec.rebalance)
    return entries.mul(mask, axis=0).astype(bool), exits.mul(mask, axis=0).astype(bool)


# --------------------------------------------------------------------------
# 3) 실행
# --------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """리포트에 필요한 값만 담는다.

    Portfolio 객체를 그대로 들고 다니면 JSON 직렬화도 안 되고 테스트도 어려워진다.
    """

    spec: StrategySpec
    start: date
    end: date
    trading_days: int
    init_cash: float
    final_value: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    sharpe: float
    n_trades: int
    win_rate_pct: float
    benchmark_return_pct: float   # 동일비중 Buy & Hold

    # plotly figure 를 dict 로 담는다 ({"data": [...], "layout": {...}}).
    # 프론트는 Plotly.newPlot 으로 그대로 그린다.
    chart: dict

    # metrics() 에서 빼야 하는 필드. 수치 요약이 아니라 별도로 나가는 것들.
    _NON_METRIC = ("spec", "chart")

    def metrics(self) -> dict:
        """JSON 응답용 수치 요약. 날짜는 문자열로."""
        d = {k: v for k, v in asdict(self).items() if k not in self._NON_METRIC}
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        d["excess_return_pct"] = self.total_return_pct - self.benchmark_return_pct
        return d


def run_backtest(
    spec: StrategySpec, years: int = DEFAULT_YEARS, end: date | None = None
) -> BacktestResult:
    """Spec 하나를 백테스트한다. 네트워크(pykrx)를 타므로 수 초 걸린다.

    end 를 넘기지 않으면 오늘이 끝점. 과거 시점으로 재현하고 싶을 때만 지정한다.
    """
    import vectorbt as vbt   # 임포트가 무거워서 함수 안에서

    end = end or date.today()
    start = end - timedelta(days=365 * years)
    close = load_close(spec.etfs, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))

    entries, exits = build_signals(close, spec)

    # 균등 배분을 '종목별 독립 슬리브'로 구현한다.
    #   init_cash 를 종목 수로 나눠 각자에게 주고(cash_sharing=False),
    #   size=inf 로 그 슬리브의 현금을 전부 쓰게 한다.
    # → 자본 합계는 정확히 INIT_CASH, 배분은 정확히 균등이 된다.
    #   (from_signals 는 targetpercent 를 지원하지 않아 이 방식이 가장 명확하다.
    #    cash_sharing=True + percent 는 먼저 체결된 종목이 현금을 더 가져가 균등이 깨진다)
    # group_by=True 는 성과 지표를 종목별이 아니라 **포트폴리오 단위**로 집계하기 위한 것.
    pf = vbt.Portfolio.from_signals(
        close,
        entries,
        exits,
        init_cash=INIT_CASH / len(spec.etfs),
        size=np.inf,
        cash_sharing=False,
        group_by=True,
        sl_stop=spec.max_loss_pct / 100,   # 종목별 손절선
        fees=FEES,
        slippage=SLIPPAGE,
        freq="1D",
    )

    trades = pf.trades
    n_trades = int(trades.count())
    # 거래가 0건이면 win_rate 가 NaN 이라 그대로 두면 리포트에 nan 이 찍힌다.
    win_rate = float(trades.win_rate() * 100) if n_trades else 0.0

    # 벤치마크: 같은 구간을 동일비중으로 그냥 들고 있었을 때.
    # 전략이 '아무것도 안 하는 것보다 나았는가'를 보는 기준선.
    # 각 종목을 시작가 대비 배수로 바꿔 평균 = 동일비중 포트폴리오의 가치 배수.
    bench_value = INIT_CASH * (close / close.iloc[0]).mean(axis=1)
    bh = float(bench_value.iloc[-1] / INIT_CASH * 100 - 100)

    # 차트는 vectorbt 기본 플롯을 그대로 쓴다. 누적수익률 패널에 벤치마크와
    # 매매 시점 마커가 이미 들어 있고, 낙폭(underwater) 패널을 하나 더 붙였다.
    # to_html() 이 아니라 to_json() 을 쓰는 이유: to_html 은 <script> 를 포함하는데
    # innerHTML 로 넣으면 스크립트가 실행되지 않는다. 프론트가 Plotly.newPlot 으로 그린다.
    fig = pf.plot(subplots=["cum_returns", "underwater"])
    _add_trade_markers(fig, pf)
    chart = _localize(json.loads(fig.to_json()))

    return BacktestResult(
        spec=spec,
        start=close.index.min().date(),
        end=close.index.max().date(),
        trading_days=len(close),
        init_cash=INIT_CASH,
        final_value=float(pf.final_value()),
        total_return_pct=float(pf.total_return() * 100),
        cagr_pct=float(pf.annualized_return() * 100),
        max_drawdown_pct=float(pf.max_drawdown() * 100),
        sharpe=float(pf.sharpe_ratio()),
        n_trades=n_trades,
        win_rate_pct=win_rate,
        benchmark_return_pct=bh,
        chart=chart,
    )


# --------------------------------------------------------------------------
# 4) 리포트
# --------------------------------------------------------------------------

def format_report(r: BacktestResult) -> str:
    """사람이 읽는 리포트.

    터미널/로그/HTTP 응답 어디에 넣어도 되도록 문자열로 반환한다.
    """
    s = r.spec
    rules = "\n".join(
        f"│    - {x.indicator} {x.operator.value} {x.threshold:g} → {x.action.value}"
        for x in s.signals
    )
    edge = r.total_return_pct - r.benchmark_return_pct
    verdict = "전략 우위" if edge > 0 else "벤치마크 우위"
    return f"""
╭─ 전략 Spec ────────────────────────────────────────────
│  종목      {', '.join(s.etfs)}
│  신호
{rules}
│  리밸런싱  {s.rebalance.value}
│  손절선    {s.max_loss_pct:g}%
│  성향      {s.risk_profile.value}
│  근거      {s.rationale}
├─ 백테스트 ────────────────────────────────────────────
│  구간      {r.start} ~ {r.end}  ({r.trading_days}거래일)
│  초기자본  {r.init_cash:,.0f}원
│  최종자산  {r.final_value:,.0f}원
├─ 성과 ────────────────────────────────────────────────
│  총수익률  {r.total_return_pct:+.2f}%
│  연환산    {r.cagr_pct:+.2f}%
│  최대낙폭  {r.max_drawdown_pct:.2f}%
│  샤프비율  {r.sharpe:.2f}
│  거래횟수  {r.n_trades}회   승률 {r.win_rate_pct:.1f}%
├─ 벤치마크 (동일비중 Buy & Hold) ──────────────────────
│  수익률    {r.benchmark_return_pct:+.2f}%
│  초과성과  {edge:+.2f}%p  ({verdict})
╰───────────────────────────────────────────────────────
""".strip()


# --------------------------------------------------------------------------
# 셀프체크: `docker compose exec api python -m app.backtest`
# 신호 조립은 네트워크 없이 먼저 검증하고, 그다음 DB 최신 Spec 으로 실제 백테스트.
# DB 가 비어 있어도 항상 실행되도록 대체 Spec 을 둔다.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    idx = pd.bdate_range("2025-01-01", periods=120)
    fake = pd.DataFrame({"KODEX 200": pd.Series(range(100, 220), index=idx, dtype=float)})
    demo = StrategySpec(
        etfs=["KODEX 200"],
        signals=[SignalRule(indicator="momentum_20d", operator=Operator.gt,
                            threshold=0, action=Action.buy)],
        rebalance=RebalanceFreq.monthly,
        max_loss_pct=5,
        risk_profile="neutral",
        snapshot_date=date(2026, 8, 27),
        rationale="셀프체크용 Spec",
    )

    e, x = build_signals(fake, demo)
    assert e.shape == fake.shape, e.shape
    # 우상향 시리즈라 모멘텀은 계속 양수 → 월말에만 True 가 떠야 한다.
    n_entries = int(e["KODEX 200"].sum())
    assert n_entries > 0, "진입 신호가 하나도 없음"
    assert n_entries <= 6, f"월간 리밸런싱인데 신호가 {n_entries}개"
    assert not x["KODEX 200"].any(), "sell 규칙이 없는데 청산 신호가 생김"

    try:
        _rule_signal(fake["KODEX 200"],
                     SignalRule(indicator="없는지표", operator=Operator.gt,
                                threshold=0, action=Action.buy))
    except ValueError:
        pass
    else:
        raise AssertionError("모르는 지표는 거부돼야 함")

    print(f"ok — 신호 조립 검증 통과 (월간 진입신호 {n_entries}개)")

    # 여기부터는 네트워크 + 실제 백테스트.
    from app.db import list_specs

    rows = list_specs(limit=1)
    if rows:
        spec = StrategySpec.model_validate(rows[0]["spec"])
        print(f"\nDB 최신 Spec (specs.id={rows[0]['id']}) 으로 백테스트\n")
    else:
        spec = demo
        print("\nDB 에 Spec 이 없어 셀프체크용 Spec 으로 백테스트\n")

    print(format_report(run_backtest(spec)))
