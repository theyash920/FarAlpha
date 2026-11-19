FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app.py .

USER 10001:10001
EXPOSE 5000
CMD ["gunicorn", "--bind=0.0.0.0:5000", "--workers=1", "--threads=4", "--worker-tmp-dir=/tmp", "--no-control-socket", "--error-logfile=-", "app:create_app()"]
