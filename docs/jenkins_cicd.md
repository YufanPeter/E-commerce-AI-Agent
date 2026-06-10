# Jenkins CI/CD 流水线

这套流水线用于 `production` 分支发布：Jenkins 收到 push 后拉取最新代码，SSH 到服务器，在服务器部署目录执行 `git pull/reset`，然后后台运行 `scripts/start_backend.sh`。`main` 分支 push 不会自动发布，避免开发代码直接上网。

## 流水线做什么

- Jenkins checkout 最新仓库代码。
- 创建临时 `.venv-ci` 并运行后端测试。
- SSH 到目标服务器。
- 目标服务器进入 `/opt/ecommerce-ai-agent`，拉取 `origin/production` 最新代码。
- 首次不存在仓库时会用浅克隆创建部署目录。
- 用 `HOST=0.0.0.0 FORCE_RESTART=1 ./scripts/start_backend.sh` 后台重启后端。
- 最后访问 `http://127.0.0.1:8000/health` 做发布校验。

## Jenkins 需要的凭据

- `deploy-ssh-key`：Jenkins 连接目标服务器的 SSH 私钥。

## Jenkins Job 参数

- `REMOTE_HOST`：目标服务器地址，默认 `118.196.64.197`。
- `REMOTE_USER`：SSH 用户，默认 `root`。
- `REMOTE_DIR`：服务器上的仓库目录，默认 `/opt/ecommerce-ai-agent`。
- `GIT_URL`：服务器可以访问的仓库地址，默认 `https://github.com/YufanPeter/E-commerce-AI-Agent.git`。
- `DEPLOY_BRANCH`：发布分支，默认 `production`。
- `DEPLOY_PORT`：后端端口，默认 `8000`。

## 目标服务器要求

- 能通过 Jenkins 的 `deploy-ssh-key` 登录。
- 已允许服务器自身访问 GitHub 仓库；默认使用 HTTPS 地址拉取公开仓库。如果仓库改为私有，需要改成带权限的地址或给服务器配置 GitHub deploy key。
- `/opt/ecommerce-ai-agent/.env` 已存在，至少包含：

```bash
ARK_API_KEY=...
ARK_MODEL=...
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/
ARK_EMBEDDING_API_KEY=...
ARK_EMBEDDING_MODEL=...
ZHIPU_API_KEY=...
```

第一次运行时，如果服务器缺少 `git`、`curl`、`lsof`、`python3-venv`、`python3-pip`，流水线会尝试用 `apt-get` 安装。

## 自动触发

推荐在 GitHub 仓库配置 webhook：

```text
Payload URL: http://<jenkins-host>/github-webhook/
Content type: application/json
Events: Just the push event
```

Jenkins Job 需要启用 GitHub hook trigger for GITScm polling。`Jenkinsfile` 里也保留了 `pollSCM('H/2 * * * *')`，即使 webhook 没打通，也会每 2 分钟检查一次 `main` 更新。

## 手工验证

服务器上可以手工执行：

```bash
cd /opt/ecommerce-ai-agent
git fetch origin production
git reset --hard origin/production
HOST=0.0.0.0 FORCE_RESTART=1 ./scripts/start_backend.sh
```

Jenkins 发布时会用 `nohup` 后台运行，日志写入：

```bash
/opt/ecommerce-ai-agent/backend.log
```
