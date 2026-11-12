# Email Configuration Guide

**Article ID:** KB-005
**Category:** Software
**Last Updated:** January 2026

## Email Setup on Company Devices

### Outlook Desktop (Windows/macOS)
Company devices come with Outlook pre-configured. If you need to reconfigure:
1. Open Outlook and go to **File → Account Settings → Account Settings**.
2. Click **"New"** and enter your company email: `firstname.lastname@localdesk-services.eu`.
3. Outlook will auto-discover the server settings (Exchange Online).
4. Authenticate with your company credentials (MFA prompt will appear).
5. Outlook will sync your mailbox. Initial sync may take 10-30 minutes depending on mailbox size.

### Mobile Devices (iOS / Android)
1. Open the **Outlook mobile app** (preferred) or the native mail app.
2. Add a new account with your company email address.
3. You will be redirected to the company login page — enter your credentials and complete MFA.
4. For the native mail app, use these settings if auto-discover fails:
   - **Server:** outlook.office365.com
   - **Security:** SSL/TLS
   - **Port:** 993 (IMAP) or 443 (Exchange ActiveSync)
   - **Authentication:** OAuth2 / Modern Authentication

### Shared Mailboxes
To access a shared mailbox (e.g., support@, info@):
1. In Outlook desktop: **File → Account Settings → Account Settings → Change → More Settings → Advanced → Add** the shared mailbox address.
2. In Outlook mobile: Add the shared mailbox as a separate account using delegated access.
3. You must have been granted access by the mailbox owner or IT. If you don't have access, submit a request through the service desk with the mailbox owner's approval.

## Common Issues

### Emails Not Syncing
1. Check your internet connection.
2. Verify your password hasn't expired (passwords expire every 90 days).
3. In Outlook: try **Send/Receive All Folders** (Ctrl+F9).
4. Check if your mailbox is full — the default quota is 50 GB. Archive old emails if needed.

### Calendar Invitations Not Appearing
1. Ensure the calendar is visible in the folder pane.
2. Check if the invitation went to your Junk folder.
3. Restart Outlook and wait for re-sync.

## Email Signatures
Company email signatures must follow the corporate template. Download the current template from the intranet: **Intranet → Resources → Email Signature Generator**. Custom signatures that deviate from the corporate format are not permitted for external communications.

## When to Escalate
Contact IT if you experience persistent sync issues, cannot access a shared mailbox after being granted permissions, or suspect your email account has been compromised.
