FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
# 추론 extra 포함 (학습 코드 없음). torch CPU 휠.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu ".[ml]"
COPY app ./app
COPY scripts ./scripts

# 가중치는 이미지에 넣지 않는다. 시작 시 MODEL_BLOB_URL 이 있으면 models/ 로 내려받고, 없으면 제로샷(Azure OpenAI)으로 동작.
EXPOSE 8000
CMD ["sh", "-c", "python scripts/fetch_models.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
