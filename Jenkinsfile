pipeline {
    agent none

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    parameters {
        string(name: 'IMAGE_TAG', defaultValue: '', description: 'Optional image tag. Empty means use GIT_COMMIT.')
        string(name: 'REMOTE_HOST', defaultValue: '118.196.64.197', description: 'Target server IP or hostname')
        string(name: 'REMOTE_USER', defaultValue: 'root', description: 'SSH user on the target server')
        string(name: 'REMOTE_DIR', defaultValue: '/opt/ecommerce-ai-agent', description: 'Deployment directory on the target server')
        string(name: 'REGISTRY_HOST', defaultValue: '', description: 'Registry host, e.g. registry.cn-beijing.aliyuncs.com')
        string(name: 'REGISTRY_NAMESPACE', defaultValue: '', description: 'Registry namespace or project')
        string(name: 'IMAGE_NAME', defaultValue: 'cartpilot-backend', description: 'Container image name')
    }

    environment {
        DEPLOY_PORT = '8000'
        KANIKO_IMAGE = 'gcr.io/kaniko-project/executor:debug'
    }

    stages {
        stage('Checkout') {
            agent {
                kubernetes {
                    defaultContainer 'python'
                    yaml """
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: python
    image: python:3.11-slim
    command:
    - cat
    tty: true
"""
                }
            }
            steps {
                checkout scm
                script {
                    env.EFFECTIVE_TAG = params.IMAGE_TAG?.trim() ? params.IMAGE_TAG.trim() : env.GIT_COMMIT.take(12)
                    env.FULL_IMAGE = params.REGISTRY_HOST?.trim() && params.REGISTRY_NAMESPACE?.trim()
                        ? "${params.REGISTRY_HOST.trim()}/${params.REGISTRY_NAMESPACE.trim()}/${params.IMAGE_NAME.trim()}:${env.EFFECTIVE_TAG}"
                        : "${params.IMAGE_NAME.trim()}:${env.EFFECTIVE_TAG}"
                }
                stash name: 'source-tree', includes: '**/*', useDefaultExcludes: false
            }
        }

        stage('Test backend') {
            agent {
                kubernetes {
                    defaultContainer 'python'
                    yaml """
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: python
    image: python:3.11-slim
    command:
    - cat
    tty: true
"""
                }
            }
            steps {
                unstash 'source-tree'
                sh '''#!/bin/sh
                    set -eu
                    python -m pip install --upgrade pip
                    pip install -r backend/requirements.txt
                    cd backend
                    python -m pytest -q
                '''
            }
        }

        stage('Build and push image') {
            when {
                expression { return params.REGISTRY_HOST?.trim() && params.REGISTRY_NAMESPACE?.trim() }
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
            agent {
                kubernetes {
                    defaultContainer 'ssh'
                    yaml """
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: ssh
    image: alpine:3.19
    command:
    - cat
    tty: true
"""
                }
            }
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: 'deploy-ssh-key', keyFileVariable: 'SSH_KEY_FILE')]) {
                    sh '''#!/bin/sh
                        set -eu
                        apk add --no-cache openssh-client curl >/dev/null
                        SSH_OPTS="-o StrictHostKeyChecking=no -i ${SSH_KEY_FILE}"
                        ssh ${SSH_OPTS} ${REMOTE_USER}@${REMOTE_HOST} "set -eu
                            if ! command -v docker >/dev/null 2>&1; then
                                echo 'docker is not installed on target host' >&2
                                exit 1
                            fi
                            if [ ! -d '${REMOTE_DIR}' ]; then
                                echo 'deployment directory not found: ${REMOTE_DIR}' >&2
                                exit 1
                            fi
                            cd '${REMOTE_DIR}'
                            IMAGE_REF='${FULL_IMAGE}' docker compose pull
                            IMAGE_REF='${FULL_IMAGE}' docker compose up -d
                            curl -fsS --retry 20 --retry-delay 2 --max-time 5 http://127.0.0.1:${DEPLOY_PORT}/health
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