#!/usr/bin/env bash
# PREPARED, NOT YET RUN. Take the Signal Desk demo off AWS completely: instance (+ its root
# volume), Elastic IP, security group and the signal.barcik.training DNS record. Finds everything
# by name/tag, so it needs no IDs. Keeps the `signal-desk` key pair (free, reused by launch.sh).
#
#     bash deploy/teardown.sh          # prints the spend summary first when the box is reachable
#
# Safe to re-run: every step skips what is already gone.
set -uo pipefail

PROFILE=${PROFILE:-mim-lab}                # EC2 (guardrails: t3.small max, no Route 53)
DNS_PROFILE=${DNS_PROFILE:-barcik-demos}   # Route 53 for barcik.training
REGION=${REGION:-eu-central-1}
ZONE=${ZONE:-Z03933173SC9BZRSZEHBP}
NAME=${NAME:-signal.barcik.training}
TAG=${TAG:-signal-desk}
KEY=${KEY:-$HOME/.ssh/signal-desk-ec2}
ec2() { aws ec2 --profile "$PROFILE" --region "$REGION" "$@"; }

IDS=$(ec2 describe-instances --filters "Name=tag:Name,Values=$TAG" \
      "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query 'Reservations[].Instances[].InstanceId' --output text)
echo "== instances: ${IDS:-none}"

ALLOCS=$(ec2 describe-addresses --filters "Name=tag:Name,Values=$TAG" \
         --query 'Addresses[].AllocationId' --output text)
for id in $IDS; do
    ALLOCS="$ALLOCS $(ec2 describe-addresses --filters "Name=instance-id,Values=$id" \
             --query 'Addresses[].AllocationId' --output text)"
done
ALLOCS=$(echo $ALLOCS | tr ' ' '\n' | sort -u | tr '\n' ' ')
echo "== elastic IPs: ${ALLOCS:-none}"

# last look at what the visitors spent: the app's own budget ledger (in memory, since the service
# last started), read over HTTPS with Robert's login. Best effort.
if [ -n "$IDS" ]; then
    REPO=$(cd "$(dirname "$0")/.." && pwd)
    ADMIN=$(grep -E '^ADMIN_PASSWORD=' "$REPO/.env" 2>/dev/null | cut -d= -f2- | tr -d '"')
    JAR=$(mktemp)
    echo "== AI spend recorded by the app (calls, USD, per role)"
    if curl -s -c "$JAR" -o /dev/null --max-time 10 -d username=robert --data-urlencode "password=$ADMIN" \
            "https://$NAME/login" \
       && curl -s -b "$JAR" --max-time 10 "https://$NAME/api/budget" \
            | python3 -c "import json,sys; t=json.load(sys.stdin)['totals']; print('  ', t['calls'], 'calls, USD', round(t['cost_usd'], 4), {k: round(v['cost_usd'], 4) for k, v in t['by_role'].items()})"; then
        :
    else
        echo "   (not reachable, skipped)"
    fi
    rm -f "$JAR"
fi

if [ -n "$IDS" ]; then
    echo "== terminating"
    ec2 terminate-instances --instance-ids $IDS --query 'TerminatingInstances[].[InstanceId,CurrentState.Name]' --output text
    ec2 wait instance-terminated --instance-ids $IDS
fi

for a in $ALLOCS; do
    echo "== releasing $a"
    ec2 release-address --allocation-id "$a"
done

SG=$(ec2 describe-security-groups --filters "Name=group-name,Values=$TAG" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ -n "$SG" ] && [ "$SG" != "None" ]; then
    echo "== deleting security group $SG"
    for _ in 1 2 3 4 5 6; do
        ec2 delete-security-group --group-id "$SG" >/dev/null 2>&1 && break
        sleep 5
    done
fi

REC=$(aws route53 list-resource-record-sets --profile "$DNS_PROFILE" --hosted-zone-id "$ZONE" \
      --query "ResourceRecordSets[?Name=='$NAME.' && Type=='A'] | [0]" --output json)
if [ -n "$REC" ] && [ "$REC" != "null" ]; then
    echo "== deleting DNS record $NAME"
    aws route53 change-resource-record-sets --profile "$DNS_PROFILE" --hosted-zone-id "$ZONE" \
        --change-batch "$(printf '{"Changes":[{"Action":"DELETE","ResourceRecordSet":%s}]}' "$REC")" \
        --query 'ChangeInfo.Status' --output text
fi

echo "== what is left (all three lines should be empty)"
ec2 describe-instances --filters "Name=tag:Name,Values=$TAG" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text
ec2 describe-addresses --filters "Name=tag:Name,Values=$TAG" --query 'Addresses[].[AllocationId,PublicIp]' --output text
ec2 describe-security-groups --filters "Name=group-name,Values=$TAG" --query 'SecurityGroups[].GroupId' --output text
echo "== done. launch.sh issues a new guest password next time."
