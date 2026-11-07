# Docker 后端启动测试样例

[English](docker_backend_start_cases.md) | [简体中文](docker_backend_start_cases.zh-CN.md)

## 统一脚本前台启动

```bash
./scripts/start_backend.sh --docker
```

期望：

- 脚本使用 `deploy/docker-compose.yml` 启动后端。
- 镜像构建成功后监听 `8000`。
- `/health` 返回 `{"status":"ok"}`。

## 统一脚本后台启动

```bash
./scripts/start_backend.sh --docker -d
```

期望：

- Docker Compose 以 detached 模式运行。
- 脚本轮询健康检查后退出。
- 可通过 `docker compose -f deploy/docker-compose.yml logs -f backend` 查看日志。

## 缺少环境文件

前置：仓库根目录没有 `.env`。

```bash
./scripts/start_backend.sh --docker
```

期望：

- 脚本提示 `.env` 缺失。
- 容器仍可尝试启动；配置凭证前，需要模型能力的请求会返回对应配置错误。

## 镜像不安装本地 reranking 模型

```bash
docker compose -f deploy/docker-compose.yml build backend
```

期望：

- 安装 `backend/requirements.txt` 时不安装 `sentence-transformers`。
- 镜像构建过程中不拉取 Torch 或 CUDA 大包。
- `USE_RERANK=1` 时使用配置的 `ZHIPU_API_KEY`、`RERANK_MODEL` 和 `RERANK_BASE_URL` 调用 reranking API。
- 设置 `USE_RERANK=0` 后不调用 reranking API，退回原检索排序。
