# Osori Backend

Osori는 AI 챗봇의 프롬프트를 같은 질문과 기준으로 비교하고, 배포 전 정책에 어긋나는 응답을 확인할 수 있도록 돕는 서비스입니다.

백엔드는 FastAPI와 Python으로 구성합니다. 현재는 초기 개발 골격과 헬스 체크만 있으며 서비스 기능은 구현 예정입니다.

## 실행

Python 3.11 이상이 필요합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

API 문서는 `http://127.0.0.1:8000/docs`에서 확인할 수 있습니다. 프론트엔드: [Osori-Frontend](https://github.com/team-osori/Osori-Frontend)

```bash
.venv/bin/python -m pytest
```
