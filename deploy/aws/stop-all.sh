#!/usr/bin/env bash
# ============================================================================
# NÃO TESTADO: revise antes de rodar; cobra da sua conta.
# ============================================================================
# Parando as 3 instancias do HospedaSync (tag Project=hospedasync) para economizar credito.
# DRY_RUN=1 apenas imprime os comandos.
set -euo pipefail
: "${AWS_REGION:=us-east-1}"
: "${DRY_RUN:=0}"
export AWS_DEFAULT_REGION="$AWS_REGION"

echo "+ aws ec2 describe-instances (Project=hospedasync, estado: running,pending)" >&2
if [ "$DRY_RUN" = 1 ]; then IDS="<ids-das-3-instancias>"; else
  IDS=$(aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=hospedasync" "Name=instance-state-name,Values=running,pending" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
fi
[ -n "$IDS" ] || { echo "Nenhuma instancia no estado esperado." >&2; exit 0; }

echo "+ aws ec2 stop-instances --instance-ids $IDS" >&2
echo "+ aws ec2 wait instance-stopped --instance-ids $IDS" >&2
if [ "$DRY_RUN" != 1 ]; then
  aws ec2 stop-instances --instance-ids $IDS >/dev/null
  aws ec2 wait instance-stopped --instance-ids $IDS
fi
