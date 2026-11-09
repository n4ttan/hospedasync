FROM python:3.11-slim
WORKDIR /app
ENV PYTHONPATH=/app:/app/shared PYTHONUNBUFFERED=1
COPY api-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared /app/shared
COPY api-gateway/ /app/
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
