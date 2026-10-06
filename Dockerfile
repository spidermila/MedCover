FROM docker.io/library/python:3.14-slim

WORKDIR /app

# Install Microsoft ODBC Driver 18 for SQL Server
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg2 \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc-dev libgssapi-krb5-2 \
    && apt-get purge -y --auto-remove curl gnupg2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-telemetry.txt ./
# python-ldap ships no wheels: compile it against the OpenLDAP client library,
# then drop the compiler and headers but keep the libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libldap2-dev libsasl2-dev libldap2 libsasl2-2 \
    && pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip install --no-cache-dir --require-hashes -r requirements-telemetry.txt \
    && apt-get purge -y --auto-remove gcc libldap2-dev libsasl2-dev \
    && rm -rf /var/lib/apt/lists/*

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
