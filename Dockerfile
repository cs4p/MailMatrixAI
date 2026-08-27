# MailMatrixAI web app — production container image.
# Published to GHCR by .github/workflows/docker-publish.yml.
FROM python:3.14-slim

# - PYTHON_KEYRING_BACKEND: the app reads credentials from the macOS Keychain on
#   the desktop; in a Linux container there is no Keychain, so we pin the null
#   backend. keyring.get_password() then returns None and commonFunctions falls
#   back to os.environ — i.e. the env vars / K8s Secret below.
# - MAILMATRIX_DATA_DIR: learned rules (emailRules.json) + generated summaries
#   are written here; mount a volume at this path to persist them.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    MAILMATRIX_DATA_DIR=/data \
    MAILMATRIX_HOST=0.0.0.0 \
    MAILMATRIX_PORT=5000

WORKDIR /app

# Runtime dependencies (mirrors pyproject.toml) plus gunicorn as the WSGI server.
RUN pip install --no-cache-dir \
      "flask>=3.0" \
      "anthropic>=0.50.0" \
      "python-dotenv>=1.2.2" \
      "keyring>=25.0" \
      "gunicorn>=21.2"

# Application code (only what the server needs at runtime; see .dockerignore).
COPY app.py commonFunctions.py emailSummary.py sortEmail.py resortEmail.py \
     cleanupRules.py emailRulesInit.py emailRules.schema.json ./
COPY templates/ templates/
COPY static/ static/

# Run as an unprivileged user; /data is owned by it (K8s fsGroup also applies).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

VOLUME ["/data"]
EXPOSE 5000

# Single worker: the app keeps in-process caches/locks and guards emailRules.json
# with a threading.Lock (not a cross-process lock), so scale by replicas of 1
# worker, not by workers. Threads handle concurrent browser requests; the long
# timeout covers slow IMAP / Claude calls made inside a request.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "300", \
     "--bind", "0.0.0.0:5000", "--access-logfile", "-", "app:app"]
