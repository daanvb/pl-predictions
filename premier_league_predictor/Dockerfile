FROM ghcr.io/home-assistant/base:3.23

RUN apk add --no-cache \
    python3 \
    py3-pip

WORKDIR /app

COPY app/ /app/
COPY CHANGELOG.md /repository-CHANGELOG.md

RUN pip3 install \
    --no-cache-dir \
    --break-system-packages \
    flask==3.1.3 \
    requests==2.34.2 \
    google-api-python-client==2.199.0 \
    google-auth==2.57.0 \
    google-auth-httplib2==0.4.2 \
    google-auth-oauthlib==1.4.1

# Build-time regression tests run with the same Flask/Jinja dependencies as runtime.
RUN python3 -m py_compile /app/app.py /app/database.py /app/database_restore.py /app/scoring.py /app/football_api.py /app/api_football.py /app/sportscore.py \
    && cmp -s /repository-CHANGELOG.md /app/CHANGELOG.md \
    && python3 /app/audit_selftest.py \
    && rm -rf /data/* /repository-CHANGELOG.md

CMD ["python3", "/app/app.py"]
