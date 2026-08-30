# Deployment image for hosts that run a real process — Railway, Render, Fly, a VM.
#
# Preferred over the serverless path for this app. A persistent process means the
# in-process queue worker drains jobs continuously, so no external scheduler is
# needed and a long job finishes even if the user closes their tab.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change does not invalidate the install layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "uvicorn[standard]>=0.30"

COPY src/ ./src/
COPY saas/ ./saas/
COPY config/ ./config/
COPY docs/ ./docs/

# src layout: make the agent package importable without installing the project.
ENV PYTHONPATH=/app:/app/src

# The worker runs in-process here; there is a live process to run it in.
ENV WORKER_IN_PROCESS=1

# Run unprivileged. Nothing here needs root, and a container escape should not
# start from one.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# Single worker on purpose: the queue worker thread should not be duplicated per
# process, and one process is ample at this size. Scale by raising the plan's CPU
# before adding workers, and move the queue to a dedicated service after that.
CMD ["sh", "-c", "uvicorn saas.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
