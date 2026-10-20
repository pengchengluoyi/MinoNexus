# syntax=docker/dockerfile:1
ARG WHEEL_STAGE=source-wheel
FROM node:22-bookworm AS workbench
ARG WORKBENCH_REF=main
WORKDIR /workbench
RUN git init && git remote add origin https://github.com/pengchengluoyi/MinoWorkbench.git && git fetch --depth=1 origin "$WORKBENCH_REF" && git checkout --detach FETCH_HEAD
RUN npm ci && npm run build -- --base ./ && git rev-parse HEAD > /workbench-commit

FROM python:3.12-slim AS source-wheel
WORKDIR /build
COPY pyproject.toml ./
COPY mino_nexus/ ./mino_nexus/
COPY scripts/prepare_web.py ./scripts/prepare_web.py
COPY --from=workbench /workbench/dist/ /web/
COPY --from=workbench /workbench/package.json /workbench-package.json
COPY --from=workbench /workbench-commit /workbench-commit
RUN python scripts/prepare_web.py --dist /web --package-json /workbench-package.json --source-commit "$(cat /workbench-commit)" && python -m pip install --no-cache-dir build && python -m build --wheel --outdir /wheels

# Release CI uses the already built wheel, never a second source build.
FROM scratch AS release-wheel
COPY dist/*.whl /wheels/

FROM ${WHEEL_STAGE} AS selected-wheel
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MINO_NEXUS_MODE=production MINO_NEXUS_DATA_DIR=/data MINO_NEXUS_MDNS=0
COPY --from=selected-wheel /wheels/ /wheels/
RUN python -m pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels && useradd --uid 10001 --create-home nexus && mkdir /data && chown nexus:nexus /data
WORKDIR /home/nexus
USER 10001:10001
EXPOSE 50104
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD ["python", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:50104/health', timeout=4); assert r.status == 200 and json.load(r)['ok']"]
ENTRYPOINT ["mino-nexus"]
CMD ["--prod", "--host", "0.0.0.0", "--port", "50104", "--data-dir", "/data", "--no-mdns"]
