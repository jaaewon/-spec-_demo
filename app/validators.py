"""Validator — 참조(유니버스/레버리지) 계층 + 하드캡 계층.

이 파일이 존재하는 이유:
    schemas.py 의 문법 제약은 "형태"만 강제한다. etfs 가 문자열 배열이라는 건 보장되지만,
    그 문자열이 **실재하는 종목명인지**는 스키마로 표현할 수 없다.
    (유니버스는 JSON 파일에서 읽는 데이터라 코드에 박을 수 없기 때문)
    그래서 스키마 통과 후 한 겹 더 검사한다.

기획서 4.1 의 Validator 4계층 중 이 파일이 담당하는 건 2번째와 4번째다:

    1계층 스키마  — schemas.py / Ollama format 문법 제약
    2계층 참조    — load_universe() + validate_etfs()   ← 아래 전반부
    3계층 논리    — (전용 계층은 아직 없음. 논리 모순 판정은 4계층 진입부에 붙어 있다)
    4계층 하드캡  — enforce_hardcaps()                   ← 아래 후반부 (CLAUDE.md §18)

두 계층의 성격이 다르다는 점이 중요하다:
    참조 계층은 **전부 반려**한다. 유니버스 밖 종목은 고쳐줄 방법이 없다
    (어느 종목으로 바꿔야 사용자 의도에 맞는지 서버가 알 수 없으므로).

    하드캡 계층은 **수치는 조정하고 구조적 위반만 반려**한다. "손실 50% 감수"는
    의도가 명확하니 상한으로 깎아 살려주는 게 맞고, "같은 조건에 buy 와 sell 이
    동시에" 는 깎을 숫자가 없으니 반려밖에 없다.

방어는 2단계다:
    1) load_universe()  — 로드 시점에 레버리지 종목을 아예 빼버린다.
                          → 프롬프트에 노출되지 않으므로 모델이 볼 수조차 없다 (예방)
    2) validate_etfs()  — 그래도 모델이 만들어낸 경우를 잡는다 (사후 검증)
"""

import csv
from pathlib import Path

# __file__ = /srv/app/validators.py → .parent = /srv/app → .parent.parent = /srv
# 이렇게 파일 위치 기준으로 경로를 잡으면 어느 디렉토리에서 실행하든 동작한다.
UNIVERSE_PATH = Path(__file__).parent.parent / "data" / "etf_universe.csv"

LEVERAGE_KEYWORDS = ("레버리지", "인버스", "2X", "곱버스")


def load_universe(path: Path = UNIVERSE_PATH) -> list[dict]:
    """화이트리스트 로드 (CSV: name,code,theme). 반환값은 {"name","code","theme"} dict 목록.

    레버리지/인버스는 로드 단계에서 걸러 프롬프트에 아예 노출하지 않는다.
    지금 etf_universe.csv 에는 레버리지 종목이 없으므로 이 필터는 아무것도 안 거른다.
    유니버스를 나중에 확장할 때(예: KRX 전체 목록을 긁어올 때) 자동으로 막히도록
    미리 걸어둔 안전장치.

    encoding="utf-8-sig" 인 이유: Excel 로 저장한 CSV 는 맨 앞에 BOM(\\ufeff)이 붙는다.
        그냥 "utf-8" 로 읽으면 첫 컬럼 키가 'name' 이 아니라 '\\ufeffname' 이 돼서
        row["name"] 이 KeyError 로 터진다. -sig 는 BOM 이 있으면 떼고 없으면 그냥 넘어간다.
    newline="" 인 이유: csv 모듈이 줄바꿈(CRLF 포함)을 직접 처리하게 두는 표준 사용법.

    code 는 문자열 그대로 둔다 — "069500" 처럼 앞자리 0 이 있어서 숫자로 바꾸면 69500 이 된다.
    (csv 모듈은 전부 str 로 읽으므로 별도 처리 불필요. pandas 를 쓸 땐 dtype={"code": str} 필수)
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        items = [row for row in csv.DictReader(f) if row.get("name")]  # 끝의 빈 줄 방어
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


# ==========================================================================
# 4계층: 시스템 하드캡 (CLAUDE.md §18)
#
# 원칙 한 줄: **수치는 클램프, 구조적 위반은 반려.**
#
# 이 아래 코드에는 캡 값이 하나도 없다. 전부 profile 인자로 들어온다.
# 일부러 그렇게 만들었다 — 값은 hardcap_profile 테이블에서만 오고,
# 이 파일은 DB 도 import 하지 않는다. 그래서
#   (a) "코드에 상수로 하드코딩하지 않는다"가 구조적으로 보장되고
#       (여기 있는 어떤 함수도 인자 없이는 캡 값을 알 수 없다)
#   (b) 셀프체크가 DB 없이 순수 함수로 돌아간다.
#
# 하드캡을 llm.py 의 재시도 루프가 아니라 여기(= 호출은 main.py)에 둔 이유:
#   llm.py 는 검증 실패 사유를 **프롬프트에 덧붙여** 재시도한다(build_user 의
#   retry_reason). 하드캡 위반을 그 경로로 흘리면 "max_loss_pct 상한 20 초과"
#   같은 문자열이 그대로 LLM 에게 전달된다 → 모델이 상한을 알게 된다.
#   그러면 모델이 경계에 맞춰 19.9 를 생성하기 시작해 클램프가 아예 안 일어나고,
#   "적대적 입력에 대한 하드캡 차단율" 지표가 0 으로 수렴해 무의미해진다.
#   그래서 하드캡은 재시도 루프 **밖에서, 사후에** 한 번만 적용한다.
# ==========================================================================

# RebalanceFreq enum 값 → 대략적인 간격(일). 캡 값이 아니라 단위 환산이라 여기 둔다.
# monthly 30 / quarterly 90 은 달력월 길이를 무시한 근사치다 — 최소 간격 비교에만
# 쓰이므로 하루 이틀 오차가 판정을 바꾸지 않는다.
_REBALANCE_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 90}

# 판정 상태 3가지. "조용히 통과"가 없다는 게 핵심 —
# 검사할 수 없는 항목은 ok 가 아니라 undecidable 을 돌려준다.
STATUS_OK = "ok"                      # 검사했고 위반 없음
STATUS_CLAMPED = "clamped"            # 수치 초과 → 상한으로 조정
STATUS_UNDECIDABLE = "undecidable"    # 판정 불가 (통과가 아니다)


# --------------------------------------------------------------------------
# 항목별 판정. 전부 "판정 결과 dict" 하나를 돌려준다 (예외를 던지지 않는다).
# --------------------------------------------------------------------------

def check_max_loss_pct(spec: dict, profile: dict) -> dict:
    """1회 손실 한도. **데모에서 실제로 발동하는 유일한 캡.**

    상한만 있고 하한은 없다. max_loss 를 낮게 잡는 건 보수적인 선택이라 막을 이유가 없다.
    """
    requested = float(spec["max_loss_pct"])
    limit = float(profile["max_loss_pct_cap"])
    base = {"cap": "max_loss_pct_cap", "field": "max_loss_pct", "limit": limit}

    if requested <= limit:
        return {**base, "status": STATUS_OK}
    return {
        **base,
        "status": STATUS_CLAMPED,
        "requested": requested,
        "applied": limit,
        # 사용자에게 그대로 보여줄 문장 (거부가 아니라 조정이라는 게 드러나야 한다)
        "reason": f"1회 손실 한도 상한 {limit:g}% 초과 ({requested:g}%) — 상한값으로 조정",
    }


def check_min_rebalance_interval(spec: dict, profile: dict) -> dict:
    """리밸런싱 최소 간격. 현행 profile(7일) 로는 발동하지 않는다.

    **스텁이 아니라 살아 있는 로직이다.** 다만 현행 RebalanceFreq 의 최소 단위가
    weekly(7일)이고 캡도 7일이라, 어떤 설문 응답도 이 캡을 위반할 수 없다.
    캡을 8 이상으로 올리거나 enum 에 daily 가 추가되면 그 즉시 클램프가 발동한다
    (셀프체크에서 min_rebalance_days=14 프로파일로 실제 발동을 확인한다).

    캡을 7보다 크게 잡지 않은 이유: 그러면 설문의 '주 1회' 선택지가 **항상** 클램프돼
    정상 응답이 매번 조정되는 상태가 된다. 조정 내역이 의미를 가지려면
    정상 입력에서는 비어 있어야 한다.
    """
    limit = int(profile["min_rebalance_days"])
    current = spec["rebalance"]
    days = _REBALANCE_DAYS[current]
    base = {"cap": "min_rebalance_days", "field": "rebalance", "limit": limit}

    if days < limit:
        applied = _shortest_freq_at_least(limit)
        if applied is None:
            # 캡을 만족하는 주기가 enum 에 하나도 없는 경우. 클램프할 대상이 없으므로
            # 조용히 통과시키지 않고 판정 불가로 올린다 (프로파일 설정 오류에 가깝다).
            return {**base, "status": STATUS_UNDECIDABLE,
                    "reason": f"캡({limit}일)을 만족하는 리밸런싱 주기가 RebalanceFreq 에 없어 "
                              f"조정할 대상이 없다 — 프로파일 값을 재검토해야 한다"}
        return {**base, "status": STATUS_CLAMPED, "requested": current, "applied": applied,
                "reason": f"리밸런싱 최소 간격 {limit}일 미만 ({current}={days}일) — "
                          f"{applied} 로 조정"}

    if min(_REBALANCE_DAYS.values()) >= limit:
        # 위반이 안 나온 게 아니라 **나올 수가 없는** 상태. ok 로 보고하면
        # "검사해서 통과했다"로 읽혀 캡이 작동 중인 것처럼 오해된다.
        return {**base, "status": STATUS_UNDECIDABLE,
                "reason": f"현행 RebalanceFreq 최소 단위가 {min(_REBALANCE_DAYS.values())}일로 "
                          f"캡({limit}일) 이상이라 어떤 설문 응답도 이 캡을 위반할 수 없다 — "
                          f"판정 자체가 성립하지 않는다 (enum 에 더 짧은 주기가 추가되면 발동)"}

    return {**base, "status": STATUS_OK}


def check_mdd_pct(spec: dict | None, profile: dict) -> dict:
    """MDD 상한 — **스텁. 판정 불가.**

    spec 인자를 아예 쓰지 않는다는 것 자체가 판정 불가의 증거다:
    MDD 는 수익률 시계열에서만 나오는 값이라 Spec 을 아무리 들여다봐도 계산할 수 없다.
    이 데모에는 백테스트 계층이 없다.

    그런데도 profile 에 값(30)을 두는 이유: '1회 손실 한도(20) < MDD 상한(30)' 관계를
    지금부터 명시해 두려는 것. 절대값 30 은 잠정치이고, P3 에서 유니버스 실측 MDD 를
    확인한 뒤 확정한다.
    """
    return {
        "cap": "mdd_pct_cap", "field": None, "limit": float(profile["mdd_pct_cap"]),
        "status": STATUS_UNDECIDABLE,
        "reason": "MDD 는 수익률 시계열에서만 산출되는데 이 데모에는 백테스트 계층이 없다. "
                  "Spec 만으로는 판정할 수 없다 — P3 백테스트 계층에서 구현.",
    }


def check_single_etf_weight(spec: dict | None, profile: dict) -> dict:
    """단일종목 비중 상한 — **스텁. 판정 불가.**

    사유는 **'StrategySpec 에 종목별 비중 필드가 없다'** 는 것 하나다.

    'ETF 라서 이미 분산돼 있으니 불필요' 가 아니다. ETF 내부 분산과 포트폴리오 내
    ETF 비중은 별개 문제다 — 'KODEX 반도체' 하나에 100% 를 배분하면 그 ETF 가 내부적으로
    수십 종목에 분산돼 있어도 포트폴리오는 반도체 섹터에 100% 노출된다.
    이 캡이 막으려는 섹터 집중 위험은 실재하며, 지금 스키마로 표현할 수 없을 뿐이다.

    스키마에 비중 필드를 추가하는 건 이번 범위 밖(기존 필드 변경 금지)이라
    P3 배분 계층에서 재검토한다.
    """
    return {
        "cap": "single_etf_weight_cap", "field": None,
        "limit": float(profile["single_etf_weight_cap"]),
        "status": STATUS_UNDECIDABLE,
        "reason": "StrategySpec 에 종목별 비중 필드가 없어 어느 종목이 몇 %인지 알 수 없다. "
                  "(ETF 내부 분산과는 무관한 문제 — 반도체 ETF 100% 배분은 여전히 "
                  "섹터 집중 위험이다.) P3 배분 계층에서 재검토.",
    }


def _shortest_freq_at_least(days: int) -> str | None:
    """캡을 만족하는 것 중 가장 짧은 리밸런싱 주기. 없으면 None.

    클램프 방향이 '가장 가까운 허용값'이어야 사용자 의도가 최대한 보존된다
    (주 1회를 막는다고 분기 1회로 던지면 원래 요청과 너무 멀어진다).
    """
    allowed = [(d, f) for f, d in _REBALANCE_DAYS.items() if d >= days]
    return min(allowed)[1] if allowed else None


# --------------------------------------------------------------------------
# 구조적 위반 (반려). 수치가 아니라 관계가 깨진 경우라 깎아서 살릴 수가 없다.
# --------------------------------------------------------------------------

def find_logical_contradictions(spec: dict) -> list[str]:
    """Spec 안에서 서로 모순되는 조합을 찾는다. 반환이 비어 있지 않으면 반려 대상.

    클램프가 아니라 반려인 이유: 여기 걸리는 것들은 조정할 '수치'가 없다.
    모순된 규칙 하나를 서버가 임의로 골라 지우면 사용자가 요청하지 않은 전략이
    만들어진다 — 그건 조정이 아니라 조작이다.
    """
    problems: list[str] = []
    signals = spec.get("signals") or []

    # ① 매매 조건이 하나도 없는 Spec.
    #    스키마상 빈 배열도 유효한 list[SignalRule] 이라 여기서 잡는다
    #    (validate_etfs 가 빈 etfs 를 잡는 것과 같은 이유).
    if not signals:
        problems.append("signals 가 비어 있음 — 매매 조건이 없는 Spec 은 실행할 수 없다")

    # ② 완전히 같은 조건에 buy 와 sell 이 동시에 걸린 경우.
    #    주의: 같은 indicator 에 buy/sell 이 함께 있는 것 자체는 정상이다
    #    (momentum_20d > 0 → buy / momentum_20d < 0 → sell 은 평범한 추세추종 전략).
    #    모순은 indicator·operator·threshold 가 **셋 다 같은데** action 만 반대일 때다.
    #    그 경우 조건이 참인 순간 buy 와 sell 이 동시에 성립해 실행 시점에 결정이 불가능하다.
    seen: dict[tuple, str] = {}
    for s in signals:
        key = (s["indicator"], s["operator"], float(s["threshold"]))
        if key in seen and seen[key] != s["action"]:
            problems.append(
                f"동일 조건({s['indicator']} {s['operator']} {float(s['threshold']):g})에 "
                f"buy 와 sell 이 동시에 지정됨 — 실행 시점에 어느 쪽인지 결정 불가")
        seen[key] = s["action"]

    # ③ 안정형인데 레버리지 종목을 담은 경우.
    #
    #    <<데모에서 실제 발동이 어려운 검사>>
    #    이 검사는 현재 파이프라인에서 사실상 도달할 수 없다. 이유가 둘 겹쳐 있다:
    #      (a) data/etf_universe.json 에 레버리지/인버스 종목이 아예 없다.
    #      (b) 설령 있어도 2계층 validate_etfs() 가 하드캡보다 먼저 돌아 반려한다.
    #    그래도 남겨 두는 이유: 유니버스가 KRX 전체로 확장되고 레버리지 종목이
    #    '성향 무관하게 허용' 정책으로 바뀌는 순간, 이 검사만이 "안정형인데 레버리지"를
    #    잡는다. 지금은 발동하지 않는 안전장치라는 걸 알고 두는 것과,
    #    없는 걸 모르고 있는 것은 다르다.
    if spec.get("risk_profile") == "conservative":
        for name in spec.get("etfs") or []:
            if _is_leveraged(name):
                problems.append(
                    f"안정형(conservative) 성향에 레버리지/인버스 종목({name})이 지정됨 "
                    f"— 성향과 상품 위험도가 모순")

    return problems


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------

# 클램프 기록에 담을 키. 요구사항: 필드명·요청값·조정값·사유가 반드시 들어간다.
# (limit/cap 은 나중에 "그때 상한이 얼마였나"를 읽을 수 있게 같이 남긴다)
_CLAMP_KEYS = ("field", "requested", "applied", "cap", "limit", "reason")

# 클램프를 적용하는 검사들. 순서대로 돌며 spec 을 조금씩 고쳐 나간다.
# 스텁 2종(MDD·단일종목)은 고칠 필드가 없으므로 여기 없다 — hardcap_report() 에만 나온다.
_CLAMP_CHECKS = (check_max_loss_pct, check_min_rebalance_interval)


def enforce_hardcaps(spec: dict, profile: dict) -> tuple[dict, list[dict]]:
    """4계층 하드캡 적용. **(조정된 spec, 조정 내역)** 을 돌려준다.

    구조적 위반이면 ValueError — main.py 가 400 으로 바꾼다.
    수치 초과는 예외가 아니라 클램프다. 요청은 200 으로 성공하고,
    무엇이 어떻게 바뀌었는지가 두 번째 반환값에 담긴다.

    입력 spec 은 건드리지 않고 복사본을 고친다 (호출부가 원본을 비교에 쓸 수 있게).
    """
    # 반려 판정이 먼저다. 어차피 되돌려 보낼 Spec 을 클램프할 이유가 없다.
    problems = find_logical_contradictions(spec)
    if problems:
        raise ValueError(" / ".join(problems))

    clamped = dict(spec)
    clamps: list[dict] = []
    for check in _CLAMP_CHECKS:
        verdict = check(clamped, profile)
        if verdict["status"] != STATUS_CLAMPED:
            continue
        clamped[verdict["field"]] = verdict["applied"]
        clamps.append({k: verdict[k] for k in _CLAMP_KEYS})
    return clamped, clamps


def hardcap_report(spec: dict, profile: dict) -> list[dict]:
    """하드캡 4항목 전체의 판정 상태. 시연·디버깅용.

    enforce_hardcaps() 는 '조정된 것'만 돌려주므로 판정 불가 항목이 보이지 않는다.
    어떤 캡이 왜 작동하지 않고 있는지를 확인하려면 이쪽을 본다.
    """
    return [
        check_max_loss_pct(spec, profile),
        check_min_rebalance_interval(spec, profile),
        check_mdd_pct(spec, profile),
        check_single_etf_weight(spec, profile),
    ]


def undecidable_caps(profile: dict) -> list[dict]:
    """Spec 없이도 '판정 불가'가 확정되는 항목들. /health 용.

    MDD·단일종목은 Spec 과 무관하게 언제나 판정 불가다.
    최소 간격은 프로파일 값에 따라 달라지므로 실제로 검사해 본다
    (가장 짧은 주기로 물었는데도 undecidable 이면 어떤 입력으로도 발동하지 않는다).
    """
    shortest = min(_REBALANCE_DAYS, key=_REBALANCE_DAYS.get)
    verdicts = [
        check_min_rebalance_interval({"rebalance": shortest}, profile),
        check_mdd_pct(None, profile),
        check_single_etf_weight(None, profile),
    ]
    return [v for v in verdicts if v["status"] == STATUS_UNDECIDABLE]


# --------------------------------------------------------------------------
# 셀프체크: `docker compose exec api python -m app.validators`
# 통과 케이스 1개 + 거부돼야 하는 케이스 6개.
# "거부돼야 하는데 통과해버리는" 실수를 잡는 게 목적이라
# try/except/else 구조로 '예외가 안 났으면 실패' 처리한다.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    uni = load_universe()
    names = {it["name"] for it in uni}
    assert len(uni) == 20, len(uni)
    assert "KODEX 반도체" in names
    # 앞자리 0 이 살아있는지 (숫자로 파싱되면 "69500" 이 된다)
    assert next(it["code"] for it in uni if it["name"] == "KODEX 200") == "069500"
    # theme 은 프롬프트에서 설문 sector 와 문자열로 매칭되므로 비어 있으면 안 된다
    assert all(it["theme"] for it in uni)

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

    print(f"ok — 유니버스 {len(uni)}종, 참조 계층 검증 통과")

    # ======================================================================
    # 4계층: 하드캡
    #
    # 프로파일을 DB 가 아니라 여기서 dict 로 만들어 넣는다. 이 파일의 함수들은
    # 캡 값을 인자로만 받으므로 DB 없이 전부 검증할 수 있다.
    # 서로 다른 프로파일로 같은 Spec 을 돌려 결과가 달라지는 걸 확인하는 게
    # "값이 코드에 박혀 있지 않다"는 가장 직접적인 증거이기도 하다.
    # ======================================================================
    SHIPPED = {"version": 1, "max_loss_pct_cap": 20, "mdd_pct_cap": 30,
               "min_rebalance_days": 7, "single_etf_weight_cap": 40}

    def spec_of(**over) -> dict:
        base = {
            "version": 1,
            "etfs": ["KODEX 반도체"],
            "signals": [{"indicator": "momentum_20d", "operator": ">",
                         "threshold": 0, "action": "buy"}],
            "rebalance": "monthly",
            "max_loss_pct": 5,
            "risk_profile": "neutral",
            "snapshot_date": "2026-08-28",
            "rationale": "테스트용.",
        }
        return {**base, **over}

    # ── ① 정상 Spec: 조정 없음. clamps 는 빈 배열이어야 한다.
    out, clamps = enforce_hardcaps(spec_of(), SHIPPED)
    assert clamps == [], clamps
    assert out == spec_of(), "조정이 없으면 Spec 이 그대로여야 한다"

    # ── ② 설문 선택지(3/5/10)는 전부 클램프 없이 통과해야 한다.
    #      하나라도 걸리면 정상 사용자가 매번 조정을 보게 되므로 캡 값이 잘못된 것.
    for choice in (3, 5, 10):
        _, c = enforce_hardcaps(spec_of(max_loss_pct=choice), SHIPPED)
        assert c == [], f"설문 선택지 {choice}% 가 클램프됨: {c}"

    # ── ③ 경계값 20 은 통과(<=), 20.1 부터 클램프.
    assert enforce_hardcaps(spec_of(max_loss_pct=20), SHIPPED)[1] == []
    assert enforce_hardcaps(spec_of(max_loss_pct=20.1), SHIPPED)[1] != []

    # ── ④ 적대적 입력 25% → 20 으로 클램프. 요청은 실패가 아니다(예외가 안 난다).
    out, clamps = enforce_hardcaps(spec_of(max_loss_pct=25), SHIPPED)
    assert out["max_loss_pct"] == 20, out
    assert len(clamps) == 1, clamps
    rec = clamps[0]
    assert rec["field"] == "max_loss_pct"      # 어떤 필드가
    assert rec["requested"] == 25              # 어떤 값에서
    assert rec["applied"] == 20                # 어떤 값으로
    assert rec["cap"] == "max_loss_pct_cap"    # 어떤 캡 때문에
    assert "20% 초과" in rec["reason"], rec

    # 더 극단적인 입력도 같은 상한으로 수렴한다 (차단율 측정 대상)
    for adversarial in (30, 50, 100):
        assert enforce_hardcaps(spec_of(max_loss_pct=adversarial), SHIPPED)[0]["max_loss_pct"] == 20

    # ── ⑤ 캡 값이 코드가 아니라 프로파일에서 온다는 증명.
    #      같은 Spec 인데 프로파일만 바꾸면 결과가 따라 바뀐다.
    v2 = {**SHIPPED, "version": 2, "max_loss_pct_cap": 15}
    out, clamps = enforce_hardcaps(spec_of(max_loss_pct=25), v2)
    assert out["max_loss_pct"] == 15, out
    assert clamps[0]["limit"] == 15, clamps
    # 상한을 25 이상으로 올리면 같은 입력이 아예 안 걸린다
    assert enforce_hardcaps(spec_of(max_loss_pct=25), {**SHIPPED, "max_loss_pct_cap": 50})[1] == []

    # ── ⑥ 하한은 없다. 0% 도 통과 (보수적인 선택은 막을 이유가 없다).
    assert enforce_hardcaps(spec_of(max_loss_pct=0), SHIPPED)[1] == []

    # ── ⑦ 최소 리밸런싱 간격: 현행 프로파일(7일)로는 발동하지 않는다.
    #      "위반이 없다"가 아니라 "판정이 성립하지 않는다"로 보고돼야 한다.
    v = check_min_rebalance_interval(spec_of(rebalance="weekly"), SHIPPED)
    assert v["status"] == STATUS_UNDECIDABLE, v
    assert enforce_hardcaps(spec_of(rebalance="weekly"), SHIPPED)[1] == []

    #      스텁이 아니라 살아 있는 로직이라는 확인 — 캡을 14일로 올리면 즉시 발동한다.
    #      (향후 enum 에 daily 가 추가돼도 같은 경로로 걸린다)
    strict = {**SHIPPED, "version": 3, "min_rebalance_days": 14}
    out, clamps = enforce_hardcaps(spec_of(rebalance="weekly"), strict)
    assert out["rebalance"] == "monthly", out       # 가장 가까운 허용값으로
    assert clamps[0]["field"] == "rebalance", clamps
    assert clamps[0]["requested"] == "weekly" and clamps[0]["applied"] == "monthly"
    # 이미 캡을 만족하는 주기는 안 건드린다
    assert enforce_hardcaps(spec_of(rebalance="quarterly"), strict)[1] == []
    # 캡을 만족하는 주기가 아예 없으면 조용히 통과시키지 않고 판정 불가로 올린다
    absurd = check_min_rebalance_interval(spec_of(rebalance="weekly"),
                                          {**SHIPPED, "min_rebalance_days": 365})
    assert absurd["status"] == STATUS_UNDECIDABLE, absurd

    # ── ⑧ 클램프가 둘 동시에 걸리는 경우 (기록이 2건 쌓인다)
    out, clamps = enforce_hardcaps(spec_of(max_loss_pct=25, rebalance="weekly"), strict)
    assert {c["field"] for c in clamps} == {"max_loss_pct", "rebalance"}, clamps

    # ── ⑨ 스텁 3종: hardcap_profile 에 값은 있는데 판정은 불가.
    #      **조용히 ok 를 돌려주면 캡이 작동 중인 것처럼 보인다 — 그게 제일 위험하다.**
    report = hardcap_report(spec_of(), SHIPPED)
    by_cap = {r["cap"]: r for r in report}
    for cap in ("mdd_pct_cap", "min_rebalance_days", "single_etf_weight_cap"):
        assert by_cap[cap]["status"] == STATUS_UNDECIDABLE, by_cap[cap]
        assert by_cap[cap]["limit"] > 0, "값 자체는 프로파일에 존재해야 한다"
        assert by_cap[cap]["reason"], "판정 불가 사유가 반드시 있어야 한다"
    # 실제로 작동하는 캡은 하나뿐이라는 것도 못박아 둔다
    assert by_cap["max_loss_pct_cap"]["status"] == STATUS_OK

    # 단일종목 상한의 사유는 '비중 필드 부재'이지 'ETF 라서 불필요'가 아니다.
    single = by_cap["single_etf_weight_cap"]["reason"]
    assert "비중 필드" in single, single
    assert "불필요" not in single, single

    # 1회 손실 한도 < MDD 상한 관계 (mdd 값이 존재하는 유일한 근거)
    assert SHIPPED["max_loss_pct_cap"] < SHIPPED["mdd_pct_cap"]

    # /health 용 요약도 같은 3종을 집어낸다
    assert {c["cap"] for c in undecidable_caps(SHIPPED)} == {
        "mdd_pct_cap", "min_rebalance_days", "single_etf_weight_cap"}

    # ── ⑩ 구조적 위반 → 반려(ValueError). 클램프로 살리지 않는다.
    for bad, why in [
        (spec_of(signals=[]), "매매 조건 없음"),
        (spec_of(signals=[
            {"indicator": "momentum_20d", "operator": ">", "threshold": 0, "action": "buy"},
            {"indicator": "momentum_20d", "operator": ">", "threshold": 0, "action": "sell"},
        ]), "동일 조건에 buy/sell 동시"),
        (spec_of(risk_profile="conservative", etfs=["KODEX 레버리지"]),
         "안정형인데 레버리지 (현행 유니버스로는 도달 불가한 경로)"),
    ]:
        try:
            enforce_hardcaps(bad, SHIPPED)
        except ValueError:
            pass
        else:
            raise AssertionError(f"반려됐어야 함: {why}")

    # 같은 indicator 에 buy/sell 이 함께 있는 것 자체는 정상이다 (조건이 다르므로).
    # 이걸 모순으로 잡으면 평범한 추세추종 전략이 전부 반려된다.
    assert enforce_hardcaps(spec_of(signals=[
        {"indicator": "momentum_20d", "operator": ">", "threshold": 0, "action": "buy"},
        {"indicator": "momentum_20d", "operator": "<", "threshold": 0, "action": "sell"},
    ]), SHIPPED)[1] == []

    # ── ⑪ 클램프된 결과가 여전히 스키마를 만족하는지 (1계층으로 되돌려 확인)
    from app.schemas import StrategySpec
    clamped_spec, _ = enforce_hardcaps(spec_of(max_loss_pct=99), SHIPPED)
    assert StrategySpec.model_validate(clamped_spec).max_loss_pct == 20

    # ── ⑫ 원본 Spec 을 변형하지 않는다 (호출부가 before/after 를 비교할 수 있어야 한다)
    original = spec_of(max_loss_pct=25)
    enforce_hardcaps(original, SHIPPED)
    assert original["max_loss_pct"] == 25, "입력 dict 가 제자리에서 수정됐다"

    print("ok — 하드캡 계층 검증 통과 "
          f"(클램프 발동 1종 / 판정 불가 {len(undecidable_caps(SHIPPED))}종)")
