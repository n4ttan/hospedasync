#!/usr/bin/env bash
# ============================================================================
# NÃO TESTADO: revise antes de rodar; cobra da sua conta.
# ============================================================================
# TEMPLATE de provisionamento do HospedaSync na AWS (us-east-1, VPC padrao).
# Cria: par de chaves, 2 Security Groups (edge, workers) e 3 instancias EC2.
# DRY_RUN=1 apenas imprime os comandos (nada e criado).
#
# Uso:   MY_IP=203.0.113.7/32 DRY_RUN=1 bash deploy/aws/provision.sh
#        MY_IP=203.0.113.7/32 bash deploy/aws/provision.sh
# Requer: aws cli v2 autenticado (perfil/variaveis AWS_*), curl.
set -euo pipefail

# ---- Parametros (sobrescreva por variavel de ambiente) ----------------------
: "${AWS_REGION:=us-east-1}"
: "${KEY_NAME:=hospeda-key}"
: "${KEY_FILE:=$HOME/.ssh/${KEY_NAME}.pem}"
: "${EDGE_TYPE:=t3.small}"       # edge roda nginx+gateway+rabbitmq+redis
: "${WA_TYPE:=t3.small}"         # 2 workers + postgres primario
: "${WB_TYPE:=t3.small}"         # worker + replica (t3.micro pode faltar memoria)
: "${VOLUME_GB:=16}"             # EBS gp3 por instancia
: "${AMI_PARAM:=/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64}"
: "${SG_EDGE_NAME:=hospeda-edge-sg}"
: "${SG_WORKERS_NAME:=hospeda-workers-sg}"
: "${DRY_RUN:=0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_DATA="${USER_DATA:-$SCRIPT_DIR/user-data.sh}"

if [ -z "${MY_IP:-}" ]; then
  # SSH so do seu IP. Em DRY_RUN nao consulta a internet.
  if [ "$DRY_RUN" = 1 ]; then MY_IP="<SEU_IP>/32"; else MY_IP="$(curl -fsS https://checkip.amazonaws.com)/32"; fi
fi
export AWS_DEFAULT_REGION="$AWS_REGION"

# run: imprime o comando; em DRY_RUN nao executa e devolve um valor ficticio na captura
run() {
  echo "+ aws $*" >&2
  if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN-ID"; return 0; fi
  aws "$@"
}

echo "Regiao: $AWS_REGION | SSH liberado para: $MY_IP | DRY_RUN=$DRY_RUN" >&2

# ---- VPC padrao e subnet ----------------------------------------------------
VPC_ID=$(run ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
# Todas as instancias na MESMA subnet (mesma AZ): trafego privado sem custo entre AZs
SUBNET_ID=$(run ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID" Name=default-for-az,Values=true \
  --query 'Subnets[0].SubnetId' --output text)

# ---- Par de chaves (a chave privada so aparece uma vez; fica em KEY_FILE) ---
if [ "$DRY_RUN" = 1 ]; then
  echo "+ aws ec2 create-key-pair --key-name $KEY_NAME --query KeyMaterial --output text > $KEY_FILE && chmod 400 $KEY_FILE" >&2
elif aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  echo "Par de chaves $KEY_NAME ja existe em $AWS_REGION: reutilizando (arquivo local: $KEY_FILE)" >&2
  [ -f "$KEY_FILE" ] || { echo "ERRO: $KEY_FILE nao existe; copie a chave privada para la" >&2; exit 1; }
else
  mkdir -p "$(dirname "$KEY_FILE")"
  aws ec2 create-key-pair --key-name "$KEY_NAME" --query KeyMaterial --output text > "$KEY_FILE"
  chmod 400 "$KEY_FILE"
fi

# ---- Security Groups ---------------------------------------------------------
SG_EDGE=$(run ec2 create-security-group --group-name "$SG_EDGE_NAME" \
  --description "HospedaSync edge" --vpc-id "$VPC_ID" --query GroupId --output text)
SG_WORKERS=$(run ec2 create-security-group --group-name "$SG_WORKERS_NAME" \
  --description "HospedaSync workers e bancos" --vpc-id "$VPC_ID" --query GroupId --output text)

# edge: 80/443 ao mundo; 22 so do seu IP
run ec2 authorize-security-group-ingress --group-id "$SG_EDGE" --protocol tcp --port 80  --cidr 0.0.0.0/0 >/dev/null
run ec2 authorize-security-group-ingress --group-id "$SG_EDGE" --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null
run ec2 authorize-security-group-ingress --group-id "$SG_EDGE" --protocol tcp --port 22  --cidr "$MY_IP"  >/dev/null
# edge: gateway (8000), RabbitMQ (5672) e Redis (6379) so a partir dos workers
for p in 8000 5672 6379; do
  run ec2 authorize-security-group-ingress --group-id "$SG_EDGE" --protocol tcp --port "$p" --source-group "$SG_WORKERS" >/dev/null
done

# workers: 22 so do seu IP; workers (8001-8003) e Postgres (5432) so do edge e entre si
run ec2 authorize-security-group-ingress --group-id "$SG_WORKERS" --protocol tcp --port 22 --cidr "$MY_IP" >/dev/null
for src in "$SG_EDGE" "$SG_WORKERS"; do
  run ec2 authorize-security-group-ingress --group-id "$SG_WORKERS" --protocol tcp --port 8001-8003 --source-group "$src" >/dev/null
  run ec2 authorize-security-group-ingress --group-id "$SG_WORKERS" --protocol tcp --port 5432      --source-group "$src" >/dev/null
done
# 15672 (painel RabbitMQ) NAO e aberto: acesse por tunel SSH (ssh -L 15672:localhost:15672 ...)

# ---- Instancias --------------------------------------------------------------
launch() {  # launch <nome> <tipo> <sg>
  run ec2 run-instances \
    --image-id "resolve:ssm:$AMI_PARAM" \
    --instance-type "$2" \
    --key-name "$KEY_NAME" \
    --subnet-id "$SUBNET_ID" \
    --security-group-ids "$3" \
    --associate-public-ip-address \
    --user-data "file://$USER_DATA" \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,DeleteOnTermination=true}" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$1},{Key=Project,Value=hospedasync}]" \
    --query 'Instances[0].InstanceId' --output text
}
ID_EDGE=$(launch hospeda-edge      "$EDGE_TYPE" "$SG_EDGE")
ID_WA=$(launch   hospeda-workers-a "$WA_TYPE"   "$SG_WORKERS")
ID_WB=$(launch   hospeda-workers-b "$WB_TYPE"   "$SG_WORKERS")

run ec2 wait instance-running --instance-ids "$ID_EDGE" "$ID_WA" "$ID_WB"

# ---- Enderecos para preencher o .env.aws e acessar por SSH -------------------
echo >&2; echo "Instancias (Name / IP privado / IP publico):" >&2
run ec2 describe-instances --instance-ids "$ID_EDGE" "$ID_WA" "$ID_WB" \
  --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,PrivateIpAddress,PublicIpAddress]' --output text
echo "Proximo passo: preencher EDGE_IP/WA_IP/WB_IP (privados) em .env.aws e seguir deploy/aws/README.md" >&2
echo "Obs.: o IP publico muda ao parar/iniciar (sem Elastic IP); o certificado autoassinado precisa dele no SAN." >&2
