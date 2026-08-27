"""Ollama 호출 → Pydantic 검증. 이 데모의 심장부.

핵심은 chat() 의 `format=` 인자다:

    format=StrategySpec.model_json_schema()

이 한 줄이 JSON Schema 를 Ollama 에 넘기고, Ollama 는 그걸 문법(grammar)으로
변환해 토큰 생성 단계에서 강제한다. 즉 모델은 스키마에 어긋나는 출력을
"안 하는" 게 아니라 "못 한다". 프롬프트로 부탁하는 방식과 결정적으로 다르며,
JSON 파싱 실패 / 마크다운 코드펜스 / 앞뒤 잡담 같은 문제가 구조적으로 사라진다.

그래서 여기서 실패할 수 있는 건 '형식'이 아니라 '내용'이다:
없는 종목을 고르는 경우 → validators.py 가 잡고 → 1회 재시도.
"""

import os
from datetime import date

import ollama
from pydantic import ValidationError

from app.prompt import build_system, build_user
from app.schemas import StrategySpec
from app.validators import load_universe, validate_etfs

OLLAMA_HOST = os.environ["OLLAMA_HOST"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]

# 모듈이 import 될 때 딱 한 번 실행된다 (요청마다가 아니라).
# 유니버스는 정적 파일이라 매번 디스크를 읽을 이유가 없다.
# 단, 파일을 고치면 서버를 재시작해야 반영된다 (--reload 가 알아서 해준다).
_UNIVERSE = load_universe()
_NAMES = {it["name"] for it in _UNIVERSE}  # 검증용 집합 — 리스트보다 조회가 빠르다


def compile_spec(survey: dict, today: date | None = None) -> StrategySpec:
    """설문 → Spec. 검증 실패 시 사유를 붙여 1회만 재시도한다 (무한 루프 금지).

    재시도를 1회로 못 박은 이유:
        로컬 8B 모델은 한 번 호출에 30~60초 걸린다. 무한 재시도는
        사용자를 몇 분씩 기다리게 하고, 모델이 못 고치는 요청이면 영원히 안 끝난다.
    """
    # timeout=180 : 8B 모델 + 긴 프롬프트면 1분을 넘길 수 있다. 넉넉하게 잡는다.
    client = ollama.Client(host=OLLAMA_HOST, timeout=180)
    system = build_system(_UNIVERSE, today)
    reason = None  # 1차 시도에서는 None, 실패하면 사유가 담겨 2차 프롬프트에 붙는다

    for attempt in (1, 2):
        resp = client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": build_user(survey, reason)},
            ],
            format=StrategySpec.model_json_schema(),  # ← 스키마를 문법으로 강제
            options={"temperature": 0},  # 창의성 0 = 같은 입력에 같은 출력을 지향
        )
        try:
            # 1단계: 형식 검증 (사실상 항상 통과 — 문법으로 강제됐으므로)
            spec = StrategySpec.model_validate_json(resp["message"]["content"])
            # 2단계: 내용 검증 (여기서 떨어질 수 있다)
            validate_etfs(spec.etfs, _NAMES)
            return spec
        except (ValidationError, ValueError) as e:
            reason = str(e)
            if attempt == 2:
                # 2번 다 실패 → 포기하고 호출자(main.py)에게 넘긴다 → HTTP 400
                raise ValueError(f"2회 시도 모두 검증 실패: {reason}") from e

    # for 문은 위에서 반드시 return 이나 raise 로 끝나므로 여기 도달할 수 없다.
    # 혹시 루프 범위를 고치다 실수하면 조용히 None 을 반환하는 대신 여기서 터지게 둔다.
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# 셀프체크: `docker compose exec api python -m app.llm`
# 실제 Ollama 를 호출하므로 1~2분 걸린다.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    survey = {
        "sector": "반도체",
        "risk": "aggressive",
        "max_loss": 10,
        "style": "추세추종(모멘텀)",
        "rebalance": "monthly",
        "note": "",
    }
    spec = compile_spec(survey)
    print(spec.model_dump_json(indent=2))

    assert set(spec.etfs) <= _NAMES, spec.etfs  # <= 는 부분집합 검사
    assert spec.version == 1
    assert spec.snapshot_date == date.today()

    # temperature=0 재현성: 2회 호출 모두 유효해야 한다 (완전 동일까지는 요구하지 않음).
    # temperature=0 이어도 GPU 연산 순서 등 때문에 결과가 완전히 같다는 보장은 없다.
    again = compile_spec(survey)
    assert set(again.etfs) <= _NAMES, again.etfs
    print(f"\nok — 2회 모두 유효. etfs={spec.etfs} / {again.etfs}")
