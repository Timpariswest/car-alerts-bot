FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.3 gunicorn==22.0.0 requests==2.32.3
COPY webhook_server.py .
CMD gunicorn webhook_server:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --timeout 30
