# Remote Desktop Access

**Article ID:** KB-010
**Category:** Network
**Last Updated:** January 2026

## Overview

Remote Desktop allows you to access your office workstation or virtual desktop from home or while traveling. All remote desktop connections require an active VPN connection first.

## Accessing Your Office Workstation

### Prerequisites
- Your office PC must be powered on and connected to the network (do not shut it down when leaving the office — use Sleep or Lock instead).
- You must be connected to the company VPN (see KB-001).
- Remote Desktop must be enabled on your office PC (enabled by default for standard builds).

### Windows (Remote Desktop Connection)
1. Connect to VPN.
2. Open **Remote Desktop Connection** (search for "mstsc" in Start).
3. Enter your office PC's hostname: `WS-<your-asset-tag>` (e.g., `WS-LT2024-0542`). Find your asset tag on the sticker on your office workstation.
4. Click **"Connect"** and enter your company credentials.
5. Your office desktop will appear in the remote session.

### macOS (Microsoft Remote Desktop)
1. Install **Microsoft Remote Desktop** from the Mac App Store.
2. Connect to VPN.
3. Click **"+"** → **"Add PC"**.
4. Enter the PC name: `WS-<your-asset-tag>.localdesk.internal`.
5. Click the saved connection and enter your credentials.

## Virtual Desktop Infrastructure (VDI)

For users assigned a virtual desktop instead of a physical workstation:

1. Connect to VPN.
2. Open your browser and go to `https://vdi.localdesk-services.eu`.
3. Sign in with your company credentials and complete MFA.
4. Select your assigned virtual desktop from the list.
5. You can either use the web client (HTML5) or download the VMware Horizon Client for better performance.

## Performance Tips
- Use a wired Ethernet connection at home for best performance.
- Close unnecessary applications on your local machine to free up bandwidth.
- If video calls are laggy over Remote Desktop, run the video call on your local machine instead and only use Remote Desktop for work applications.
- Set the display resolution in the RDP client to match your monitor (Settings → Display) for the sharpest image.

## Troubleshooting

### "Remote Desktop Can't Connect"
1. Verify your VPN is connected and working.
2. Confirm your office PC is powered on (ask a colleague to check or contact IT).
3. Try using the full hostname: `WS-<asset-tag>.localdesk.internal`.
4. Check if another user is logged into your office PC (only one session at a time is allowed).

### Slow Performance
1. In Remote Desktop settings, reduce the color depth to 16-bit.
2. Disable visual effects (Experience → Low-speed broadband).
3. Avoid copying large files over the remote session — use OneDrive or the network drive instead.

## When to Escalate
Contact IT if you cannot connect after following these steps, if your office PC appears to be offline, or if you need VDI access set up for the first time.
