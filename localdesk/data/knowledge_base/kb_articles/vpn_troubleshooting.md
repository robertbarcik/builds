# VPN Troubleshooting Guide

**Article ID:** KB-001
**Category:** Network
**Last Updated:** February 2026

## Common VPN Issues and Solutions

### Issue: VPN Disconnects When Switching Networks (WiFi ↔ Ethernet)

**Symptoms:** VPN connection drops when moving between WiFi and wired connections, or when switching WiFi networks (e.g., moving between meeting rooms).

**Steps to resolve:**
1. Open the FortiClient VPN application.
2. Go to **Settings → Connection** and enable **"Auto-reconnect on network change"**.
3. If the option is greyed out, you need an updated VPN profile. Contact IT to push the latest profile to your device.
4. As a temporary workaround, manually reconnect after switching networks.

### Issue: VPN Connection Fails Entirely

**Steps to resolve:**
1. Verify your internet connection is working (try opening a website without VPN).
2. Check that you are using the correct VPN gateway: `vpn.localdesk-services.eu`.
3. Ensure your credentials are current — VPN passwords expire every 90 days. Reset via the self-service portal at `https://password.localdesk-services.eu`.
4. Disable any personal firewall or antivirus temporarily to test if it's blocking the VPN.
5. On Windows: run `ipconfig /flushdns` in Command Prompt. On macOS: run `sudo dscacheutil -flushcache`.
6. Restart the FortiClient application (not just disconnect — fully quit and reopen).
7. If none of the above work, reboot your machine and try again.

### Issue: VPN Connected but Cannot Access Internal Resources

**Steps to resolve:**
1. Check if you can ping the internal DNS server: `ping 10.0.1.10`.
2. Verify split-tunneling settings haven't been modified. Open FortiClient → check **Tunnel Mode** is set to "Full Tunnel" for corporate access.
3. Clear your DNS cache (see commands above).
4. Try accessing the resource by IP address instead of hostname to rule out DNS issues.

### When to Escalate
If the above steps do not resolve your issue, create a support ticket with:
- Your operating system and version
- FortiClient version (Help → About)
- Screenshot of the error message
- Whether the issue is consistent or intermittent
- The network you're connecting from (office, home, public WiFi)
