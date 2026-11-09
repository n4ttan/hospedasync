FROM python:3.11-slim
WORKDIR /app
ENV PYTHONPATH=/app:/app/shared PYTHONUNBUFFERED=1
COPY workers/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared /app/shared
COPY workers/*.py /app/
EXPOSE 8001
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}"]
