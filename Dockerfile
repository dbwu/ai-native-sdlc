FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY requirements.lock ./
COPY knowledge ./knowledge
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
USER 10001
EXPOSE 8000
CMD ["sh", "-c", "python -m knowledge.db && exec uvicorn knowledge.api:app --host 0.0.0.0 --port 8000"]
