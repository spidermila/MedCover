FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY . .

# Embed the git commit hash at build time:
#   docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) .
ARG GIT_COMMIT=dev
ENV GIT_COMMIT=${GIT_COMMIT}

COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY docker-entrypoint-scheduler.sh /docker-entrypoint-scheduler.sh
RUN chmod +x /docker-entrypoint.sh /docker-entrypoint-scheduler.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} \"app:create_app()\""]
