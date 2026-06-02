# Google Jules: Deployment & Verification Instructions

Welcome to the CloudFlow project. Use this file as your integration instruction layer for autonomous changes, builds, and verifications.

---

## 🏗️ 1. Project Context & Constraints
* **Nature**: Flask-based Single Page Application (SPA) serving as a high-density seedr client.
* **Core Rule**: **Zero-Regression Protocol (ZRP)**. All modifications must be statically analyzed and verified using local scripts before committing.
* **Technical Debt Guidelines**: Read `TECH_DEBT_MAP.md` before making modifications, especially regarding mobile search row columns, JS fragment order, and Bitsearch rate limits.

---

## 🛠️ 2. Build Pipeline
The frontend relies on fragmented JS modules compiled into a single production bundle.
* **Source Location**: `streamly_hardened/static/js/src/`
* **Target Bundle**: `streamly_hardened/static/js/app.js`
* **Rebuild Command**:
  ```powershell
  & "D:\Downloads\Projects\Python Project\WPy64-31241\python-3.12.4.amd64\python.exe" streamly_hardened/static/js/build_js.py
  ```
  *(Always run this command after making changes to any fragment under `src/`. Never edit `app.js` directly).*

---

## 🧪 3. Pre-Flight Verification Checks
Before proposing a pull request or pushing code to deployment, you MUST execute the check suite and ensure it is 100% green.
* **Verification Command**:
  ```powershell
  cd ai/deploy
  & "D:\Downloads\Projects\Python Project\WPy64-31241\python-3.12.4.amd64\python.exe" check.py
  ```
* **Checked Parameters**:
  - Automatically rebuilds `app.js`.
  - Asserts syntax checking and JS bracket balances.
  - Asserts CSS brace balances in `base.css` and `responsive.css`.
  - Runs local Flask smoke tests to ensure HTTP 200 on all endpoints.

---

## 🚀 4. Deployment Workflow
* **Trigger**: Render is configured to auto-deploy on push to branch `main`.
* **Execution**:
  1. Add changes: `git add -A`
  2. Commit with descriptive summary: `git commit -m "<msg>"`
  3. Push: `git push origin main`
* **Troubleshooting Failures**: If Render deployment builds fail, analyze build logs, write a fix, run the local verification suite, and push a repair commit.

---

## 🔄 5. Rollback Procedures
If a bad deploy slips through:
1. Run `rollback.bat` in `ai/deploy/` or call the rollback script to check previous version hashes.
2. Select a stable snapshot and confirm redeployment.
