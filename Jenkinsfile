pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    triggers {
        pollSCM('H/2 * * * *')
    }

    parameters {
        string(name: 'REMOTE_HOST', defaultValue: '118.196.64.197', description: 'Target server host')
        string(name: 'REMOTE_USER', defaultValue: 'root', description: 'SSH user on the target server')
        string(name: 'REMOTE_DIR', defaultValue: '/opt/ecommerce-ai-agent', description: 'Repository directory on the target server')
        string(name: 'GIT_URL', defaultValue: 'https://github.com/YufanPeter/E-commerce-AI-Agent.git', description: 'Git repository URL reachable from the target server')
        string(name: 'DEPLOY_BRANCH', defaultValue: 'production', description: 'Branch to deploy')
        string(name: 'DEPLOY_PORT', defaultValue: '8000', description: 'Backend port for health check')
    }

    environment {
        PYTHONUNBUFFERED = '1'
    }

    stages {
        stage('Checkout latest code') {
            steps {
                checkout scm
            }
        }

        stage('Test backend') {
            steps {
                sh '''#!/bin/bash
                    set -euo pipefail
                    python3 -m venv .venv-ci
                    . .venv-ci/bin/activate
                    python -m pip install --upgrade pip
                    pip install -r backend/requirements.txt
                    cd backend
                    python -m pytest -q
                '''
            }
        }

        stage('Deploy production to server') {
            when {
                anyOf {
                    branch 'production'
                    expression { return env.BRANCH_NAME == null || env.BRANCH_NAME == params.DEPLOY_BRANCH }
                    expression { return env.GIT_BRANCH == "origin/${params.DEPLOY_BRANCH}" }
                }
            }
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: 'deploy-ssh-key', keyFileVariable: 'SSH_KEY_FILE', usernameVariable: 'SSH_CREDENTIAL_USER')]) {
                    sh '''#!/bin/bash
                        set -euo pipefail

                        SSH_OPTS="-o StrictHostKeyChecking=no -i ${SSH_KEY_FILE}"
                        REMOTE_LOGIN="${REMOTE_USER:-${SSH_CREDENTIAL_USER}}@${REMOTE_HOST}"

                        ssh ${SSH_OPTS} "${REMOTE_LOGIN}" \
                          "REMOTE_DIR='${REMOTE_DIR}' GIT_URL='${GIT_URL}' DEPLOY_BRANCH='${DEPLOY_BRANCH}' DEPLOY_PORT='${DEPLOY_PORT}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! command -v lsof >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl lsof python3 python3-venv python3-pip
  fi
fi

mkdir -p "$(dirname "${REMOTE_DIR}")"

if [ ! -d "${REMOTE_DIR}/.git" ]; then
  if [ -d "${REMOTE_DIR}" ] && [ "$(find "${REMOTE_DIR}" -mindepth 1 -maxdepth 1 2>/dev/null | head -1)" ]; then
    echo "${REMOTE_DIR} exists but is not a git repository; move it away or clone the repo there first." >&2
    exit 1
  fi
  git clone --depth 1 --branch "${DEPLOY_BRANCH}" "${GIT_URL}" "${REMOTE_DIR}"
else
  cd "${REMOTE_DIR}"
  git fetch origin "${DEPLOY_BRANCH}"
  git checkout "${DEPLOY_BRANCH}"
  git reset --hard "origin/${DEPLOY_BRANCH}"
fi

cd "${REMOTE_DIR}"

if [ ! -f .env ]; then
  echo "missing ${REMOTE_DIR}/.env on target host" >&2
  exit 1
fi

chmod +x scripts/start_backend.sh
nohup env HOST=0.0.0.0 PORT="${DEPLOY_PORT}" FORCE_RESTART=1 BACKEND_WARMUP=1 \
  ./scripts/start_backend.sh > backend.log 2>&1 &

for i in $(seq 1 90); do
  if curl -fsS --max-time 5 "http://127.0.0.1:${DEPLOY_PORT}/health"; then
    echo
    echo "backend deployed from ${DEPLOY_BRANCH} and is healthy"
    exit 0
  fi
  sleep 2
done

echo "health check failed; tailing backend.log" >&2
tail -n 120 backend.log >&2 || true
exit 1
REMOTE_SCRIPT
                    '''
                }
            }
        }
    }

    post {
        always {
            cleanWs(deleteDirs: true, disableDeferredWipeout: true)
        }
    }
}
