# Multi-Factor Authentication (MFA) Setup

**Article ID:** KB-007
**Category:** Access
**Last Updated:** January 2026

## Why MFA Is Required

Multi-factor authentication is mandatory for all company accounts. MFA adds a second layer of security beyond your password, protecting your account even if your password is compromised. This is a company policy requirement and cannot be opted out of.

## Supported MFA Methods

1. **Microsoft Authenticator app** (recommended) — push notifications for quick approval
2. **TOTP authenticator apps** — Google Authenticator, Authy, or any TOTP-compatible app
3. **SMS codes** — sent to your registered mobile number (least secure, use only as backup)
4. **Hardware security key** — FIDO2/WebAuthn keys (YubiKey, etc.) available from IT for high-security roles

## Setup Instructions

### Microsoft Authenticator (Recommended)
1. Install **Microsoft Authenticator** from the App Store (iOS) or Google Play (Android).
2. On your computer, go to `https://aka.ms/mysecurityinfo` and sign in with your company account.
3. Click **"Add sign-in method"** → select **"Authenticator app"**.
4. Follow the prompts: click **"Next"** until you see a QR code.
5. In the Authenticator app, tap **"+"** → **"Work or school account"** → **"Scan QR code"**.
6. Scan the QR code displayed on your screen.
7. Approve the test notification sent to your phone.
8. Done! You'll now receive push notifications for MFA challenges.

### TOTP Authenticator App
1. Go to `https://aka.ms/mysecurityinfo` and sign in.
2. Click **"Add sign-in method"** → select **"Authenticator app"**.
3. Click **"I want to use a different authenticator app"**.
4. Scan the QR code with your preferred TOTP app.
5. Enter the 6-digit code displayed in the app to verify.

### Setting Up Backup Methods
We strongly recommend registering at least two MFA methods. If you lose access to your primary method (e.g., lost phone), the backup method prevents account lockout.

1. Go to `https://aka.ms/mysecurityinfo`.
2. Add a secondary method (e.g., SMS as backup to the Authenticator app).

## Common Issues

### Lost Phone / Can't Access Authenticator
1. Try your backup MFA method if one is registered.
2. If no backup method is available, contact the IT service desk. You will need to verify your identity in person (bring your company badge/ID) or via video call with your manager present.
3. IT will issue a temporary access pass valid for 24 hours while you re-register MFA on your new device.

### MFA Prompt Not Appearing
1. Check that notifications are enabled for the Authenticator app on your phone.
2. Ensure your phone has an internet connection (push notifications require connectivity).
3. Open the Authenticator app manually — the approval request may be waiting in the app.
4. If using SMS, check that your registered phone number is correct at `https://aka.ms/mysecurityinfo`.

## When to Escalate
Contact IT immediately if you believe your MFA has been compromised (e.g., you receive MFA prompts you didn't initiate). This could indicate someone has your password.
