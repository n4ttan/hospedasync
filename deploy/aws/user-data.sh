#!/bin/bash
# Bootstrap (cloud-init / user data) para Amazon Linux 2023. SEM segredos: o .env.aws e copiado depois, por scp.
# Log: /var/log/hospeda-bootstrap.log
set -euxo pipefail
exec > >(tee -a /var/log/hospeda-bootstrap.log) 2>&1

dnf -y update
dnf -y install docker git openssl

systemctl enable --now docker
usermod -aG docker ec2-user

# Plugin do Compose v2 (nao vem no pacote docker do AL2023)
COMPOSE_VERSION="${COMPOSE_VERSION:-v2.29.7}"
ARCH="$(uname -m)"   # x86_64 (t3) ou aarch64
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Swap de 1 GiB: o build das imagens em t3.micro (1 GiB RAM) pode estourar a memoria
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "bootstrap concluido: $(docker --version); $(docker compose version)"
