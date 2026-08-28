# -spec-_demo

## 사전 요구

- Docker Desktop
- [Ollama](https://ollama.com) — **호스트에서 실행** (Mac은 컨테이너에서 GPU를 못 씀)

## 실행

```bash
# 1. Ollama (호스트, 최초 1회 pull ~5.2GB)
ollama serve            # 이미 떠 있으면 생략
ollama pull qwen3:8b

# 2. 앱
docker compose up -d --build
```

브라우저에서 http://localhost:8000

## 확인

```bash
curl localhost:8000/health
# {"db":"ok","ollama":"ok","model":"qwen3:8b",
#  "indicators":"ok (5종 / 관측치 16건)",
#  "hardcap":"ok (v1 / max_loss<=20% mdd<=30% rebalance>=7d single_etf<=40%)"}
```

`db`/`ollama`/`hardcap` 중 하나라도 `error`면 그 메시지에 원인이 적혀 있음.
`indicators` 만 `empty` 인 건 정상 — 지표 없이도 `/compile` 은 동작한다.
반면 `hardcap` 이 `error` 면 `/compile` 이 503 이다 (안전 계층이라 fail closed, CLAUDE.md §18.5).

## 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 설문 UI |
| POST | `/compile` | 설문 → Spec JSON (+ 하드캡 조정 내역 `clamps`) |
| GET | `/specs` | 저장 이력 (원문 + Spec) |
| GET | `/health` | Ollama·DB·지표·하드캡 상태 |

## 셀프체크

```bash
docker compose exec api python -m app.schemas      # 스키마
docker compose exec api python -m app.validators   # 유니버스/레버리지 차단 + 하드캡
docker compose exec api python -m app.llm          # 실제 LLM 호출 (1~2분)
```

## 하드캡 (CLAUDE.md §18)

`max_loss_pct` 상한 초과는 **거부가 아니라 조정**이다 — 200 으로 응답하고 무엇이 어떻게
바뀌었는지 `clamps` 에 담긴다. 구조적 위반(동일 조건에 buy/sell 동시)만 400.

```bash
# 설문 밖 값 25% → 20 으로 클램프. HTTP 200 이다.
curl -s -X POST localhost:8000/compile -H 'Content-Type: application/json' \
  -d '{"sector":"반도체","risk":"aggressive","max_loss":25,
       "style":"추세추종(모멘텀)","rebalance":"monthly","note":""}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["spec"]["max_loss_pct"],d["clamps"])'
```

캡 값은 코드가 아니라 `hardcap_profile` 테이블에 있다. **활성 버전 = `MAX(version)`** 이고
요청마다 읽으므로, 새 버전을 INSERT 하면 **재시작 없이** 다음 요청부터 적용된다.

```bash
docker compose exec db psql -U demo -d demo -c \
  "INSERT INTO hardcap_profile VALUES (2, 15, 30, 7, 40, '상한 15로 조정');"
```

기존 버전은 UPDATE 로 덮지 않는다 (추가만 가능한 원장). 되돌릴 때도 DELETE 가 아니라
원래 값으로 새 버전을 INSERT 한다.

> 초기값은 **전부 팀 잠정 결정이며 개발 중 변경 가능**. MDD·최소 리밸런싱 간격·단일종목
> 상한 3종은 값만 저장하고 판정은 "판정 불가"를 반환하는 스텁이다 (CLAUDE.md §18.1).


## 기타

```bash
docker compose logs -f api     # 로그
docker compose down            # 종료
docker compose down -v         # DB까지 초기화 (재기동 시 스키마 재생성)
```

- `app/` 는 볼륨 마운트 + `--reload` 라 코드 수정 시 재빌드 불필요.
- `requirements.txt` 를 바꿨을 때만 `--build` 필요.
- 컴파일 1회에 **30~60초** 걸림 (로컬 8B 모델).
