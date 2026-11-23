# App 2: normal worker pod (web frontend). Replace the placeholder tag below.
ARG BASE_IMAGE=base-images-catalog.artifactory-bringer.group.bringernet/ibm9/python-311:1-52.xxxxx
FROM ${BASE_IMAGE}

# Build-time proxy / package index settings. These are not kept in the final image.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN if [ -n "$HTTP_PROXY" ]; then \
        pip install --proxy "$HTTP_PROXY" --no-cache-dir -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

COPY frontend.py .
COPY templates ./templates
COPY static ./static

EXPOSE 8080

# Numeric non-root user: OpenShift's restricted SCC replaces the UID, and the files
# above are readable by any UID. Change 1001 if your base image uses another user.
USER 1001

CMD ["python", "frontend.py"]
# Optional, if gunicorn is approved in your Artifactory (add it to requirements.txt):
# CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "frontend:app"]
