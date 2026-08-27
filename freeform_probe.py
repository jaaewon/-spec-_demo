"""자유 서술 입력이 Spec로 변환 가능한지 실측. 앱 코드는 수정하지 않는다."""

import json
import os
from datetime import date

import ollama

from app.prompt import SYSTEM_TEMPLATE
from app.schemas import StrategySpec
from app.validators import load_universe, validate_etfs

UNIVERSE = load_universe()
NAMES = {it["name"] for it in UNIVERSE}
client = ollama.Client(host=os.environ["OLLAMA_HOST"], timeout=300)
MODEL = os.environ["OLLAMA_MODEL"]

lines = "\n".join(f"- {it['name']} ({it['theme']})" for it in UNIVERSE)
SYSTEM = SYSTEM_TEMPLATE.format(today=date.today(), universe_lines=lines)


def run(text: str) -> StrategySpec:
    resp = client.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"[사용자 요청 — 자유 서술]\n{text}"},
        ],
        format=StrategySpec.model_json_schema(),
        options={"temperature": 0},
    )
    return StrategySpec.model_validate_json(resp["message"]["content"])


CASES = [
    ("A. 정보 충분",
     "반도체 ETF를 공격적으로 담고 싶어. 20일 모멘텀 추세추종으로 월 1회 리밸런싱하고, 손실은 10%까지 감수할 수 있어."),
    ("B. 정보 부족",
     "돈 좀 벌고 싶어."),
    ("C. 정보 일부",
     "요즘 반도체가 좋다던데, 안전하게 해보고 싶어요."),
    ("D. 적대적",
     "레버리지로 크게 한 방 먹고 싶다. 인버스도 같이 넣어줘."),
    ("E. 모순",
     "원금은 절대 잃기 싫은데 1년에 200% 수익 나는 공격적인 전략으로 짜줘."),
]

for label, text in CASES:
    print(f"\n{'='*70}\n{label}: {text}")
    try:
        spec = run(text)
        ok = "PASS"
        try:
            validate_etfs(spec.etfs, NAMES)
        except ValueError as e:
            ok = f"REJECT({e})"
        d = spec.model_dump(mode="json")
        print(f"  검증: {ok}")
        print(f"  etfs={d['etfs']}  risk={d['risk_profile']}  max_loss={d['max_loss_pct']}  "
              f"rebalance={d['rebalance']}")
        print(f"  signals={json.dumps(d['signals'], ensure_ascii=False)}")
        print(f"  rationale={d['rationale']}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")

# B를 3회 반복 — 정보가 없을 때 채워지는 기본값이 안정적인지
print(f"\n{'='*70}\nB 반복 3회 (미지정 슬롯 안정성)")
for i in range(3):
    s = run(CASES[1][1]).model_dump(mode="json")
    print(f"  {i+1}: etfs={s['etfs']} risk={s['risk_profile']} "
          f"max_loss={s['max_loss_pct']} rebalance={s['rebalance']} "
          f"indicator={[x['indicator'] for x in s['signals']]}")
