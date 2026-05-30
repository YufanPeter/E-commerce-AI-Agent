# Jenkins CI/CD 流水线

这套流水线面向“云原生 Jenkins”：构建和发布都按 Kubernetes ephemeral agent 运行，Jenkins 只负责编排，不要求把 Python 依赖装到宿主机。

## 流水线做什么

- 拉取仓库代码。
- 在容器里运行后端测试。
- 用 Kaniko 构建后端镜像并推送到镜像仓库。
- SSH 到云服务器，拉取新镜像并重启后端服务。
- 最后访问 `/health` 做发布校验。

## Jenkins 需要的凭据

- `registry-credentials`：镜像仓库用户名和密码。
- `deploy-ssh-key`：登录云服务器的 SSH 私钥。

## Jenkins 节点要求

- Jenkins 已安装 Kubernetes 插件，并能创建 ephemeral agent。
- 能访问镜像仓库。
- 能通过 SSH 访问目标服务器。

## 目标服务器要求

- 已安装 Docker 和 Docker Compose 插件。
- 已把仓库部署到 `/opt/ecommerce-ai-agent`，或在 Jenkins 参数里改成别的目录。
- 目录里有 `.env`，至少包含：

```bash
ARK_API_KEY=...
ARK_MODEL=...
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/
ARK_EMBEDDING_API_KEY=...
ARK_EMBEDDING_MODEL=...
```

## Jenkins Job 参数

- `IMAGE_TAG`：可选，手工指定镜像 tag；不填时使用提交号前 12 位。
- `REMOTE_HOST`：目标服务器地址，默认 `118.196.64.197`。
- `REMOTE_USER`：SSH 用户，默认 `root`。
- `REMOTE_DIR`：部署目录，默认 `/opt/ecommerce-ai-agent`。
- `REGISTRY_HOST`：镜像仓库地址。
- `REGISTRY_NAMESPACE`：镜像仓库命名空间或项目名。
- `IMAGE_NAME`：镜像名，默认 `cartpilot-backend`。

## 触发建议

- `main` 分支合并后自动发布。
- `feature/*` 分支只跑测试，不走发布。
- 镜像 tag 建议使用提交号，避免覆盖旧版本。

## 你这份仓库的注意点

- 后端镜像用的是 `deploy/Dockerfile`。
- `deploy/docker-compose.yml` 已支持 `IMAGE_REF`，Jenkins 部署时会显式拉取并使用指定镜像。
- 你的云服务器之前还没有装 Docker，所以 Jenkins 发布前必须先把服务器侧 Docker 环境装好。