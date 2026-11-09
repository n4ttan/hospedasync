#!/usr/bin/env bash
# ============================================================================
# NÃO TESTADO: revise antes de rodar; cobra da sua conta.
# ============================================================================
# Iniciando as 3 instancias do HospedaSync (tag Project=hospedasync) para economizar credito.
# DRY_RUN=1 apenas imprime os comandos.
set -euo pipefail
: "${AWS_REGION:=us-east-1}"
: "${DRY_RUN:=0}"
export AWS_DEFAULT_REGION="$AWS_REGION"

echo "+ aws ec2 describe-instances (Project=hospedasync, estado: stopped)" >&2
if [ "$DRY_RUN" = 1 ]; then IDS="<ids-das-3-instancias>"; else
  IDS=$(aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=hospedasync" "Name=instance-state-name,Values=stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
fi
[ -n "$IDS" ] || { echo "Nenhuma instancia no estado esperado." >&2; exit 0; }

echo "+ aws ec2 start-instances --instance-ids $IDS" >&2
echo "+ aws ec2 wait instance-running --instance-ids $IDS" >&2
if [ "$DRY_RUN" != 1 ]; then
  aws ec2 start-instances --instance-ids $IDS >/dev/null
  aws ec2 wait instance-running --instance-ids $IDS
fi
echo "Instancias no ar. O IP PUBLICO MUDOU (sem Elastic IP): regenere o certificado do edge com o novo IP" >&2
echo "(scripts/gen_selfsigned_cert.sh <novo_ip>) e reinicie o nginx. Os IPs privados permanecem. Os containers" >&2
echo "voltam sozinhos (restart: unless-stopped); a replica leva alguns segundos para recuperar o atraso." >&2
echo "+ aws ec2 describe-instances ... (IPs)" >&2
if [ "$DRY_RUN" != 1 ]; then
  aws ec2 describe-instances --instance-ids $IDS \
    --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,PrivateIpAddress,PublicIpAddress]' --output text
fi
