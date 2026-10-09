FROM python:3.13-alpine3.22
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py docker_api.py backups.py instances.py ./
COPY static ./static
ARG REVISION=development
ENV XDOCKER_REVISION=$REVISION PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
USER 1000:1000
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "90", "app:create_app()"]
