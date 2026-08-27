"""설문 응답 → LLM 프롬프트 문자열 조립.

프롬프트는 두 덩어리로 나뉜다:

    system : 매 요청마다 동일한 '규칙'. 유니버스 목록이 여기 들어간다.
    user   : 이번 사용자의 설문 응답.

유니버스를 프롬프트에 통째로 넣는 이유:
    모델은 KRX 상장 ETF 목록을 정확히 외우고 있지 않다. 그냥 물어보면
    그럴듯한 가짜 종목명을 지어낸다(환각). 고를 수 있는 목록을 직접 보여주면
    "생성"이 아니라 "선택" 문제로 바뀌어 환각이 크게 줄어든다.
    (그래도 100% 는 아니라서 validators.py 가 뒤를 받친다)
"""

from datetime import date

# {today}, {universe_lines} 는 build_system() 에서 .format() 으로 채워진다.
SYSTEM_TEMPLATE = """너는 '전략 컴파일러'다. 사용자의 설문 응답을 StrategySpec JSON으로 변환한다.

규칙:
- etfs는 <UNIVERSE>에 나열된 종목명 중에서만, 표기 그대로 선택한다. 목록에 없는 종목 생성 금지.
- 레버리지/인버스 종목은 절대 선택 금지.
- 관심 섹터에 해당하는 종목을 1~3개 고른다.
- signals는 매매 스타일을 구체적 지표로 변환한다.
  추세추종 → momentum_20d, 역추세 → rsi_14, 이평선 교차 → ma_cross_5_20
- threshold는 투자 성향에 맞춰 판단한다 (공격형일수록 진입 문턱을 낮게).
- snapshot_date는 오늘({today}).
- version은 1.
- rationale은 한국어 1~2문장.
- 스키마에 맞는 JSON만 출력. 설명·마크다운 금지.

<UNIVERSE>
{universe_lines}
</UNIVERSE>
/no_think"""
# ↑ <UNIVERSE> 같은 태그로 감싸는 건 관례다. 데이터의 시작/끝이 명확해져
#   모델이 규칙 문장과 목록을 헷갈리지 않는다.
#
# ↑ 마지막 줄 /no_think 는 qwen3 전용 스위치로 사고 과정(<think> 블록) 출력을 끈다.
#   사실 format 문법 제약이 첫 토큰부터 JSON 을 강제해서 어차피 <think> 는 못 나오지만,
#   ollama 라이브러리 0.4.5 에 think 파라미터가 없어 프롬프트로 넣어뒀다.
#   다른 모델에서는 그냥 무시되는 문자열이라 해가 없다.

USER_TEMPLATE = """[설문 응답]
- 관심 섹터/테마: {sector}
- 투자 성향: {risk}
- 감내 가능 손실: {max_loss}%
- 매매 스타일: {style}
- 리밸런싱 주기: {rebalance}
- 자유 서술: {note}"""


def build_system(universe: list[dict], today: date | None = None) -> str:
    """시스템 프롬프트 완성. 유니버스를 '- 종목명 (테마)' 줄 목록으로 펼쳐 넣는다."""
    lines = "\n".join(f"- {it['name']} ({it['theme']})" for it in universe)
    # today 를 인자로 받는 이유: 테스트에서 날짜를 고정할 수 있게 하려고.
    # None 이면 실제 오늘 날짜를 쓴다.
    return SYSTEM_TEMPLATE.format(today=today or date.today(), universe_lines=lines)


def build_user(survey: dict, retry_reason: str | None = None) -> str:
    """사용자 프롬프트 완성.

    이 함수의 결과물은 두 군데서 쓰인다:
      1) LLM 에 보내는 user 메시지
      2) DB requests.nl_text 에 저장하는 '원문 자연어'
    (별도 변환 코드를 만들지 않으려고 main.py 에서 재사용한다)
    """
    # note 가 빈 문자열이면 "자유 서술: " 로 끝나 어색하므로 "(없음)" 으로 채운다.
    prompt = USER_TEMPLATE.format(**{**survey, "note": survey.get("note") or "(없음)"})

    # 재시도일 때만 실패 사유를 덧붙인다 (llm.py 의 2번째 시도).
    # 모델에게 "뭘 틀렸는지" 알려줘야 같은 실수를 반복하지 않는다.
    if retry_reason:
        prompt += f"\n\n[직전 시도 실패 사유 — 반드시 수정할 것]\n{retry_reason}"
    return prompt
