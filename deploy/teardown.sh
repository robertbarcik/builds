#!/usr/bin/env bash
# Take the LocalDesk demo off AWS completely: instance (+ its root volume), Elastic IP,
# security group and the desk.barcik.training DNS record. Finds everything by name/tag,
# so it needs no IDs. Keeps the `localdesk` key pair (free, reused by launch.sh).
#
#     bash deploy/teardown.sh          # prints the spend summary first when the box is reachable
#
# Safe to re-run: every step skips what is already gone.
set -uo pipefail

PROFILE=${PROFILE:-mim-lab}            # EC2 (guardrails: t3.small max, no Route 53)
DNS_PROFILE=${DNS_PROFILE:-barcik-demos}   # Route 53 for barcik.training
REGION=${REGION:-eu-central-1}
ZONE=${ZONE:-Z03933173SC9BZRSZEHBP}
NAME=${NAME:-desk.barcik.training}
KEY=${KEY:-$HOME/.ssh/localdesk-ec2}
ec2() { aws ec2 --profile "$PROFILE" --region "$REGION" "$@"; }

IDS=$(ec2 describe-instances --filters "Name=tag:Name,Values=localdesk" \
      "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query 'Reservations[].Instances[].InstanceId' --output text)
echo "== instances: ${IDS:-none}"

# Elastic IPs: tagged by launch.sh, or attached to one of the instances
ALLOCS=$(ec2 describe-addresses --filters "Name=tag:Name,Values=localdesk" \
         --query 'Addresses[].AllocationId' --output text)
for id in $IDS; do
    ALLOCS="$ALLOCS $(ec2 describe-addresses --filters "Name=instance-id,Values=$id" \
             --query 'Addresses[].AllocationId' --output text)"
done
ALLOCS=$(echo $ALLOCS | tr ' ' '\n' | sort -u | tr '\n' ' ')
echo "== elastic IPs: ${ALLOCS:-none}"

# last look at what the room spent (best effort: ssh is only open to the launch-day IP)
for id in $IDS; do
    IP=$(ec2 describe-instances --instance-ids "$id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
    echo "== spend on $IP (role | model | calls | USD)"
    ssh -i "$KEY" -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "ubuntu@$IP" \
        "sudo sqlite3 /opt/localdesk/data/db/localdesk.db 'select role,model,count(*),round(sum(cost_usd),4) from request_metrics group by 1,2'" \
        2>/dev/null || echo "   (box not reachable over ssh, skipped)"
done

if [ -n "$IDS" ]; then
    echo "== terminating"
    ec2 terminate-instances --instance-ids $IDS --query 'TerminatingInstances[].[InstanceId,CurrentState.Name]' --output text
    ec2 wait instance-terminated --instance-ids $IDS
fi

for a in $ALLOCS; do
    echo "== releasing $a"
    ec2 release-address --allocation-id "$a"
done

SG=$(ec2 describe-security-groups --filters "Name=group-name,Values=localdesk" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
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
ec2 describe-instances --filters "Name=tag:Name,Values=localdesk" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text
ec2 describe-addresses --query 'Addresses[].[AllocationId,PublicIp]' --output text
ec2 describe-security-groups --filters "Name=group-name,Values=localdesk" --query 'SecurityGroups[].GroupId' --output text
echo "== done. Rotate nothing by hand: launch.sh issues a new student password next time."
