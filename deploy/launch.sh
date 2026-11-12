#!/usr/bin/env bash
# One command from nothing to https://desk.barcik.training for a training room (~8 minutes):
# fresh student password, security group, t3.small, Elastic IP, DNS record, code + .env,
# host setup, and a check that the login page answers. Counterpart: deploy/teardown.sh.
#
#     bash deploy/launch.sh                  # new student password each launch
#     KEEP_PASSWORD=1 bash deploy/launch.sh  # keep APP_PASSWORD from .env
#
# Needs on this Mac: AWS profiles mim-lab (EC2) and barcik-demos (Route 53), the key pair
# `localdesk` with its private half in ~/.ssh/localdesk-ec2, and a .env in the repo root with
# OPENAI_API_KEY, APP_PASSWORD, ADMIN_PASSWORD (OPENROUTER_API_KEY only for mode: cloud).
set -euo pipefail

PROFILE=${PROFILE:-mim-lab}            # guardrail policy: t3.small is the largest allowed type
DNS_PROFILE=${DNS_PROFILE:-barcik-demos}
REGION=${REGION:-eu-central-1}
ZONE=${ZONE:-Z03933173SC9BZRSZEHBP}    # barcik.training
NAME=${NAME:-desk.barcik.training}
KEY=${KEY:-$HOME/.ssh/localdesk-ec2}
REPO=$(cd "$(dirname "$0")/.." && pwd)
ec2() { aws ec2 --profile "$PROFILE" --region "$REGION" "$@"; }
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8)

test -f "$REPO/.env" || { echo ".env missing in $REPO"; exit 1; }
test -f "$KEY" || { echo "private key $KEY missing (see README: key pair)"; exit 1; }
RUNNING=$(ec2 describe-instances --filters "Name=tag:Name,Values=localdesk" \
          "Name=instance-state-name,Values=pending,running,stopping,stopped" \
          --query 'Reservations[].Instances[].InstanceId' --output text)
[ -z "$RUNNING" ] || { echo "already up: $RUNNING (run deploy/teardown.sh first)"; exit 1; }

if [ "${KEEP_PASSWORD:-0}" != "1" ]; then
    echo "== new student password"
    python3 "$REPO/deploy/rotate_student_password.py" >/dev/null
fi

echo "== security group (80/443 public, 22 from this Mac's current IP only)"
MYIP=$(curl -s https://checkip.amazonaws.com)
SG=$(ec2 describe-security-groups --filters "Name=group-name,Values=localdesk" --query 'SecurityGroups[0].GroupId' --output text)
if [ -z "$SG" ] || [ "$SG" = "None" ]; then
    VPC=$(ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
    SG=$(ec2 create-security-group --group-name localdesk --description "LocalDesk demo" --vpc-id "$VPC" --query GroupId --output text)
    ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
        "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MYIP/32}]" \
        'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]' \
        'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]' >/dev/null
fi

echo "== instance (latest Ubuntu 24.04, t3.small, 16 GB gp3)"
AMI=$(ec2 describe-images --owners 099720109477 \
      --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
ID=$(ec2 run-instances --image-id "$AMI" --instance-type t3.small --key-name localdesk \
     --security-group-ids "$SG" --metadata-options HttpTokens=required \
     --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=16,VolumeType=gp3}' \
     --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=localdesk}]' \
     --query 'Instances[0].InstanceId' --output text)
ec2 wait instance-running --instance-ids "$ID"
ALLOC=$(ec2 allocate-address --domain vpc \
        --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=localdesk}]' \
        --query AllocationId --output text)
ec2 associate-address --instance-id "$ID" --allocation-id "$ALLOC" >/dev/null
IP=$(ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text)
echo "   $ID  $IP"

echo "== DNS $NAME -> $IP"
aws route53 change-resource-record-sets --profile "$DNS_PROFILE" --hosted-zone-id "$ZONE" --change-batch \
    "{\"Comment\":\"LocalDesk demo instance, temporary\",\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"$NAME\",\"Type\":\"A\",\"TTL\":60,\"ResourceRecords\":[{\"Value\":\"$IP\"}]}}]}" \
    --query 'ChangeInfo.Status' --output text

echo "== waiting for ssh"
for _ in $(seq 1 30); do
    "${SSH[@]}" "ubuntu@$IP" 'sudo mkdir -p /opt/localdesk && sudo chown ubuntu /opt/localdesk' 2>/dev/null && break
    sleep 6
done

echo "== code + .env, then host setup (apt, Ollama embeddings, venv, seed, ingest, Caddy): ~5 min"
rsync -az -e "ssh -i $KEY" --exclude venv --exclude .venv --exclude .git --exclude .env --exclude '.env.bak*' \
    --exclude vectorstore --exclude logs --exclude 'data/db/*.db*' --exclude __pycache__ --exclude .DS_Store \
    "$REPO/" "ubuntu@$IP:/opt/localdesk/"
scp -q -i "$KEY" "$REPO/.env" "ubuntu@$IP:/opt/localdesk/.env"
# both names: the branded one for the room, sslip.io as a fallback that needs no DNS
"${SSH[@]}" "ubuntu@$IP" "sudo HOST='$NAME, ${IP//./-}.sslip.io' bash /opt/localdesk/deploy/setup.sh" | tail -6

echo "== waiting for the certificate and the login page"
CODE=000
for _ in $(seq 1 30); do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$NAME/login" || true)
    [ "$CODE" = "200" ] && break
    sleep 5
done
echo
echo "   https://$NAME   (login page: HTTP $CODE)"
echo "   username: student"
echo "   password: $(grep -E '^APP_PASSWORD=' "$REPO/.env" | cut -d= -f2- | tr -d '"')"
echo "   Robert:   robert / ADMIN_PASSWORD from .env"
echo "   Before the room gets it: one voice call from a real browser, spend cap on the OpenAI account."
