# Backend Docker 部署说明

## 适用场景

这套 Docker 配置用于固定后端运行环境，避免不同机器上的 Python 版本、依赖和启动命令不一致。后续推到火山云镜像仓库时，也可以直接复用同一个镜像。

当前镜像会复制 `backend/` 和 `data/`。如果本地已经构建过 `backend/storage/`，SQLite 商品库和 Chroma 索引也会一起打进镜像；`.env` 不会进入镜像，密钥在运行容器时注入。

## 本地准备

先确认本地数据已构建：

```bash
cd backend
../.venv/bin/python -m store.import_product_data --reset
../.venv/bin/python -m store.import_image_manifest
../.venv/bin/python -m rag.build_chroma --reset
cd ..
```

确认仓库根目录有 `.env`：

```bash
ARK_API_KEY=你的豆包API_Key
ARK_MODEL=你的endpoint_id
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/
ARK_EMBEDDING_API_KEY=你的embedding_API_Key
ARK_EMBEDDING_MODEL=你的embedding_endpoint_id
ZHIPU_API_KEY=你的智谱APIKey
RERANK_MODEL=rerank
RERANK_BASE_URL=https://open.bigmodel.cn/api/paas/v4/rerank
```

## 本地运行

推荐直接使用统一启动脚本，脚本会自动指定 `deploy/docker-compose.yml`，并轮询 `/health`：

```bash
./scripts/start_backend.sh --docker

# 后台运行
./scripts/start_backend.sh --docker -d
```

也可以手动执行 compose。Docker 相关文件都放在 `deploy/`，需要通过 `-f` 指定 compose 文件：

```bash
docker compose -f deploy/docker-compose.yml up --build
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

返回 `{"status":"ok"}` 即启动成功。

后台运行：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f backend
```

停止：

```bash
docker compose -f deploy/docker-compose.yml down
```

## 直接使用 docker 命令

在仓库根目录执行，通过 `-f` 指定 `deploy/Dockerfile`：

```bash
docker build -f deploy/Dockerfile -t cartpilot-backend:local .
docker run --rm --env-file .env -p 8000:8000 cartpilot-backend:local
```

## 推送到火山云镜像仓库

在火山云容器镜像服务中创建命名空间和仓库后，按控制台给出的地址登录、打标签、推送。下面用占位符表示：

```bash
docker login <registry-host>
docker tag cartpilot-backend:local <registry-host>/<namespace>/cartpilot-backend:0.1.0
docker push <registry-host>/<namespace>/cartpilot-backend:0.1.0
```

云服务器运行：

```bash
docker pull <registry-host>/<namespace>/cartpilot-backend:0.1.0
docker run -d \
  --name cartpilot-backend \
  --restart unless-stopped \
  --env-file /opt/cartpilot/.env \
  -p 8000:8000 \
  <registry-host>/<namespace>/cartpilot-backend:0.1.0
```

安全组需要放行 TCP `8000`，或者前面再挂 Nginx/负载均衡并只对外暴露 `80/443`。

## 注意事项

- `.env` 不要打进镜像，也不要提交到 Git。
- 如果更换 embedding endpoint，必须重建 `backend/storage/chroma` 后重新构建镜像。
- Rerank 走智谱云端 API，不再安装本地 `torch` / `sentence-transformers` / CUDA 依赖。
- Docker 默认 `USE_RERANK=1`，通过 `ZHIPU_API_KEY`、`RERANK_MODEL`、`RERANK_BASE_URL` 调用智谱精排；调试阶段不做失败降级，智谱接口失败会直接报错。需要减少 API 请求时运行容器时设为 `USE_RERANK=0`。
- 如果不想把 `backend/storage` 打进镜像，可以在云服务器挂载外部目录到 `/app/backend/storage`。
