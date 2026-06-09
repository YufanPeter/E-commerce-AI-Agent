pipeline {
    agent none

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    parameters {
        choice(
            name: 'DEPLOY_MODE',
            choices: ['local-transfer', 'registry'],
            description: 'local-transfer: 本地 docker build + scp 传输镜像到 ECS；registry: Kaniko 构建并推送到镜像仓库后 pull 部署'
        )
        string(name: 'IMAGE_TAG', defaultValue: '', description: 'Optional image tag. Empty means use GIT_COMMIT.')
        string(name: 'REMOTE_HOST', defaultValue: '118.196.64.197', description: '火山云 ECS 公网 IP')
        string(name: 'REMOTE_USER', defaultValue: 'root', description: 'SSH user on the target server')
        string(name: 'REMOTE_DIR', defaultValue: '/opt/ecommerce-ai-agent', description: 'Deployment directory on the target server')
        string(name: 'REGISTRY_HOST', defaultValue: '', description: 'Registry host, e.g. cr-cn-shanghai.volces.com')
        string(name: 'REGISTRY_NAMESPACE', defaultValue: '', description: 'Registry namespace or project')
        string(name: 'IMAGE_NAME', defaultValue: 'cartpilot-backend', description: 'Container image name')
        string(name: 'BASE_IMAGE', defaultValue: 'docker.1ms.run/library/python:3.11-slim', description: 'Docker base image (use mirror when Docker Hub is blocked)')
    }

    environment {
        DEPLOY_PORT = '8000'
        COMPOSE_FILE = 'docker-compose.prod.yml'
        DOCKER_BASE_IMAGE = "${params.BASE_IMAGE?.trim() ?: 'docker.1ms.run/library/python:3.11-slim'}"
    }

    stages {
        stage('Checkout') {
            agent any
            steps {
                checkout scm
                script {
                    env.EFFECTIVE_TAG = params.IMAGE_TAG?.trim() ? params.IMAGE_TAG.trim() : env.GIT_COMMIT.take(12)
                    env.FULL_IMAGE = params.DEPLOY_MODE == 'registry' && params.REGISTRY_HOST?.trim() && params.REGISTRY_NAMESPACE?.trim()
                        ? "${params.REGISTRY_HOST.trim()}/${params.REGISTRY_NAMESPACE.trim()}/${params.IMAGE_NAME.trim()}:${env.EFFECTIVE_TAG}"
                        : "${params.IMAGE_NAME.trim()}:${env.EFFECTIVE_TAG}"
                }
                stash name: 'source-tree', includes: '**/*', useDefaultExcludes: false
            }
        }

        stage('Test backend') {
            agent any
            steps {
                unstash 'source-tree'
                sh '''#!/bin/bash
                    set -euo pipefail
                    python3 -m pip install --upgrade pip
                    pip install -r backend/requirements.txt
                    cd backend
                    python3 -m pytest -q
                '''
            }
        }

        stage('Build image (local docker)') {
            when {
                expression { return params.DEPLOY_MODE == 'local-transfer' }
            }
            agent any
            steps {
                unstash 'source-tree'
                sh '''#!/bin/bash
                    set -euo pipefail
                    if ! command -v docker >/dev/null 2>&1; then
                        echo "docker is required on the Jenkins node for local-transfer mode" >&2
                        exit 1
                    fi
                    if docker build \
                        --build-arg BASE_IMAGE="${DOCKER_BASE_IMAGE}" \
                        -f deploy/Dockerfile \
                        -t "${FULL_IMAGE}" . ; then
                        docker save "${FULL_IMAGE}" | gzip > image.tar.gz
                    else
                        echo "local docker build failed; falling back to remote ECS build" >&2
                        echo "remote-build" > build-mode.txt
                        rsync -az --delete \
                            --exclude '.git/' --exclude '.venv/' --exclude 'client/' --exclude 'docs/' \
                            --exclude '__pycache__/' --exclude '.pytest_cache/' \
                            "${WORKSPACE}/" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/build-context/"
                        ssh -o StrictHostKeyChecking=no "${REMOTE_USER}@${REMOTE_HOST}" "set -eu
                            cd '${REMOTE_DIR}/build-context'
                            docker build \
                              --build-arg BASE_IMAGE='${DOCKER_BASE_IMAGE}' \
                              -f deploy/Dockerfile \
                              -t '${FULL_IMAGE}' .
                            docker save '${FULL_IMAGE}' | gzip > /tmp/cartpilot-image.tar.gz
                        "
                        scp -o StrictHostKeyChecking=no "${REMOTE_USER}@${REMOTE_HOST}:/tmp/cartpilot-image.tar.gz" image.tar.gz
                        ssh -o StrictHostKeyChecking=no "${REMOTE_USER}@${REMOTE_HOST}" "rm -f /tmp/cartpilot-image.tar.gz"
                    fi
                '''
                stash name: 'docker-image', includes: 'image.tar.gz'
            }
        }

        stage('Build and push image (registry)') {
            when {
                expression {
                    return params.DEPLOY_MODE == 'registry' && params.REGISTRY_HOST?.trim() && params.REGISTRY_NAMESPACE?.trim()
                }
            }
            agent {
                kubernetes {
                    defaultContainer 'kaniko'
                    yaml """
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: kaniko
    image: gcr.io/kaniko-project/executor:debug
    command:
    - cat
    tty: true
"""
                }
            }
            steps {
                unstash 'source-tree'
                withCredentials([usernamePassword(credentialsId: 'registry-credentials', usernameVariable: 'REGISTRY_USER', passwordVariable: 'REGISTRY_PASSWORD')]) {
                    sh '''#!/busybox/sh
                        set -eu
                        mkdir -p /kaniko/.docker
                        AUTH_TOKEN=$(printf '%s:%s' "$REGISTRY_USER" "$REGISTRY_PASSWORD" | base64 | tr -d '\n')
                        cat > /kaniko/.docker/config.json <<EOF
{"auths":{"${REGISTRY_HOST}":{"auth":"${AUTH_TOKEN}"}}}
EOF
                        /kaniko/executor \
                          --context "$WORKSPACE" \
                          --dockerfile "$WORKSPACE/deploy/Dockerfile" \
                          --destination "$FULL_IMAGE" \
                          --snapshotMode=redo \
                          --use-new-run
                    '''
                }
            }
        }

        stage('Deploy to server') {
            when {
                allOf {
                    expression { return params.REMOTE_HOST?.trim() }
                    expression { return params.REMOTE_USER?.trim() }
                }
            }
            agent any
            steps {
                script {
                    if (params.DEPLOY_MODE == 'local-transfer') {
                        unstash 'docker-image'
                    }
                }
                withCredentials([sshUserPrivateKey(credentialsId: 'deploy-ssh-key', keyFileVariable: 'SSH_KEY_FILE', usernameVariable: 'SSH_USER')]) {
                    sh '''#!/bin/bash
                        set -euo pipefail
                        SSH_OPTS="-o StrictHostKeyChecking=no -i ${SSH_KEY_FILE}"
                        REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

                        ssh ${SSH_OPTS} "${REMOTE}" "set -eu
                            if ! command -v docker >/dev/null 2>&1; then
                                apt-get update -qq
                                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2
                                systemctl enable --now docker
                            fi
                            mkdir -p /etc/docker
                            if [ ! -f /etc/docker/daemon.json ]; then
                                cat > /etc/docker/daemon.json <<'MIRROR'
{
  \"registry-mirrors\": [
    \"https://docker.1ms.run\",
    \"https://docker.ketches.cn\"
  ]
}
MIRROR
                                systemctl restart docker
                                sleep 2
                            fi
                            mkdir -p '${REMOTE_DIR}'
                        "

                        scp ${SSH_OPTS} deploy/docker-compose.prod.yml "${REMOTE}:${REMOTE_DIR}/${COMPOSE_FILE}"

                        if [ "${DEPLOY_MODE}" = "local-transfer" ]; then
                            if [ -f image.tar.gz ]; then
                                scp ${SSH_OPTS} image.tar.gz "${REMOTE}:/tmp/cartpilot-image.tar.gz"
                                ssh ${SSH_OPTS} "${REMOTE}" "set -eu
                                    gunzip -c /tmp/cartpilot-image.tar.gz | docker load
                                    rm -f /tmp/cartpilot-image.tar.gz
                                "
                            fi
                        else
                            ssh ${SSH_OPTS} "${REMOTE}" "set -eu
                                docker login '${REGISTRY_HOST}' || true
                                IMAGE_REF='${FULL_IMAGE}' docker compose -f '${REMOTE_DIR}/${COMPOSE_FILE}' pull
                            "
                        fi

                        ssh ${SSH_OPTS} "${REMOTE}" "set -eu
                            cd '${REMOTE_DIR}'
                            if [ ! -f .env ]; then
                                echo 'missing ${REMOTE_DIR}/.env on target host' >&2
                                exit 1
                            fi
                            IMAGE_REF='${FULL_IMAGE}' docker compose -f '${COMPOSE_FILE}' up -d --remove-orphans
                            for i in \$(seq 1 30); do
                                if curl -fsS --max-time 5 http://127.0.0.1:${DEPLOY_PORT}/health >/dev/null; then
                                    curl -fsS http://127.0.0.1:${DEPLOY_PORT}/health
                                    exit 0
                                fi
                                sleep 2
                            done
                            echo 'health check failed' >&2
                            docker compose -f '${COMPOSE_FILE}' logs --tail=80 backend || true
                            exit 1
                        "
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
