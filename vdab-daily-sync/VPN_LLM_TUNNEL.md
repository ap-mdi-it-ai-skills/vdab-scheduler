# VPN Setup for School LLM Tunnel (GCP VM)

## 1. Install OpenVPN on the VM

```bash
sudo apt update
sudo apt install -y openvpn
```

## 2. Configure and start OpenVPN

Move config into the OpenVPN directory:

```bash
sudo cp ~/fw-UDP4-1210-ITAI-config.ovpn /etc/openvpn/client/school.conf
```

If your `.ovpn` needs auth, edit config and make sure it contains:

```text
auth-user-pass /etc/openvpn/client/vpn-auth.txt
```

Then copy credentials:

```bash
sudo cp ~/vpn-auth.txt /etc/openvpn/client/vpn-auth.txt
sudo chmod 600 /etc/openvpn/client/vpn-auth.txt
```

Start VPN and enable autostart:

```bash
sudo systemctl enable --now openvpn-client@school
sudo systemctl status openvpn-client@school
```

## 3. Verify tunnel is active

Check interface:

```bash
ip a | grep tun
```

Check route:

```bash
ip route
```

Check logs if needed:

```bash
sudo journalctl -u openvpn-client@school -f
```

## 4. Run your app with VPN active

From project folder:

```bash
cd ~/vdab-scheduler/vdab-daily-sync
docker compose up -d --build
```

Since containers use the host network stack for outbound routing (via NAT), traffic to VPN-routed destinations will go through the VPN as long as the host tunnel is up.

## 5. Quick troubleshooting

- VPN service fails to start:
  - Check: `sudo journalctl -u openvpn-client@school -n 200`
  - Common issue: wrong auth path or invalid credentials.
- LLM still unreachable:
  - Confirm tunnel is up (`tun0` exists).
  - Confirm your school LLM endpoint/IP is included in routes pushed by VPN.
  - Test from VM first with `curl` to the LLM endpoint before testing inside Docker.
- Docker app starts before VPN:
  - Start VPN first, then restart containers:
    `docker compose down; docker compose up -d --build`

## 6. Security notes

- Never commit `.ovpn` or `vpn-auth.txt` to Git.
- Keep credential files permissioned as `600`.
- Prefer VPN accounts dedicated to server usage when possible.
