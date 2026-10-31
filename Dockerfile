# One image: builds the judge-facing frontend (web/) and serves it, /api and /ws from the Python backend, for Render (see render.yaml).
# web/ imports types and hooks from ../dashboard/src, so both folders are copied into the build stage.

FROM node:22-slim AS web
WORKDIR /app
COPY dashboard/src ./dashboard/src
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci
COPY web ./web
RUN cd web && npm run build

FROM python:3.12-slim
ARG INSTALL_ML=0
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt requirements-ml.txt ./
RUN pip install -r requirements.txt
# The grounding gate's models (torch, sentence-transformers). Needed for MUNIM_MODE=llm or offline; they need about 2 GB of RAM, so they are off by default.
RUN if [ "$INSTALL_ML" = "1" ]; then pip install -r requirements-ml.txt; fi
COPY . .
COPY --from=web /app/web/dist ./web/dist
CMD ["sh", "scripts/render_start.sh"]
