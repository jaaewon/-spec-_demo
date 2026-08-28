"""FastAPI 엔드포인트 — 이 파일이 시스템의 입구.

전체 흐름 (POST /compile 기준):
    브라우저 설문
      → SurveyRequest 로 입력 검증        (schemas.py)
      → 프롬프트 문자열 조립               (prompt.py)
      → requests 테이블에 원문 저장        (db.py)
      → Ollama 호출 + 스키마·참조 검증      (llm.py → validators.py)  ← 1~2계층, 재시도 있음
      → 하드캡 적용: 클램프 or 반려         (validators.py)           ← 4계층, 재시도 없음
      → snapshot_date 기준 경제지표 as-of 조회 (indicators.py) — 실패해도 무시
      → specs 테이블에 결과 저장           (db.py)
      → Spec JSON + 조정 내역 응답
"""

import os
from contextlib import asynccontextmanager
from datetime import date

import ollama
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.db import (hardcap_status, list_specs, load_active_hardcap_profile,
                    save_request, save_spec, seed_hardcap_profile)
from app.indicators import get_indicators_as_of, indicators_status, seed_indicators
from app.llm import compile_spec
from app.prompt import build_user
from app.schemas import StrategySpec, SurveyRequest
from app.validators import enforce_hardcaps

# 설정은 전부 환경변수로 (docker-compose.yml 의 environment 참고).
# os.environ[...] 은 없으면 즉시 KeyError → 설정 누락을 기동 시점에 바로 알 수 있다.
# (os.getenv 를 쓰면 None 인 채로 굴러가다 한참 뒤에 터진다)
DATABASE_URL = os.environ["DATABASE_URL"]
OLLAMA_HOST = os.environ["OLLAMA_HOST"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    """기동 시 경제지표 seed 와 하드캡 프로파일을 1회 적재한다.

    별도 마이그레이션 도구를 안 쓰는 이유: 테이블 생성은 db/schema.sql 이
    (볼륨 최초 생성 시) 맡고, 데이터 적재만 여기서 한다. 두 seed 함수 모두
    멱등이라 --reload 로 몇 번을 다시 떠도 중복이 쌓이지 않는다.

    적재 실패는 둘 다 삼킨다. 다만 이유가 다르다:
      - 지표: 없어도 /compile 이 정상 동작한다 (부가정보).
      - 하드캡: 없으면 /compile 이 503 이다. 그래도 여기서 예외를 터뜨려 서버 기동을
        막지는 않는다 — 그러면 원인이 컨테이너 로그에만 남고 /health 로 확인할 수가 없다.
        기동은 시키고 상태를 /health 에 드러내는 편이 진단이 빠르다.
    """
    try:
        print(f"[startup] 지표 seed 적재: {seed_indicators()}")
    except Exception as e:
        print(f"[startup] 지표 seed 적재 실패 (무시하고 계속): {e}")
    try:
        print(f"[startup] 하드캡 프로파일 적재: {seed_hardcap_profile()}")
    except Exception as e:
        print(f"[startup] 하드캡 프로파일 적재 실패 (/compile 이 503 이 된다): {e}")
    yield


app = FastAPI(title="Strategy Spec Compiler (demo)", lifespan=lifespan)


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

    # ── 1~2계층: 스키마 + 참조(유니버스/레버리지). 실패 시 llm.py 안에서 1회 재시도한다.
    try:
        spec = compile_spec(payload)
    except ValueError as e:
        # 유니버스 밖 종목 / 레버리지 / 스키마 위반 → 사용자 입력 탓이므로 400
        raise HTTPException(status_code=400, detail=f"Spec 검증 실패: {e}")
    except Exception as e:
        # Ollama 미기동, 타임아웃 등 → 서버 사정이므로 503
        raise HTTPException(status_code=503, detail=f"LLM 호출 실패: {e}")

    # ── 4계층: 하드캡 (CLAUDE.md §18).
    #
    # 반드시 compile_spec **밖**에서 한다. llm.py 의 재시도는 실패 사유를 프롬프트에
    # 덧붙이는 방식이라, 하드캡 위반을 그 경로로 흘리면 상한값이 LLM 에게 새어 나간다.
    # 그러면 모델이 경계에 맞춰 생성해 클램프가 발생하지 않고, 나중에 측정할
    # "적대적 입력에 대한 하드캡 차단율"이 무의미해진다. LLM 은 하드캡을 모르는 채로
    # 만들고, 서버가 사후에 깎는다.
    try:
        profile = load_active_hardcap_profile()
    except Exception as e:
        # 지표(_as_of_snapshot)와 달리 여기서 조용히 넘어가지 않는다.
        # 하드캡은 부가정보가 아니라 안전 계층이라, 없는 채로 Spec 을 내보내는 게
        # 에러보다 나쁘다 (fail closed).
        raise HTTPException(
            status_code=503,
            detail=f"하드캡 프로파일을 읽을 수 없어 Spec 을 낼 수 없습니다: {e}")

    try:
        spec_json, clamps = enforce_hardcaps(spec.model_dump(mode="json"), profile)
    except ValueError as e:
        # 구조적 위반만 여기로 온다. 수치 초과는 예외가 아니라 clamps 로 나간다.
        raise HTTPException(status_code=400, detail=f"하드캡 구조적 위반: {e}")

    # 클램프한 결과를 1계층으로 되돌려 확인한다. 조정 로직이 스키마를 깨뜨리면
    # (예: 캡이 음수로 잘못 들어가 max_loss_pct 가 ge=0 을 위반) 저장 전에 잡힌다.
    spec = StrategySpec.model_validate(spec_json)
    spec_json = spec.model_dump(mode="json")

    # Spec 의 snapshot_date 를 그대로 as-of 키로 써서 "그 시점에 보였던 지표"를 뜬다.
    # 이 값은 프롬프트에 들어가지 않는다 — LLM 출력에 영향을 주지 않으므로
    # 기존 시연 시나리오(§15)의 재현성이 그대로 유지된다. 기록·조회 계층일 뿐이다.
    indicators = _as_of_snapshot(spec.snapshot_date)

    # 모델·지표 스냅샷·조정 내역·정책 버전을 함께 박제
    save_spec(request_id, spec_json, OLLAMA_MODEL, indicators, clamps, profile["version"])
    return {"request_id": request_id, "spec": spec_json,
            "indicators": indicators, "clamps": clamps}


def _as_of_snapshot(as_of: date) -> dict:
    """지표 조회. **어떤 이유로 실패하든 {} 를 돌려준다.**

    지표 테이블이 비어 있거나(아직 seed 전), 아예 없거나(구 볼륨), DB 가 흔들려도
    /compile 은 200 이어야 한다. 지표는 Spec 생성의 의존성이 아니라 부가정보다.
    """
    try:
        return get_indicators_as_of(as_of)
    except Exception as e:
        print(f"[compile] 지표 조회 실패 (무시): {e}")
        return {}


@app.get("/specs")
def specs(limit: int = 20):
    """저장 이력 조회 (원문 설문 + 생성된 Spec).

    min(limit, 100) — limit 은 URL 쿼리스트링이라 사용자가 조작할 수 있다.
    limit=999999 로 DB 를 통째로 긁어가지 못하게 상한을 건다.
    """
    return list_specs(min(limit, 100))


@app.get("/indicators")
def indicators(as_of: date | None = None):
    """as_of 시점에 **공개돼 있던** 최신 지표들. as_of 생략 시 오늘.

    타입을 date 로 선언했으므로 FastAPI 가 YYYY-MM-DD 파싱까지 해준다
    (형식이 틀리면 이 함수는 호출되지 않고 422). 미래 날짜는 막지 않는다 —
    seed 의 미래 관측치를 미리 볼 수 있는 게 아니라, 그냥 전부 공개된 상태로 보일 뿐이라
    as-of 규칙 자체는 깨지지 않는다.
    """
    as_of = as_of or date.today()
    return {"as_of": as_of.isoformat(), "indicators": get_indicators_as_of(as_of)}


@app.get("/health")
def health():
    """DB·Ollama 가 모두 살아 있는지 한 번에 확인. 기동 직후 제일 먼저 찔러볼 곳."""
    return {
        "db": _db_status(),
        "ollama": _ollama_status(),
        "model": OLLAMA_MODEL,
        # 지표는 "없어도 되는" 계층이라 error 대신 empty 라는 상태가 따로 있다.
        # empty 여도 /compile 은 정상 동작한다.
        "indicators": indicators_status(),
        # 하드캡은 반대로 '없어도 되는' 상태가 없다 — 못 읽으면 /compile 이 503.
        # 활성 버전과 캡 값이 그대로 찍히므로 "지금 어떤 정책이 걸려 있나"를 여기서 본다.
        "hardcap": hardcap_status(),
    }


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
