#!/usr/bin/env bash
# Deploy mcp-call a ECS Fargate con ALB
# Uso: ./deploy-ecs.sh
# Requiere: aws cli v2, jq

set -euo pipefail

# ── Configuración ────────────────────────────────────────────────────────────
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
APP_NAME="mcp-call"
ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${APP_NAME}"
CLUSTER="${APP_NAME}-cluster"
SERVICE="${APP_NAME}-service"
TASK_FAMILY="${APP_NAME}-task"
LOG_GROUP="/ecs/${APP_NAME}"
CONTAINER_PORT=8000

echo "==> Account: ${AWS_ACCOUNT_ID} | Region: ${AWS_REGION}"

# ── 1. ECR: crear repo y subir imagen ────────────────────────────────────────
echo "==> Creando repositorio ECR (si no existe)..."
aws ecr describe-repositories --repository-names "${APP_NAME}" --region "${AWS_REGION}" 2>/dev/null \
  || aws ecr create-repository --repository-name "${APP_NAME}" --region "${AWS_REGION}"

echo "==> Login a ECR..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "==> Build y push de imagen..."
docker build -t "${APP_NAME}" .
docker tag "${APP_NAME}:latest" "${ECR_REPO}:latest"
docker push "${ECR_REPO}:latest"

# ── 2. Secrets Manager: guardar variables sensibles ──────────────────────────
echo "==> Creando secrets en Secrets Manager (si no existen)..."

put_secret() {
  local name="$1" value="$2"
  local full_name="${APP_NAME}/${name}"
  if aws secretsmanager describe-secret --secret-id "${full_name}" --region "${AWS_REGION}" &>/dev/null; then
    aws secretsmanager put-secret-value --secret-id "${full_name}" --secret-string "${value}" --region "${AWS_REGION}" > /dev/null
  else
    aws secretsmanager create-secret --name "${full_name}" --secret-string "${value}" --region "${AWS_REGION}" > /dev/null
  fi
  echo "  [ok] ${full_name}"
}

# Lee del .env local si existe, o pide al usuario
load_env() {
  local key="$1"
  if [ -f .env ]; then
    local val
    val=$(grep -E "^${key}=" .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    [ -n "$val" ] && echo "$val" && return
  fi
  read -rsp "  Ingresa ${key}: " val; echo
  echo "$val"
}

for var in ANTHROPIC_API_KEY TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN \
           TWILIO_PHONE_NUMBER YOUR_PHONE_NUMBER \
           ELEVENLABS_API_KEY ELEVENLABS_VOICE_ID DATABASE_URL \
           TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_ID; do
  val=$(load_env "$var")
  [ -n "$val" ] && put_secret "$var" "$val"
done

# ── 3. IAM Role para la task ─────────────────────────────────────────────────
TASK_ROLE="${APP_NAME}-task-role"
EXEC_ROLE="${APP_NAME}-exec-role"

echo "==> Creando IAM roles..."

aws iam get-role --role-name "${EXEC_ROLE}" &>/dev/null || aws iam create-role \
  --role-name "${EXEC_ROLE}" \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > /dev/null

aws iam attach-role-policy --role-name "${EXEC_ROLE}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy 2>/dev/null || true

# Permiso para leer secrets
aws iam put-role-policy --role-name "${EXEC_ROLE}" \
  --policy-name "read-secrets" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [\"secretsmanager:GetSecretValue\"],
      \"Resource\": \"arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${APP_NAME}/*\"
    }]
  }"

aws iam get-role --role-name "${TASK_ROLE}" &>/dev/null || aws iam create-role \
  --role-name "${TASK_ROLE}" \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > /dev/null

EXEC_ROLE_ARN=$(aws iam get-role --role-name "${EXEC_ROLE}" --query Role.Arn --output text)
TASK_ROLE_ARN=$(aws iam get-role --role-name "${TASK_ROLE}" --query Role.Arn --output text)

# ── 4. CloudWatch log group ──────────────────────────────────────────────────
aws logs create-log-group --log-group-name "${LOG_GROUP}" --region "${AWS_REGION}" 2>/dev/null || true

# ── 5. Task Definition ───────────────────────────────────────────────────────
echo "==> Registrando Task Definition..."

secret_arn() {
  local name="$1"
  aws secretsmanager describe-secret \
    --secret-id "${APP_NAME}/${name}" \
    --region "${AWS_REGION}" \
    --query ARN --output text 2>/dev/null || true
}

# Construye el array de secrets solo para los que existen
SECRETS_JSON=""
for var in ANTHROPIC_API_KEY TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN \
           TWILIO_PHONE_NUMBER YOUR_PHONE_NUMBER \
           ELEVENLABS_API_KEY ELEVENLABS_VOICE_ID DATABASE_URL \
           TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_ID SERVER_URL; do
  arn=$(secret_arn "$var")
  [ -z "$arn" ] && continue
  SECRETS_JSON="${SECRETS_JSON},{\"name\":\"${var}\",\"valueFrom\":\"${arn}\"}"
done
SECRETS_JSON="[${SECRETS_JSON:1}]"  # quita la coma inicial

aws ecs register-task-definition \
  --family "${TASK_FAMILY}" \
  --network-mode awsvpc \
  --requires-compatibilities FARGATE \
  --cpu "512" \
  --memory "1024" \
  --execution-role-arn "${EXEC_ROLE_ARN}" \
  --task-role-arn "${TASK_ROLE_ARN}" \
  --container-definitions "[{
    \"name\": \"${APP_NAME}\",
    \"image\": \"${ECR_REPO}:latest\",
    \"portMappings\": [{\"containerPort\": ${CONTAINER_PORT}, \"protocol\": \"tcp\"}],
    \"secrets\": ${SECRETS_JSON},
    \"logConfiguration\": {
      \"logDriver\": \"awslogs\",
      \"options\": {
        \"awslogs-group\": \"${LOG_GROUP}\",
        \"awslogs-region\": \"${AWS_REGION}\",
        \"awslogs-stream-prefix\": \"ecs\"
      }
    },
    \"essential\": true
  }]" > /dev/null

echo "==> Task Definition registrada."

# ── 6. Networking: VPC default ──────────────────────────────────────────────
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query "Vpcs[0].VpcId" --output text --region "${AWS_REGION}")
SUBNET_IDS=$(aws ec2 describe-subnets --filters "Name=vpcId,Values=${VPC_ID}" \
  --query "Subnets[*].SubnetId" --output text --region "${AWS_REGION}" | tr '\t' ',')
echo "  VPC: ${VPC_ID} | Subnets: ${SUBNET_IDS}"

# Security group para la app
SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${APP_NAME}-sg" "Name=vpc-id,Values=${VPC_ID}" \
  --query "SecurityGroups[0].GroupId" --output text --region "${AWS_REGION}" 2>/dev/null || echo "None")

if [ "${SG_ID}" = "None" ] || [ -z "${SG_ID}" ]; then
  SG_ID=$(aws ec2 create-security-group \
    --group-name "${APP_NAME}-sg" \
    --description "mcp-call ECS security group" \
    --vpc-id "${VPC_ID}" \
    --region "${AWS_REGION}" \
    --query GroupId --output text)
  # Permitir tráfico desde el ALB (puerto 8000) y salida total
  aws ec2 authorize-security-group-ingress --group-id "${SG_ID}" \
    --protocol tcp --port "${CONTAINER_PORT}" --cidr 0.0.0.0/0 --region "${AWS_REGION}" > /dev/null
  aws ec2 authorize-security-group-egress --group-id "${SG_ID}" \
    --protocol -1 --cidr 0.0.0.0/0 --region "${AWS_REGION}" 2>/dev/null || true
fi
echo "  Security Group: ${SG_ID}"

# ── 7. ALB ───────────────────────────────────────────────────────────────────
echo "==> Creando ALB..."
ALB_ARN=$(aws elbv2 describe-load-balancers --names "${APP_NAME}-alb" \
  --query "LoadBalancers[0].LoadBalancerArn" --output text --region "${AWS_REGION}" 2>/dev/null || true)

if [ -z "${ALB_ARN}" ] || [ "${ALB_ARN}" = "None" ]; then
  ALB_ARN=$(aws elbv2 create-load-balancer \
    --name "${APP_NAME}-alb" \
    --subnets $(echo "${SUBNET_IDS}" | tr ',' ' ') \
    --security-groups "${SG_ID}" \
    --scheme internet-facing \
    --type application \
    --region "${AWS_REGION}" \
    --query "LoadBalancers[0].LoadBalancerArn" --output text)
fi

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --load-balancer-arns "${ALB_ARN}" \
  --query "LoadBalancers[0].DNSName" --output text --region "${AWS_REGION}")
echo "  ALB DNS: ${ALB_DNS}"

# Target group
TG_ARN=$(aws elbv2 describe-target-groups --names "${APP_NAME}-tg" \
  --query "TargetGroups[0].TargetGroupArn" --output text --region "${AWS_REGION}" 2>/dev/null || true)

if [ -z "${TG_ARN}" ] || [ "${TG_ARN}" = "None" ]; then
  TG_ARN=$(aws elbv2 create-target-group \
    --name "${APP_NAME}-tg" \
    --protocol HTTP \
    --port "${CONTAINER_PORT}" \
    --vpc-id "${VPC_ID}" \
    --target-type ip \
    --health-check-path "/" \
    --health-check-interval-seconds 30 \
    --healthy-threshold-count 2 \
    --region "${AWS_REGION}" \
    --query "TargetGroups[0].TargetGroupArn" --output text)
fi

# Listener HTTP (port 80) — para HTTPS necesitas un certificado ACM
aws elbv2 describe-listeners --load-balancer-arn "${ALB_ARN}" \
  --query "Listeners[?Port==\`80\`]" --output text --region "${AWS_REGION}" | grep -q . \
  || aws elbv2 create-listener \
    --load-balancer-arn "${ALB_ARN}" \
    --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" \
    --region "${AWS_REGION}" > /dev/null

# ── 8. ECS Cluster y Service ─────────────────────────────────────────────────
echo "==> Creando ECS Cluster..."
aws ecs describe-clusters --clusters "${CLUSTER}" --region "${AWS_REGION}" \
  --query "clusters[0].status" --output text 2>/dev/null | grep -q ACTIVE \
  || aws ecs create-cluster --cluster-name "${CLUSTER}" --region "${AWS_REGION}" > /dev/null

echo "==> Creando/actualizando ECS Service..."
if aws ecs describe-services --cluster "${CLUSTER}" --services "${SERVICE}" \
  --region "${AWS_REGION}" --query "services[0].status" --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs update-service \
    --cluster "${CLUSTER}" \
    --service "${SERVICE}" \
    --task-definition "${TASK_FAMILY}" \
    --force-new-deployment \
    --region "${AWS_REGION}" > /dev/null
  echo "  Service actualizado (force redeploy)."
else
  # Construir JSON de subnets desde lista separada por comas
  SUBNETS_JSON=$(echo "${SUBNET_IDS}" | tr ',' '\n' | sed 's/.*/"&"/' | paste -sd',')

  aws ecs create-service \
    --cluster "${CLUSTER}" \
    --service-name "${SERVICE}" \
    --task-definition "${TASK_FAMILY}" \
    --desired-count 1 \
    --launch-type FARGATE \
    --network-configuration "{\"awsvpcConfiguration\":{\"subnets\":[${SUBNETS_JSON}],\"securityGroups\":[\"${SG_ID}\"],\"assignPublicIp\":\"ENABLED\"}}" \
    --load-balancers "[{\"targetGroupArn\":\"${TG_ARN}\",\"containerName\":\"${APP_NAME}\",\"containerPort\":${CONTAINER_PORT}}]" \
    --region "${AWS_REGION}" > /dev/null
  echo "  Service creado."
fi

# ── 9. Resumen ───────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Deploy completado                                       ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  ALB URL:  http://${ALB_DNS}"
echo "║"
echo "║  Pasos manuales que quedan:"
echo "║  1. Espera ~2 min a que la task arranque (ECS console)"
echo "║  2. Actualiza SERVER_URL en Secrets Manager:"
echo "║     https://<tu-dominio>  o  http://${ALB_DNS}"
echo "║  3. Apunta tu dominio al ALB y añade certificado ACM"
echo "║     para HTTPS (Twilio y Telegram lo exigen)"
echo "║  4. Configura el webhook de Twilio en:"
echo "║     https://<tu-dominio>/twiml"
echo "╚══════════════════════════════════════════════════════════╝"
