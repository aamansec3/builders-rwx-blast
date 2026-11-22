# Same base image the scorer labeled with: its Python and SQLite decide which
# sqlglot tests pass, and the selector runs some of them itself.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, byte-compiled so runs never compile it again.
RUN python -c "import io, tarfile, urllib.request; \
data = urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz', timeout=120).read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')"
RUN mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot

COPY selector /app/selector
COPY maps /app/maps
RUN python -m compileall -q /app/selector && chmod -R a+rX /app

ENV PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 BLAST_SRC=/opt/sqlglot
CMD ["python", "/app/selector/main.py"]
