# Example child apps

Drop-in PWAs you can upload to a portal to see the SDK in action. Each
directory is the raw bundle — zip it up (or use the Claude skill's
`package.py`) and upload via **Admin → Apps**.

| App | SDK services used | What it does |
| --- | --- | --- |
| [hello-receipt](hello-receipt/) | `user`, `pdf`, `email`, `storage` | Canonical reference — exercises every SDK service in one tiny app. |
| [mileage-log](mileage-log/) | `pdf`, `storage` | Log business trips with miles and purpose; export an IRS-ready deduction PDF. |
| [time-tracker](time-tracker/) | `pdf`, `email`, `storage` | Track billable time by client/project; export a clean timesheet PDF. |
| [expense-logger](expense-logger/) | `pdf`, `storage` | Log expenses by category and export a monthly PDF report. |
| [customer-directory](customer-directory/) | `email`, `storage` | Lightweight CRM — track customers, tag them, send quick emails. |
| [quote-builder](quote-builder/) | `pdf`, `email`, `storage`, `share` | Build itemized quotes, generate a PDF, share a link the customer can open. |
| [invoice-gen](invoice-gen/) | `pdf`, `email`, `storage` | Build line-item invoices, generate a branded PDF, email it to the customer. |

## Install one

```bash
# From a cloned repo:
python3 claude-skill/pwa-portal-app/scripts/package.py examples/mileage-log
PORTAL_URL=https://your-portal PORTAL_TOKEN=<admin-token> \
  python3 claude-skill/pwa-portal-app/scripts/upload.py mileage-log-1.0.0.zip
```

Or zip the directory by hand and upload via the admin UI.
