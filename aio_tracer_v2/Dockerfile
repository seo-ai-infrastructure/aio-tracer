FROM node:22-bookworm-slim AS ui
WORKDIR /ui
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 AIO_DATA_DIR=/data AIO_DATABASE_PATH=/data/aio.sqlite
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium
COPY . ./
COPY --from=ui /ui/dist ./dashboard/dist
EXPOSE 8080
CMD ["sh", "-c", "python -m uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]
