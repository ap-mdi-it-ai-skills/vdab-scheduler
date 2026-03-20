# Deploying `vdab-daily-sync` on a Google Cloud VM

This guide explains how to:

1. Create a VM in Google Cloud Platform (GCP).
2. Install Docker and Docker Compose on the VM.
3. Deploy and run the scheduler service.

## 1. Prerequisites

- A Google Cloud account with billing enabled.
- Access to a GCP project.
- This repository URL: `https://github.com/ap-mdi-it-ai-skills/vdab-scheduler.git`
- Required scheduler secrets and config values for `.env`.

## 2. Create the VM in GCP

1. Open **Compute Engine** in GCP.
2. Click **Create instance**.
3. Recommended settings:
   - **Name**: `vdab-daily-sync`
   - **Region**: a free-tier eligible region if you want to minimize cost (for example `us-central1`)
   - **Machine type**: `e2-micro` (free-tier eligible)
   - **Boot disk**: Debian 12 (Bookworm) or Ubuntu LTS (22.04/24.04), 20 GB standard persistent disk
   - **Firewall**: enable SSH access (HTTP/HTTPS only if you need external web access)
4. Click **Create**.

## 3. Connect to the VM

1. In Compute Engine VM list, click **SSH** on your new instance.
2. A browser terminal opens on the VM as your Linux user.

## 4. Install Docker Engine and Compose plugin

Run these commands on the VM.
They auto-detect Debian vs Ubuntu and configure the correct Docker APT repository.

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
source /etc/os-release

if [ "$ID" = "debian" ]; then
   DOCKER_REPO_BASE="https://download.docker.com/linux/debian"
elif [ "$ID" = "ubuntu" ]; then
   DOCKER_REPO_BASE="https://download.docker.com/linux/ubuntu"
else
   echo "Unsupported OS: $ID"
   exit 1
fi

curl -fsSL "$DOCKER_REPO_BASE/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] $DOCKER_REPO_BASE $VERSION_CODENAME stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker $USER
```

If you previously added the wrong Docker repo (for example Ubuntu repo on Debian), clean it first:

```bash
sudo rm -f /etc/apt/sources.list.d/docker.list
sudo apt update
```

Apply group changes by either:

- Logging out and reconnecting through SSH, or
- Running `newgrp docker` in the current shell.

Validate installation:

```bash
docker --version
docker compose version
```

## 5. Clone and configure the scheduler

Run:

```bash
sudo apt install -y git
git clone https://github.com/ap-mdi-it-ai-skills/vdab-scheduler.git
cd vdab-scheduler/vdab-daily-sync
cp .env.example .env
```

Edit environment variables:

```bash
nano .env
```

At minimum, make sure these are set:

- `VDAB_CLIENT_ID`
- `VDAB_CLIENT_SECRET`
- `VDAB_IBM_CLIENT_ID`
- `DATABASE_URL` or `SUPABASE_DB_URL` (according to your setup)

Optional scheduler settings you can tune:

- `DAILY_SYNC_CRON`
- `DAILY_SYNC_TIMEZONE`
- `DAILY_SYNC_RUN_ON_STARTUP`
- `LOG_LEVEL`

## 6. Build and run the service

From `vdab-scheduler/vdab-daily-sync`:

```bash
docker compose up -d --build
```

Check status:

```bash
docker compose ps
```

Follow logs:

```bash
docker compose logs -f
```

Stop service:

```bash
docker compose down
```

Restart service:

```bash
docker compose restart
```

## 7. Updating to latest code

```bash
cd ~/vdab-scheduler
git pull
cd vdab-daily-sync
docker compose up -d --build
```

## 8. Reboot behavior

Your `docker-compose.yml` uses `restart: unless-stopped`, so the container should come back after VM reboot.

To test:

```bash
sudo reboot
```

After reconnecting:

```bash
docker compose -f ~/vdab-scheduler/vdab-daily-sync/docker-compose.yml ps
```

## 9. Basic troubleshooting

- Build fails on permissions: run `newgrp docker` or reconnect SSH.
- Container exits immediately: inspect logs with `docker compose logs -f`.
- Env var errors: verify `.env` values and spelling.
- Out-of-memory issues on small VM: reduce extra services and use only required containers.

## 10. Cost control tips

- Use `e2-micro` in a free-tier eligible region.
- Avoid attaching unnecessary static external IPs.
- Keep disk size modest.
- Stop or delete unused VM instances.
