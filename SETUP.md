# Hone Lab PaperScout — Setup Guide

Complete setup takes about 45 minutes, done once. After that the digest runs itself every morning.

---

## What you'll set up

| Piece | What it does | Where it lives |
|---|---|---|
| Google Sheet | Subscriber database | Google Drive |
| Google Apps Script | Handles form signups / returns subscriber list | Attached to the Sheet |
| GitHub repo | Hosts the signup page + runs the daily script | github.com |
| SendGrid account | Sends the daily emails | sendgrid.com |

---

## Step 1 — Create a GitHub account and repo

1. Go to **github.com** and click **Sign up**. Use your Columbia email.
2. After logging in, click the **+** in the top-right → **New repository**.
3. Name it something like `hone-lab-paperscout`. Set it to **Public** (required for free GitHub Pages).
4. Check **Add a README file**, then click **Create repository**.

---

## Step 2 — Upload the project files

1. In your new repo, click **Add file → Upload files**.
2. Upload all files from this folder:
   - `paper_scout.py`
   - `index.html`
   - `seen_ids.json`
   - The `.github/` folder (including `workflows/daily_digest.yml`)
3. Click **Commit changes**.

> **Tip:** Drag the entire folder onto the upload page — GitHub will preserve the subfolder structure.

---

## Step 3 — Enable GitHub Pages (the signup form)

1. In your repo, click **Settings** (top tab).
2. Click **Pages** in the left sidebar.
3. Under **Branch**, select `main` and folder `/` (root). Click **Save**.
4. Wait 1–2 minutes, then your signup form will be live at:
   `https://YOUR_USERNAME.github.io/hone-lab-paperscout/`

---

## Step 4 — Create the Google Sheet

1. Go to **sheets.google.com** → create a **New spreadsheet**. Name it `PaperScout Subscribers`.
2. In row 1, type these headers exactly (one per column, A through H):

   `Name` | `Email` | `Interests` | `TrackedPIs` | `FreeText` | `Active` | `SignupDate` | `LastUpdated`

3. Copy the **Spreadsheet ID** from the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
   (You won't need this ID directly — the Apps Script uses it automatically.)

---

## Step 5 — Set up the Google Apps Script

1. In your Google Sheet, click **Extensions → Apps Script**.
2. Delete all the placeholder code in the editor.
3. Open `Code.gs` from this folder, copy everything, and paste it into the editor.
4. Click **Save** (floppy disk icon).

### Set the admin key

5. In the Apps Script editor, find the function `setupAdminKey` near the bottom.
6. Replace `"REPLACE_WITH_YOUR_SECRET_KEY"` with a long random string — for example, make one at **passwordsgenerator.net** (32+ characters, letters+numbers).
7. **Write this key down** — you'll need it again in Step 7.
8. Click the dropdown next to **Run** and select `setupAdminKey`. Click **Run**. Approve the permissions popup.

### Deploy as a web app

9. Click **Deploy → New deployment**.
10. Click the gear icon next to **Type** and select **Web app**.
11. Set:
    - **Description:** PaperScout v1
    - **Execute as:** Me
    - **Who has access:** Anyone
12. Click **Deploy**. Copy the **Web app URL** — it looks like:
    `https://script.google.com/macros/s/AKfyc.../exec`
    **Save this URL** — you'll need it twice (Step 6 and Step 7).

---

## Step 6 — Update the signup form with your Apps Script URL

1. In your GitHub repo, click on `index.html`.
2. Click the **pencil icon** (Edit) in the top right.
3. Find this line near the bottom of the file:
   ```
   var APPS_SCRIPT_URL = "https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec";
   ```
4. Replace `YOUR_DEPLOYMENT_ID` with the actual ID from your deployment URL.
5. Click **Commit changes**.

---

## Step 7 — Create a SendGrid account

1. Go to **sendgrid.com** → **Start for Free** (100 emails/day free forever).
2. Sign up and verify your email.
3. Go to **Settings → API Keys → Create API Key**.
4. Name it `PaperScout`, select **Restricted Access**, enable **Mail Send**.
5. Click **Create & View**. Copy the key (starts with `SG.`). **You can only see it once.**

### Verify your sender email

6. Go to **Settings → Sender Authentication → Single Sender Verification**.
7. Add the Gmail address you'll send FROM (e.g. your lab Gmail). Verify it via the email they send.

---

## Step 8 — Store secrets in GitHub

This is where you store all the private keys so the daily script can access them without putting passwords in the code.

1. In your GitHub repo, click **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** for each of the following (name must match exactly):

| Secret name | Value |
|---|---|
| `SENDGRID_API_KEY` | The `SG.xxx` key from Step 7 |
| `FROM_EMAIL` | The sender email you verified in SendGrid (e.g. `yourlab@gmail.com`) |
| `APPS_SCRIPT_URL` | The full Apps Script web app URL from Step 5 |
| `APPS_SCRIPT_KEY` | The admin key you created in `setupAdminKey` |
| `PAGES_URL` | Your GitHub Pages URL, e.g. `https://yourname.github.io/hone-lab-paperscout` |

---

## Step 9 — Test the whole system

### Test the signup form
1. Visit your GitHub Pages URL.
2. Sign yourself up with a few interest categories selected.
3. Check that a new row appeared in your Google Sheet.

### Test the script manually
1. In your GitHub repo, click the **Actions** tab.
2. Click **Daily Paper Digest** in the left list.
3. Click **Run workflow → Run workflow** (green button).
4. Watch the run — it should complete in 2–5 minutes.
5. Check your email. Also check the repo: a new `seen_ids.json` commit should appear.

---

## Day-to-day operation

- **Digest runs automatically** every day at 9 AM UTC (5 AM ET). No action needed.
- **Add/remove subscribers:** They use the signup form themselves, or you can edit the Sheet directly (set `Active` to FALSE to pause someone).
- **Change delivery time:** Edit `.github/workflows/daily_digest.yml`, update the `cron:` line. Use crontab.guru to check the schedule.
- **Force a run:** Actions tab → Daily Paper Digest → Run workflow.
- **Test without sending email:** SSH/terminal access → `python paper_scout.py --test`

---

## Handing off to someone new

Give them:
1. Access to the GitHub repo (Settings → Collaborators → Add people)
2. Access to the Google Sheet (Share → add their email)
3. The SendGrid login
4. This SETUP.md

The only secret they'd need to rotate is the SendGrid API key (Settings → API Keys in SendGrid, create a new one, update the GitHub secret).

---

## Troubleshooting

**"No active subscribers" in the Actions log**
→ Check that APPS_SCRIPT_URL and APPS_SCRIPT_KEY secrets are correct. Try the URL in a browser: `YOUR_URL?action=subscribers&key=YOUR_KEY` — should return JSON.

**SendGrid error 403**
→ The FROM_EMAIL wasn't verified in SendGrid. Go back to Step 7 sender verification.

**"Script error" when submitting the signup form**
→ The Apps Script needs to be re-deployed after any edits. Deploy → Manage deployments → edit the existing one.

**Papers are repeating in the digest**
→ The `seen_ids.json` file in the repo may not be updating. Check the Actions log for the "Save seen IDs" step. Make sure the workflow has `permissions: contents: write`.
