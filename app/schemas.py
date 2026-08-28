"""데이터 형태 정의 (Pydantic). 이 프로젝트의 핵심 아이디어가 담긴 파일.

이 파일에는 방향이 반대인 두 종류의 모델이 있다:

  1) StrategySpec  = LLM 의 **출력** 형태
     model_json_schema() 로 JSON Schema 를 뽑아 Ollama 의 `format` 인자로 넘긴다.
     그러면 Ollama 가 이 스키마를 '문법(grammar)'으로 바꿔 토큰 생성 자체를 제약하므로,
     모델은 스키마에서 벗어난 문자열을 **만들어낼 수가 없다.**
     (프롬프트로 "JSON 으로만 답해"라고 부탁하는 것과 근본적으로 다른 수준의 강제)

  2) SurveyRequest / FreeInputRequest = 사용자의 **입력** 형태
     POST /compile, POST /compile/free 의 요청 본문을 검증한다.
     외부 입력이 들어오는 신뢰 경계.

     둘의 신뢰 경계 두께가 다르다는 점이 중요하다:
       SurveyRequest    — Enum 으로 선택지를 좁혀 막는다 (note 만 자유 텍스트).
       FreeInputRequest — 본문 전체가 자유 텍스트라 Enum 으로 막을 게 없다.
                          방어가 길이 상한과 프롬프트 격리(prompt.py)로 옮겨간다.

Enum 을 적극적으로 쓰는 이유:
  위 1) 의 문법 제약은 Enum 을 "이 값들 중 하나"로 번역한다.
  operator 를 str 로 두면 모델이 "greater_than" 같은 걸 뱉을 수 있지만,
  Enum 으로 두면 ">", "<", ">=", "<=", "==" 외에는 생성이 불가능해진다.
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# StrategySpec 을 구성하는 값 목록들.
# str 을 함께 상속시키면 JSON 직렬화 시 자동으로 문자열이 된다 (RebalanceFreq.weekly -> "weekly").
# --------------------------------------------------------------------------


# 리밸런싱 주기.
class RebalanceFreq(str, Enum):
    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"


# 투자 성향. 설문 입력(SurveyRequest)과 Spec 출력 양쪽에서 재사용한다.
class RiskProfile(str, Enum):
    conservative = "conservative"
    neutral = "neutral"
    aggressive = "aggressive"


# 비교 연산자. CLAUDE.md 원안은 str 이었으나 Enum 으로 승격했다.
# 문법 제약이 걸리므로 모델이 여기 없는 기호는 생성 자체를 못 한다.
class Operator(str, Enum):
    gt = ">"
    lt = "<"
    gte = ">="
    lte = "<="
    eq = "=="


# 매매 방향. 위와 같은 이유로 Enum.
class Action(str, Enum):
    buy = "buy"
    sell = "sell"


# 매매 규칙 한 줄. "momentum_20d > 0 이면 buy" 를 구조화한 것.
class SignalRule(BaseModel):
    # indicator 만 str 로 남겼다. 지표 종류는 앞으로 늘어날 값이라
    # Enum 으로 고정하면 확장할 때마다 코드를 고쳐야 한다.
    # description 은 JSON Schema 에 그대로 실려 모델에게 힌트로 전달된다.
    indicator: str = Field(description="예: momentum_20d, rsi_14, ma_cross_5_20")
    operator: Operator
    threshold: float
    action: Action


# LLM 이 만들어내야 할 최종 산출물. 이 클래스 = 출력 계약.
# 필드를 추가/삭제하면 프롬프트를 안 고쳐도 모델 출력이 따라 바뀐다 (스키마가 곧 문법이므로).
#
# 주의: 이 클래스와 그 하위 모델의 **docstring 은 JSON Schema 의 description 으로 실려
# 그대로 LLM 에게 전송된다.** 코드 설명용 메모는 docstring 이 아니라 # 주석으로 쓸 것.
# 반대로 모델에게 알려주고 싶은 힌트는 Field(description=...) 에 넣는다.
class StrategySpec(BaseModel):
    version: int = 1
    etfs: list[str] = Field(description="반드시 제공된 유니버스 내 종목명만")
    signals: list[SignalRule]
    rebalance: RebalanceFreq
    # ge/le = greater-equal / less-equal. 0~100 범위를 벗어나면 검증 실패.
    max_loss_pct: float = Field(ge=0, le=100)
    risk_profile: RiskProfile
    # 어떤 시점 기준으로 만든 Spec 인지 박제 (나중에 재현·감사할 때 필요)
    snapshot_date: date
    rationale: str = Field(description="선정 근거 1~2문장 (한국어)")

    # 여기서 ETF 이름까지 검증하지 않는 이유:
    # 유니버스 목록은 data/etf_universe.json 에서 읽는 '데이터'라 스키마에 박을 수 없다.
    # 그래서 종목 검증만 app/validators.py 로 분리했다 (CLAUDE.md §8).


# --------------------------------------------------------------------------
# 여기서부터는 방향이 반대 — 사용자 입력(설문) 쪽 모델.
# --------------------------------------------------------------------------


class Sector(str, Enum):
    """설문의 섹터 선택지.

    값이 etf_universe.json 의 "theme" 과 **똑같은 문자열**이어야 한다.
    프롬프트에 "관심 섹터: 반도체" 와 "- KODEX 반도체 (반도체)" 가 같이 들어가서
    모델이 문자열 매칭으로 후보를 좁히기 때문.
    """
    semiconductor = "반도체"
    battery = "2차전지"
    dividend = "배당"
    largecap = "대형주"
    kosdaq = "코스닥"
    us_equity = "미국주식"
    bond = "채권"


class TradeStyle(str, Enum):
    """매매 스타일. 이 값이 prompt.py 의 규칙에 따라 지표로 번역된다.
    (추세추종 → momentum_20d, 역추세 → rsi_14, 이평선 교차 → ma_cross_5_20)"""
    momentum = "추세추종(모멘텀)"
    mean_reversion = "역추세"
    ma_cross = "이평선 교차"


class SurveyRequest(BaseModel):
    """POST /compile 의 요청 본문.

    외부 입력이 들어오는 신뢰 경계라 선택지를 최대한 좁게 잡는다.
    여기서 막히면 LLM 을 아예 호출하지 않으므로 (느린) 낭비도 없다.
    """

    sector: Sector
    risk: RiskProfile        # StrategySpec 의 risk_profile 과 같은 Enum 재사용
    max_loss: float = Field(ge=0, le=100)
    style: TradeStyle
    rebalance: RebalanceFreq  # 이것도 재사용
    # max_length=500 은 단순 편의가 아니라 방어 목적:
    # 이 문자열은 프롬프트에 그대로 삽입되므로, 길이를 안 막으면
    # 긴 지시문을 밀어넣어 시스템 프롬프트를 덮으려는 시도가 가능해진다.
    note: str = Field(default="", max_length=500, description="자유 서술 (선택)")


class FreeInputRequest(BaseModel):
    """POST /compile/free 의 요청 본문 — 자유 입력 한 단락.

    SurveyRequest 와 달리 Enum 이 없다. 그게 이 경로의 존재 이유다:
    "반도체 비중은 줄이되 배당은 유지" 같은 복합 의도를 sector 단수 Enum 으로
    접어 넣으면 상위 기획서가 지목한 '의도 표현력의 병목' 을 입력단에 다시 세우게 된다
    (CLAUDE.md §19.1).

    대신 신뢰 경계가 얇아지므로 방어를 길이와 격리로 옮긴다 — 아래 text 참고.
    """

    # max_length=2000 의 근거 (note 의 500 과 같은 방어 목적, 다른 값):
    #   ① 자유 입력은 한 단락이 정상이라 500 으로는 기능이 성립하지 않는다.
    #   ② 상한이 없으면 긴 지시문을 밀어넣어 시스템 규칙을 밀어내는 시도가 가능해진다.
    #   ③ 로컬 8B 모델은 프롬프트가 길어질수록 품질이 떨어진다 (CLAUDE.md §17.2).
    #      유니버스 13줄이 이미 시스템 프롬프트에 들어가 있다.
    #   2000자 ≈ 한국어 700~1000 토큰. 초과하면 이 모델이 422 로 막는다 (LLM 호출 전).
    #
    # min_length=1 로 빈 문자열도 막는다. 빈 입력은 슬롯이 하나도 없어
    # LLM 이 Spec 을 통째로 지어내게 되는데, 그건 "언급하지 않은 슬롯을 지어내지 마라"
    # 요구사항과 정면으로 어긋난다.
    text: str = Field(min_length=1, max_length=2000,
                      description="투자 의도 자유 서술 (한 단락)")


# --------------------------------------------------------------------------
# 셀프체크: `docker compose exec api python -m app.schemas` 로 실행.
# 테스트 프레임워크 없이 assert 만 쓴다. 스키마를 고쳤을 때 여기가 깨지면
# 의도치 않게 검증이 느슨해진 것.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    schema = StrategySpec.model_json_schema()
    assert schema["properties"]["etfs"]["type"] == "array"

    spec = StrategySpec.model_validate_json(json.dumps({
        "version": 1,
        "etfs": ["KODEX 반도체"],
        "signals": [{"indicator": "momentum_20d", "operator": ">", "threshold": 0, "action": "buy"}],
        "rebalance": "monthly",
        "max_loss_pct": 5,
        "risk_profile": "neutral",
        "snapshot_date": "2026-08-27",
        "rationale": "반도체 강세 국면에서 20일 모멘텀 추세추종.",
    }))
    assert spec.rebalance is RebalanceFreq.monthly

    for bad, why in [
        ({"max_loss_pct": 150}, "max_loss_pct 상한 초과"),
        ({"rebalance": "daily"}, "enum 밖 리밸런싱 주기"),
        ({"signals": [{"indicator": "rsi_14", "operator": "~", "threshold": 30, "action": "buy"}]}, "enum 밖 operator"),
    ]:
        try:
            StrategySpec.model_validate({**spec.model_dump(mode="json"), **bad})
        except Exception:
            pass
        else:
            raise AssertionError(f"거부됐어야 함: {why}")

    survey = SurveyRequest.model_validate(
        {"sector": "반도체", "risk": "neutral", "max_loss": 5,
         "style": "추세추종(모멘텀)", "rebalance": "monthly", "note": ""})
    assert survey.sector is Sector.semiconductor

    for bad, why in [
        ({"sector": "밈주식"}, "유니버스에 없는 섹터"),
        ({"max_loss": -1}, "음수 손실"),
        ({"style": "느낌대로"}, "enum 밖 매매 스타일"),
        ({"note": "x" * 501}, "자유서술 길이 초과"),
    ]:
        try:
            SurveyRequest.model_validate({**survey.model_dump(mode="json"), **bad})
        except Exception:
            pass
        else:
            raise AssertionError(f"거부됐어야 함: {why}")

    free = FreeInputRequest.model_validate({"text": "반도체 ETF를 월 1회 리밸런싱으로."})
    assert free.text

    for bad, why in [
        ({"text": ""}, "빈 자유 입력 — 슬롯이 하나도 없어 Spec 을 통째로 지어내게 된다"),
        ({"text": "가" * 2001}, "자유 입력 길이 상한 초과 (프롬프트 밀어내기 방어)"),
    ]:
        try:
            FreeInputRequest.model_validate(bad)
        except Exception:
            pass
        else:
            raise AssertionError(f"거부됐어야 함: {why}")

    # 경계값은 통과해야 한다 (상한이 '초과부터' 막는지 확인)
    assert FreeInputRequest.model_validate({"text": "가" * 2000}).text

    print("ok — 스키마 검증 통과")
    print(json.dumps(schema, ensure_ascii=False)[:200] + " ...")
