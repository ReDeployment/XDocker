FROM python:3.13-alpine3.22
WORKDIR /app
COPY requirements.txt .
RUN apk add --no-cache git openssh-client
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py docker_api.py backups.py instances.py preflight.py backup_plans.py image_versions.py github_watch.py github_webhooks.py github_triage.py github_auth.py ./
COPY host_metrics.py gpu_metrics.py resource_monitor.py registry_store.py native_services.py gateway_runtime.py service_hub_api.py native_runtime.py ./
COPY sync_scan.py sync_runtime.py sync_store.py sync_agent.py code_sync.py code_sync_api.py ./
COPY static ./static
ARG REVISION=development
ENV XDOCKER_REVISION=$REVISION PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
USER 1000:1000
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "90", "app:create_app()"]
