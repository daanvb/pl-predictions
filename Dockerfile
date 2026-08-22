FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache \
    python3 \
    py3-pip

WORKDIR /app

COPY app/ /app/

RUN pip3 install \
    --no-cache-dir \
    --break-system-packages \
    flask \
    requests \
    google-api-python-client \
    google-auth \
    google-auth-httplib2 \
    google-auth-oauthlib

# Build-time regression tests run with the same Flask/Jinja dependencies as runtime.
RUN python3 -m py_compile /app/app.py /app/database.py /app/scoring.py /app/football_api.py \
    && python3 /app/audit_selftest.py \
    && rm -rf /data/*

CMD ["python3", "/app/app.py"]
