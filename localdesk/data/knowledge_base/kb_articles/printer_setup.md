# Printer Setup and Troubleshooting

**Article ID:** KB-003
**Category:** Hardware
**Last Updated:** January 2026

## Adding a Network Printer

### Windows
1. Open **Settings → Devices → Printers & Scanners**.
2. Click **"Add a printer or scanner"** and wait for the scan to complete.
3. If your printer appears, select it and click **"Add device"**.
4. If it does not appear, click **"The printer I want isn't listed"** → select **"Add a printer using a TCP/IP address"**.
5. Enter the printer's IP address (see the list of office printers below).
6. Windows should automatically install the correct driver. If prompted, select the manufacturer and model.

### macOS
1. Open **System Settings → Printers & Scanners**.
2. Click the **"+"** button to add a new printer.
3. The printer should appear under the **"IP"** tab if you're on the office network.
4. Enter the printer's IP address, select protocol **"HP Jetdirect - Socket"**, and the driver should auto-detect.

## Office Printer Directory

| Location | Model | IP Address | Capabilities |
|----------|-------|-----------|-------------|
| 2nd Floor East | HP LaserJet Pro M404 | 10.0.2.50 | B&W, Duplex |
| 2nd Floor West | HP Color LaserJet M455 | 10.0.2.51 | Color, Duplex, Scan |
| 3rd Floor | Xerox VersaLink C405 | 10.0.3.50 | Color, Duplex, Scan, Fax |
| Reception | HP LaserJet Pro M304 | 10.0.1.50 | B&W only |

## Common Issues

### Print Jobs Stuck in Queue
1. Open the print queue (Windows: Devices → Printers → Open queue; macOS: Printers & Scanners → Open Print Queue).
2. Cancel all pending jobs.
3. Restart the Print Spooler service (Windows: `net stop spooler && net start spooler` in admin CMD).
4. Try printing again.

### Poor Print Quality
1. Run a cleaning cycle from the printer's front panel menu.
2. Check toner/ink levels — replacement cartridges are available from the supply room on the 2nd floor.
3. If quality is still poor after cleaning, submit a maintenance ticket.

## When to Escalate
Submit a ticket if you cannot add the printer after following these steps, or for hardware issues like paper jams that won't clear, error lights, or network connectivity problems with the printer itself.
