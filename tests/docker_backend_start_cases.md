# Docker 后端启动测试样例

## 统一脚本前台启动

命令：

```bash
./scripts/start_backend.sh --docker
```

期望：
- 脚本使用 `deploy/docker-compose.yml` 启动后端。
- 镜像构建成功后监听 `8000`。
- `/health` 返回 `{"status":"ok"}`。

## 统一脚本后台启动

命令：

```bash
./scripts/start_backend.sh --docker -d
```

期望：
- Docker Compose 以 detached 模式运行。
- 脚本轮询健康检查后退出。
- 可通过 `docker compose -f deploy/docker-compose.yml logs -f backend` 查看日志。

## 缺少密钥文件

前置：仓库根目录没有 `.env`。

命令：

```bash
./scripts/start_backend.sh --docker
```

期望：
- 脚本提示 `.env` 缺失。
- 容器仍可尝试启动；如果后续对话需要豆包/embedding 密钥，会由后端返回对应错误。

## 镜像不安装本地 rerank 模型

前置：使用默认 Docker 配置。

命令：

```bash
docker compose -f deploy/docker-compose.yml build backend
```

期望：
- 镜像安装 `backend/requirements.txt` 时不安装 `sentence-transformers`。
- 镜像构建过程中不拉取 `torch` / CUDA 相关大包。
- 默认通过 `ARK_RERANKING_API_KEY`、`ARK_RERANKING_MODEL` 和 `USE_RERANK=1` 调用云端 API。
- 需要减少 API 请求时可设置 `USE_RERANK=0` 退回向量排序。
