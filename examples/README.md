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

**No repo checkout?** Easiest path: open the app's folder on GitHub
(e.g. <https://github.com/jacob-scheatzle/claude-pwa-portal/tree/main/examples/mileage-log>),
download the three files into a directory, zip the directory so
`portal.json` is at the root, and upload via **Admin → Apps** in the portal UI.

**With the Claude skill installed** (see [`docs/app-authoring.md`](../docs/app-authoring.md)):

```bash
git clone https://github.com/jacob-scheatzle/claude-pwa-portal.git
cd claude-pwa-portal
python3 ~/.claude/skills/pwa-portal-app/scripts/package.py examples/mileage-log
PORTAL_URL=https://your-portal PORTAL_TOKEN=<admin-token> \
  python3 ~/.claude/skills/pwa-portal-app/scripts/upload.py mileage-log-1.0.0.zip
```

**From a cloned repo without the skill** — the same scripts live at
`claude-skill/pwa-portal-app/scripts/`.
