# Network Drive Mapping

**Article ID:** KB-008
**Category:** Network
**Last Updated:** December 2025

## Overview

Company shared drives are hosted on the internal file server and are accessible when connected to the office network or via VPN. Each employee has a personal drive (H:) and access to one or more department shared drives.

## Available Network Drives

| Drive Letter | Path | Description |
|-------------|------|-------------|
| H: | `\\fileserver\users\<username>` | Personal drive (10 GB quota) |
| S: | `\\fileserver\shared` | Company-wide shared drive (read-only for most users) |
| P: | `\\fileserver\projects\<department>` | Department project drive |
| T: | `\\fileserver\temp` | Temporary shared space (auto-deleted after 30 days) |

## Mapping a Network Drive

### Windows
1. Open **File Explorer**.
2. Click **"This PC"** in the left panel.
3. Click **"Map network drive"** in the toolbar (or right-click This PC → Map network drive).
4. Select a drive letter (use the standard letter from the table above).
5. Enter the folder path (e.g., `\\fileserver\users\jsmith`).
6. Check **"Reconnect at sign-in"** to make it persistent.
7. Click **"Finish"**. Enter your company credentials if prompted.

### macOS
1. Open **Finder**.
2. Press **Cmd+K** or go to **Go → Connect to Server**.
3. Enter the path using SMB format: `smb://fileserver/users/<username>`.
4. Click **"Connect"** and enter your company credentials.
5. To make it persistent: Go to **System Settings → General → Login Items** and add the mounted drive.

## Troubleshooting

### Drive Not Accessible
1. Verify you are connected to the office network or VPN.
2. Check if you can ping the file server: `ping fileserver` or `ping 10.0.1.20`.
3. Verify your credentials are current (passwords expire every 90 days).
4. On Windows, try accessing the path directly: open File Explorer and type `\\fileserver` in the address bar.

### Drive Mapped but Shows as Disconnected
1. Right-click the drive and select **"Disconnect"**.
2. Re-map the drive following the steps above.
3. If persistent, restart the Workstation service: open CMD as admin and run `net stop workstation && net start workstation`.

### Permission Denied
- You may not have access to the requested drive or folder.
- Submit a request through the IT service desk specifying which drive/folder you need access to and your business justification.
- Access changes require approval from the folder owner or department manager.

## Storage Quotas
- Personal drive (H:): 10 GB per user
- Department drives: managed by department — contact your manager for quota increases
- Temporary drive (T:): no per-user quota, but files older than 30 days are automatically deleted

## When to Escalate
Contact IT if the file server is unreachable for all users (potential server outage), if you receive persistent permission errors after being granted access, or if you need a quota increase for your personal drive.
