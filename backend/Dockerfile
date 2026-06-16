# Emma product backend (Prompt 31, Part E). Deploy target: Fly.io.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/backend
ENV PYTHONUNBUFFERED=1 PORT=8000

EXPOSE 8000
# WebSocket-capable single worker (the proxy holds long-lived sockets).
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
