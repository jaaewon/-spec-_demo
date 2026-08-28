"""FastAPI 엔드포인트 — 이 파일이 시스템의 입구.

전체 흐름 (POST /compile 기준):
    브라우저 설문
      → SurveyRequest 로 입력 검증        (schemas.py)
      → 프롬프트 문자열 조립               (prompt.py)
      → requests 테이블에 원문 저장        (db.py)
      → Ollama 호출 + Spec 검증            (llm.py → validators.py)
      → specs 테이블에 결과 저장           (db.py)
      → Spec JSON 응답
"""

import os
from functools import lru_cache

import ollama
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from app.db import get_spec, list_specs, save_request, save_spec
from app.llm import compile_spec
from app.prompt import build_user
from app.schemas import StrategySpec, SurveyRequest

# 설정은 전부 환경변수로 (docker-compose.yml 의 environment 참고).
# os.environ[...] 은 없으면 즉시 KeyError → 설정 누락을 기동 시점에 바로 알 수 있다.
# (os.getenv 를 쓰면 None 인 채로 굴러가다 한참 뒤에 터진다)
DATABASE_URL = os.environ["DATABASE_URL"]
OLLAMA_HOST = os.environ["OLLAMA_HOST"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]

app = FastAPI(title="Strategy Spec Compiler (demo)")


@app.get("/")
def index():
    """설문 HTML 서빙. 빌드 과정 없는 단일 정적 파일이라 그냥 파일로 내려준다."""
    return FileResponse("static/index.html")


@app.post("/compile")
def compile_(survey: SurveyRequest):
    """설문 → LLM → 검증 → 저장 → Spec 반환.

    인자 타입을 SurveyRequest 로 선언하면 FastAPI 가 요청 본문을 자동으로
    검증한다. 검증 실패 시 이 함수는 아예 호출되지 않고 422 가 나간다.
    → 잘못된 입력이 LLM 까지 도달하지 못하는 1차 방어선.
    """
    # enum 을 그대로 두면 json 직렬화가 안 되므로 mode="json" 으로 원시 값(문자열)으로 변환
    payload = survey.model_dump(mode="json")

    # 프롬프트에 쓰는 문자열을 그대로 '원문 자연어'로 저장한다.
    # 별도 변환 코드를 만들지 않기 위해 build_user 를 재사용.
    nl_text = build_user(payload)

    # LLM 호출보다 먼저 저장한다: 실패한 요청도 기록이 남아야
    # "어떤 입력이 거부됐는지" 시연에서 보여줄 수 있다.
    # (specs 행이 없는 requests 행 = 실패한 시도)
    request_id = save_request(payload, nl_text)

    try:
        spec = compile_spec(payload)
    except ValueError as e:
        # 유니버스 밖 종목 / 레버리지 / 스키마 위반 → 사용자 입력 탓이므로 400
        raise HTTPException(status_code=400, detail=f"Spec 검증 실패: {e}")
    except Exception as e:
        # Ollama 미기동, 타임아웃 등 → 서버 사정이므로 503
        raise HTTPException(status_code=503, detail=f"LLM 호출 실패: {e}")

    spec_json = spec.model_dump(mode="json")
    spec_id = save_spec(request_id, spec_json, OLLAMA_MODEL)  # 어떤 모델이 만들었는지도 함께 박제
    # spec_id 를 돌려주는 이유: 프론트가 바로 POST /backtest/{spec_id} 를 호출할 수 있어야 한다.
    # (기존 키는 그대로 두므로 이 응답을 쓰던 쪽은 영향 없다)
    return {"request_id": request_id, "spec_id": spec_id, "spec": spec_json}


@app.get("/specs")
def specs(limit: int = 20):
    """저장 이력 조회 (원문 설문 + 생성된 Spec).

    min(limit, 100) — limit 은 URL 쿼리스트링이라 사용자가 조작할 수 있다.
    limit=999999 로 DB 를 통째로 긁어가지 못하게 상한을 건다.
    """
    return list_specs(min(limit, 100))


@app.post("/backtest/{spec_id}")
def backtest_(spec_id: int, years: int | None = None):
    """저장된 Spec 을 vectorbt 로 백테스트하고 리포트를 반환.

    초기 자본 1천만원, 기본 구간은 오늘로부터 5년 (app/backtest.py 의 상수).
    한 번에 수 초~수십 초 걸린다 (pykrx 시세 조회 + 최초 호출 시 numba 컴파일).
    """
    row = get_spec(spec_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Spec 없음: id={spec_id}")

    # 지연 임포트: vectorbt/pandas 는 무겁다. 백테스트를 안 쓰는 사람이 /compile 만
    # 쓸 때 앱 기동이 느려지지 않도록 여기서 들여온다.
    from app.backtest import DEFAULT_YEARS, format_report, run_backtest

    spec = StrategySpec.model_validate(row["spec"])
    try:
        # years 는 쿼리스트링이라 사용자가 조작할 수 있다. 상한을 걸어
        # years=9999 로 KRX 를 통째로 긁는 요청을 막는다.
        result = run_backtest(spec, years=min(years or DEFAULT_YEARS, 10))
    except ValueError as e:
        # 티커 매핑 누락 / 지원하지 않는 지표 → Spec 쪽 문제
        raise HTTPException(status_code=400, detail=f"백테스트 불가: {e}")
    except Exception as e:
        # pykrx 조회 실패 등 외부 사정
        raise HTTPException(status_code=503, detail=f"시세 조회 실패: {e}")

    return {
        "spec_id": spec_id,
        "spec": row["spec"],
        "metrics": result.metrics(),
        "report": format_report(result),   # 사람이 읽는 텍스트 리포트
        "chart": result.chart,             # plotly figure (프론트가 Plotly.newPlot 으로 그린다)
    }


@lru_cache(maxsize=1)
def _plotly_js() -> str:
    """plotly.js 번들 원문 (약 4.5MB). 매 요청마다 파일을 읽지 않도록 캐시한다."""
    from plotly.offline import get_plotlyjs

    return get_plotlyjs()


@app.get("/plotly.js")
def plotly_js():
    """차트 라이브러리를 직접 서빙한다.

    CDN 을 쓰지 않는 이유: 네트워크가 막힌 곳에서 시연하면 차트만 빈 칸이 된다.
    plotly 는 vectorbt 의 의존성이라 컨테이너 안에 이미 들어 있으므로 그대로 내보낸다.
    immutable 캐시라 브라우저는 최초 1회만 받는다.
    """
    return Response(
        _plotly_js(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/health")
def health():
    """DB·Ollama 가 모두 살아 있는지 한 번에 확인. 기동 직후 제일 먼저 찔러볼 곳."""
    return {"db": _db_status(), "ollama": _ollama_status(), "model": OLLAMA_MODEL}


def _db_status() -> str:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            # 단순 연결만 보지 않고 specs 테이블 존재까지 확인한다.
            # LIMIT 0 이라 행은 안 읽고 테이블 유무만 검사 (schema.sql 이 돌았는지 확인)
            conn.execute("SELECT 1 FROM specs LIMIT 0")
        return "ok"
    except Exception as e:
        # 헬스체크는 죽으면 안 되므로 예외를 문자열로 바꿔 그대로 노출한다.
        # 원인이 응답에 찍혀 있어야 팀원이 로그를 안 뒤져도 된다.
        return f"error: {e}"


def _ollama_status() -> str:
    try:
        tags = ollama.Client(host=OLLAMA_HOST, timeout=5).list()
        names = {m.model for m in tags.models}
        # 서버가 떠 있어도 모델을 안 받았으면 /compile 이 터진다. 여기서 미리 구분해 준다.
        if OLLAMA_MODEL not in names:
            return f"error: 모델 미설치 ({OLLAMA_MODEL}). `ollama pull {OLLAMA_MODEL}` 필요"
        return "ok"
    except Exception as e:
        return f"error: {e}"
