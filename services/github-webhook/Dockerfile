FROM python:3.13-alpine3.22
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY receiver.py projects.json ./
ARG REVISION=development
ENV XDOCKER_REVISION=$REVISION PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
USER 1000:1000
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "15", "receiver:create_app()"]
