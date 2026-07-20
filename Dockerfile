# Single image, shared by the web, worker, and migrate services in
# docker-compose.yml — only the command differs per service.
FROM python:3.12-slim

WORKDIR /app

# psycopg2-binary ships prebuilt wheels for this base image, so no build-time
# Postgres headers are needed. gcc is kept for any dependency that doesn't
# publish a wheel for this platform/Python combination.
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # gunicorn is Linux-only (uses os.fork/fcntl) and would break `pip install
    # -r requirements.txt` on the Windows dev machine this project is built
    # on — installed here, container-side only, never added to requirements.txt.
    && pip install --no-cache-dir gunicorn==23.0.0

# Baked into the image so the container never needs internet access at
# runtime to anonymize a resume — satisfies "no manual setup steps".
RUN python -m spacy download en_core_web_sm

COPY . .

RUN mkdir -p webapp/uploads

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "3", "--timeout", "60", "webapp.app:app"]
