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
# {"db":"ok","ollama":"ok","model":"qwen3:8b"}
```

`db`/`ollama` 중 하나라도 `error`면 그 메시지에 원인이 적혀 있음.

## 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 설문 UI |
| POST | `/compile` | 설문 → Spec JSON |
| GET | `/specs` | 저장 이력 (원문 + Spec) |
| GET | `/health` | Ollama·DB 상태 |

## 셀프체크

```bash
docker compose exec api python -m app.schemas      # 스키마
docker compose exec api python -m app.validators   # 유니버스/레버리지 차단
docker compose exec api python -m app.llm          # 실제 LLM 호출 (1~2분)
```

## 기타

```bash
docker compose logs -f api     # 로그
docker compose down            # 종료
docker compose down -v         # DB까지 초기화 (재기동 시 스키마 재생성)
```

- `app/` 는 볼륨 마운트 + `--reload` 라 코드 수정 시 재빌드 불필요.
- `requirements.txt` 를 바꿨을 때만 `--build` 필요.
- 컴파일 1회에 **30~60초** 걸림 (로컬 8B 모델).
