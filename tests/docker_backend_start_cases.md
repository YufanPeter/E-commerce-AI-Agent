# Docker Backend Startup Test Cases

[English](docker_backend_start_cases.md) | [简体中文](docker_backend_start_cases.zh-CN.md)

## Foreground startup

```bash
./scripts/start_backend.sh --docker
```

Expected:

- The script starts the backend with `deploy/docker-compose.yml`.
- The image builds successfully and listens on port `8000`.
- `/health` returns `{"status":"ok"}`.

## Detached startup

```bash
./scripts/start_backend.sh --docker -d
```

Expected:

- Docker Compose runs in detached mode.
- The script exits after the health check succeeds.
- Logs are available with `docker compose -f deploy/docker-compose.yml logs -f backend`.

## Missing environment file

Precondition: the repository root has no `.env` file.

```bash
./scripts/start_backend.sh --docker
```

Expected:

- The script reports the missing `.env` file.
- The container may still start, but model-dependent requests return configuration errors until credentials are provided.

## No local reranking model in the image

```bash
docker compose -f deploy/docker-compose.yml build backend
```

Expected:

- Installing `backend/requirements.txt` does not install `sentence-transformers`.
- The build does not download large Torch or CUDA packages.
- Reranking uses the configured `ZHIPU_API_KEY`, `RERANK_MODEL`, and `RERANK_BASE_URL` when `USE_RERANK=1`.
- Setting `USE_RERANK=0` falls back to retrieval ordering without a reranking request.
