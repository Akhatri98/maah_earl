FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEMO_MODE=true \
    PORT=8000 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN groupadd --gid 10001 earl && useradd --uid 10001 --gid earl --create-home earl

COPY --chown=earl:earl earl ./earl
COPY --chown=earl:earl scripts ./scripts
COPY --chown=earl:earl tests ./tests
RUN mkdir /app/out && chown earl:earl /app/out
USER earl
RUN python -m unittest discover -s tests -q

ARG EARL_BUILD_SHA=local-astra
ENV EARL_BUILD_SHA=${EARL_BUILD_SHA}
LABEL org.opencontainers.image.title="EARL" \
      org.opencontainers.image.source="https://github.com/Akhatri98/maah_earl/tree/astra" \
      org.opencontainers.image.revision=${EARL_BUILD_SHA}

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=4)"
CMD ["sh", "-c", "exec python -m uvicorn earl.web:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
