# LocalDesk for a training room: launch, run, tear down

The demo is **not running** between trainings. It is launched for a session and torn down after
it; nothing is left on AWS in between except the free `localdesk` key pair. Current state and
history: [Deployment log](#deployment-log) at the bottom.

```bash
bash deploy/launch.sh       # ~8 min → https://desk.barcik.training, prints the student login
bash deploy/teardown.sh     # prints what the room spent, then removes everything
```

## What you get

One t3.small (2 GB RAM, Ubuntu 24.04, 16 GB gp3) in eu-central-1. The app runs under systemd on
`127.0.0.1:7860`; Caddy in front terminates HTTPS (Let's Encrypt). Ollama runs on the box for the
`nomic-embed-text` embeddings only; every LLM call goes to OpenAI (`config.yaml → mode: openai`),
so the server needs `OPENAI_API_KEY` in `.env`.

- **URL:** https://desk.barcik.training. A branded name on purpose: on 2026-11-11 corporate
  laptops could not open the `sslip.io` address (proxies block dynamic-DNS names), the
  barcik.training name worked from company networks. The `<ip-with-dashes>.sslip.io` name is
  served too as a fallback that needs no DNS.
- **Login:** the app's own sign-in page (`app/auth.py`), switched on by `LOGIN_ENABLED=1`, which
  `setup.sh` adds to the server's `.env` only. Local runs stay open. A signed cookie covers every
  route and the `/ws` WebSocket. Not Caddy basic auth: Safari does not send basic-auth
  credentials on WebSocket handshakes, and without `/ws` the ops room is dead and the human gate
  fails closed.
- **Accounts:** `student` / `APP_PASSWORD` (handed to the room, new on every launch) and
  `robert` / `ADMIN_PASSWORD`; both live in the gitignored `.env` in the repo root.
- **HTTPS is not optional:** the voice orb needs a secure origin for the microphone.

## Assets, and where each one lives

| Asset | Where | Notes |
|---|---|---|
| `launch.sh` | here | everything from nothing to a verified login page |
| `teardown.sh` | here | finds resources by name/tag, needs no IDs; used for the 2026-11-12 teardown |
| `setup.sh` | here, runs **on the box** | idempotent host setup: swap, apt, Ollama + embedding model, venv, seed DB, ingest KB, systemd, Caddy. Re-running it **re-seeds the DB** (wipes the room's tickets) |
| `rotate_student_password.py` | here | new `word-word-1234` password into `.env`, old file kept as `.env.bak` |
| `Caddyfile` | here | template, `{HOST}` may be a comma-separated list; `flush_interval -1` so SSE chat streams token by token |
| `localdesk.service` | here | systemd unit, user `desk` |
| AWS profile `mim-lab` | `~/.aws` | EC2. Its `mim-lab-guardrails` policy denies anything above **t3.small**, volumes over 30 GB, other regions and Route 53. Stay inside it |
| AWS profile `barcik-demos` | `~/.aws` | Route 53 only (zone `Z03933173SC9BZRSZEHBP`, barcik.training). It has no EC2 rights |
| Key pair `localdesk` | AWS + `~/.ssh/localdesk-ec2` | kept across launches. Lost the private half? `ssh-keygen -t ed25519 -N "" -f ~/.ssh/localdesk-ec2`, delete the AWS key pair, `aws ec2 import-key-pair --key-name localdesk --public-key-material fileb://~/.ssh/localdesk-ec2.pub` |
| `.env` | repo root, gitignored | `OPENAI_API_KEY`, `APP_PASSWORD`, `ADMIN_PASSWORD` (`OPENROUTER_API_KEY` only for `mode: cloud`) |

Created per launch and removed by `teardown.sh`: the instance (tag `Name=localdesk`) with its
root volume, one Elastic IP (tag `Name=localdesk`), the security group `localdesk` (80/443 from
anywhere, 22 only from the IP this Mac had at launch) and the A record `desk.barcik.training`
(TTL 60).

## Before the room gets the login

1. One voice call from a real browser on the public URL (the headless check below does not
   cover the microphone path).
2. A spend cap on the OpenAI account. The API key never reaches the browser, but anyone with the
   password can spend on it: voice is about 0.05 USD per minute per person.
3. Say it out loud: conversations and voice transcripts are logged on the box, nothing personal.

## While it is up

```bash
IP=$(aws ec2 describe-instances --profile mim-lab --region eu-central-1 \
     --filters Name=tag:Name,Values=localdesk Name=instance-state-name,Values=running \
     --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

# ssh times out? This Mac's IP changed since launch (other Wi-Fi). Re-authorize it:
SG=$(aws ec2 describe-security-groups --profile mim-lab --region eu-central-1 \
     --filters Name=group-name,Values=localdesk --query 'SecurityGroups[0].GroupId' --output text)
aws ec2 authorize-security-group-ingress --profile mim-lab --region eu-central-1 \
    --group-id $SG --protocol tcp --port 22 --cidr "$(curl -s https://checkip.amazonaws.com)/32"

# push a code change. The tree belongs to `desk` after setup, hence sudo rsync + chown.
rsync -az --rsync-path="sudo rsync" -e "ssh -i ~/.ssh/localdesk-ec2" --exclude venv --exclude .venv \
  --exclude .git --exclude .env --exclude '.env.bak*' --exclude vectorstore --exclude logs \
  --exclude 'data/db/*.db*' --exclude __pycache__ --exclude .DS_Store ./ ubuntu@$IP:/opt/localdesk/
ssh -i ~/.ssh/localdesk-ec2 ubuntu@$IP 'sudo chown -R desk:desk /opt/localdesk && sudo systemctl restart localdesk'
# (a restart drops in-memory conversations; tickets and metrics in SQLite survive)

# logs, health, spend
ssh -i ~/.ssh/localdesk-ec2 ubuntu@$IP 'sudo journalctl -u localdesk -n 50 --no-pager'
ssh -i ~/.ssh/localdesk-ec2 ubuntu@$IP "sudo sqlite3 /opt/localdesk/data/db/localdesk.db \
  'select role,model,count(*),round(sum(cost_usd),4) from request_metrics group by 1,2'"
ssh -i ~/.ssh/localdesk-ec2 ubuntu@$IP 'cd /opt/localdesk && sudo -u desk .venv/bin/python \
  scripts/live_smoke.py "what is the SLA response time for a gold customer with a critical incident?"'

# signed-in API call from the Mac
curl -s -c /tmp/mu.jar -o /dev/null -d username=robert --data-urlencode "password=$ADMIN_PASSWORD" https://desk.barcik.training/login
curl -s -b /tmp/mu.jar https://desk.barcik.training/api/status | jq
```

Expected from outside: `/` → 303 to `/login`, `/api/*` → 401, `/ws` without cookie → 403, port
7860 unreachable.

## How a full room behaves

- **Shared by everyone:** simulation, sentinel threads, incidents, the server fleet, charts and
  the live/realtime voice switch (one person's shift-click on the orb or a storm changes it for
  all). That is the ops room; tell the room before someone terminates a server.
- **Per browser:** conversations, voice calls and approval windows. `/ws` broadcasts to every
  browser, so the UI drops per-person pushes it does not own (`isMySession` in
  `static/index.html`). Before that fix one person hanging up ended everybody's call.
- **Capacity:** on 2026-11-11 the box sat at ~620 MB of 1.9 GB RAM, swap untouched, no OOM, no
  service restart. That was light use (see the log); a room of 20 all on voice is untested, but
  voice audio flows browser ↔ OpenAI directly, the box only runs the tool pipeline.
- **Voice on corporate networks:** WebRTC to OpenAI needs outbound UDP. If the page opens but
  voice does not connect on a company laptop, that is their network, not the box.

## Cost

About 0.03 USD per hour while up (t3.small + Elastic IP + 16 GB gp3), roughly 23 USD if left
on for a month. Zero when torn down; the DNS zone is paid for anyway. LLM spend is separate, on
the OpenAI account: text turns are fractions of a cent, voice ~0.05 USD/min, a running
simulation calls the sentinel every 25 s.

## Deployment log

| Date | What | Notes |
|---|---|---|
| 2026-09-16 | first deploy, `https://18-198-245-243.sslip.io`, Caddy basic auth, cloud mode (OpenRouter) | for 1-2 participants between trainings; instance was gone by 2026-09-30 |
| 2026-11-11 | launched for a training room, `i-003b37e458553a85d`, 3.71.243.226, `desk.barcik.training` added mid-session | app login form and per-browser push scoping written that day (commit `7819db3`). Up ~18 h. Recorded on the box: 18 agent + 18 judge calls on gpt-6-luna and one 15 s gpt-live-1 call, 0.036 USD in total (includes the deploy checks). No errors, no restarts |
| 2026-11-12 | torn down with `teardown.sh` | instance, volume, Elastic IP `eipalloc-0aee1017cb4fc5e40`, SG `sg-07b2081fa4a485f49` and the DNS record removed; verified empty. Key pair kept |

`launch.sh` was assembled from the exact commands run by hand on 2026-11-11 and has passed a
syntax check only; its first real run is the next training. If a step fails, the manual
sequence is the script itself, top to bottom.
