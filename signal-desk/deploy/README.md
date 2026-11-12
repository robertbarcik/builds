# Signal Desk on a public URL: launch, run, tear down

**Status: prepared, never run.** Nothing of this kit has touched AWS yet. The scripts mirror
`localdesk/deploy/` (same accounts, guardrails, shape) and have passed `bash -n` only. The first
real launch should be watched step by step; if a step fails, the manual sequence is the script
itself, top to bottom.

```bash
bash deploy/launch.sh       # ~5 min → https://signal.barcik.training, prints the guest login
bash deploy/teardown.sh     # prints what the visitors spent, then removes everything
```

## What you get

One t3.small (2 GB RAM, Ubuntu 24.04, 12 GB gp3) in eu-central-1. The app runs under systemd on
`127.0.0.1:7870` (`signal-desk.service`, user `desk`); Caddy in front terminates HTTPS (Let's
Encrypt) and proxies with `flush_interval -1`, so the server-sent event stream (`/api/stream`)
arrives token by token. Every model call goes to OpenRouter; nothing runs on the box but Python.

- **URL:** https://signal.barcik.training, plus `<ip-with-dashes>.sslip.io` as a fallback that
  needs no DNS. A branded name on purpose: corporate laptops blocked `sslip.io` for localdesk on
  2026-11-11.
- **Login:** the app's own sign-in form (`app/auth.py`, ported from localdesk), switched on by
  `LOGIN_ENABLED=1`, which `setup.sh` adds to the server's `.env` only. Local runs stay open.
  Accounts: `guest` / `APP_PASSWORD` (handed to the customer, new on every launch) and
  `robert` / `ADMIN_PASSWORD`.
- **Public mode:** the unit sets `SIGNAL_DESK_PUBLIC=1`: the tighter caps from `config.yaml →
  public.caps` apply (USD 1.50 an hour, USD 6 a day, engineer USD 0.80 an hour and one live run
  per 5 minutes) and the developer hook `POST /api/rules` is off whatever `.env` says.
- **What a cap looks like to the visitor:** triage switches to rule-based grouping with the chip
  "AI paused (spend cap reached)"; the engineer offers the saved run. No error pages.

## Assets

| Asset | Where | Notes |
|---|---|---|
| `launch.sh` | here | from nothing to a verified login page |
| `teardown.sh` | here | finds resources by tag `Name=signal-desk`, needs no IDs |
| `setup.sh` | here, runs **on the box** | idempotent: swap, apt (python3-venv, caddy), venv, history, systemd, Caddy |
| `rotate_guest_password.py` | here | new `word-word-1234` into `.env`, old file kept as `.env.bak` |
| `Caddyfile` | here | template, `{HOST}` may be a comma-separated list |
| `signal-desk.service` | here | systemd unit, `SIGNAL_DESK_PUBLIC=1` |
| AWS profile `mim-lab` | `~/.aws` | EC2. Guardrails deny anything above **t3.small**, volumes over 30 GB, other regions and Route 53 |
| AWS profile `barcik-demos` | `~/.aws` | Route 53 only (zone `Z03933173SC9BZRSZEHBP`, barcik.training) |
| Key pair `signal-desk` | AWS + `~/.ssh/signal-desk-ec2` | **does not exist yet.** Create once: `ssh-keygen -t ed25519 -N "" -f ~/.ssh/signal-desk-ec2`, then `aws ec2 import-key-pair --profile mim-lab --region eu-central-1 --key-name signal-desk --public-key-material fileb://~/.ssh/signal-desk-ec2.pub` |
| `.env` | repo root, gitignored | `OPENROUTER_API_KEY`, `ADMIN_PASSWORD`; `APP_PASSWORD` is written by the rotation |

Created per launch and removed by `teardown.sh`: the instance with its root volume, one Elastic
IP, the security group `signal-desk` (80/443 from anywhere, 22 only from the IP this Mac had at
launch) and the A record `signal.barcik.training` (TTL 60).

## Before the customer gets the login

1. Walk the five-minute run sheet in the main README once from a real browser on the public
   URL (break something, run the engineer, accept, watch the strip, reset).
2. A credit limit on the OpenRouter key. The key never reaches the browser and the app caps its
   own spend, but a limit on the provider side is the last line.
3. Several browsers share one desk: the simulator, the board, the rules and the engineer run are
   the same for everyone. Reset demo resets it for all.

## While it is up

```bash
IP=$(aws ec2 describe-instances --profile mim-lab --region eu-central-1 \
     --filters Name=tag:Name,Values=signal-desk Name=instance-state-name,Values=running \
     --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

# push a code change (the tree belongs to `desk` after setup, hence sudo rsync + chown)
rsync -az --rsync-path="sudo rsync" -e "ssh -i ~/.ssh/signal-desk-ec2" --exclude .venv --exclude .git \
  --exclude .env --exclude '.env.bak*' --exclude logs --exclude data/state --exclude data/history.jsonl \
  --exclude __pycache__ --exclude .DS_Store ./ ubuntu@$IP:/opt/signal-desk/
ssh -i ~/.ssh/signal-desk-ec2 ubuntu@$IP 'sudo chown -R desk:desk /opt/signal-desk && sudo systemctl restart signal-desk'

# logs
ssh -i ~/.ssh/signal-desk-ec2 ubuntu@$IP 'sudo journalctl -u signal-desk -n 50 --no-pager'

# signed-in API call from the Mac (spend so far)
curl -s -c /tmp/sd.jar -o /dev/null -d username=robert --data-urlencode "password=$ADMIN_PASSWORD" https://signal.barcik.training/login
curl -s -b /tmp/sd.jar https://signal.barcik.training/api/budget | jq .totals
```

Expected from outside: `/` → 303 to `/login`, `/api/*` → 401 without the cookie, port 7870
unreachable. A restart starts a fresh desk (rules back to the seed set, board empty, budget
ledger empty); the saved engineer run in `data/saved_run.json` survives.

## Cost

About 0.03 USD per hour while up (t3.small + Elastic IP + 12 GB gp3). OpenRouter spend is
separate: live triage ~0.5 USD an hour while a browser is open (it pauses when none is), one
engineer run 0.2-0.4 USD, capped as above.

## Deployment log

| Date | What | Notes |
|---|---|---|
| 2026-11-12 | kit prepared | not launched |
