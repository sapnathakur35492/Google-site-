# 🚀 Google Sites Bulk Automation Engine (v2.0)

Professional-grade Django automation tool for high-speed, bulk generation of Google Sites with built-in resilience, background execution, and multi-system support.

## ✨ Key Features

- **Unstoppable Background Engine**: Uses 'Ghost Mode' (Off-Screen Headed) to bypass Google's headless detection while remaining unobtrusive.
- **Multi-System Support**: Machine-specific `SLUG_PREFIX` ensures multiple laptops can work simultaneously without URL collisions.
- **Smart Resumption**: Automatically skips completed sites and picks up exactly where it left off after any interruption.
- **Lock-Screen Resilience**: Optimized with Chromium anti-throttling flags to continue working even when the Windows screen is locked.
- **Dynamic Slug Logic**: Automatically generates clean, sequential slugs (e.g., `santosh-01`, `santosh-02`).
- **Editor Stability**: Advanced detection for Google Sites Editor readiness ensures 100% content injection success.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
- Python 3.10+
- Google Chrome installed on the system.

### 2. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chrome
```

### 3. Configure Environment
Create/Edit the `.env` file in the root directory:
```env
# Change this on each system (e.g., santosh, manish, rahul)
SLUG_PREFIX=santosh
```

### 4. Initialize Database
```bash
python manage.py migrate
```

---

## 🚀 How to Use

1. **Start the Server**:
   ```bash
   python manage.py runserver
   ```
2. **Access Dashboard**: Open `http://127.0.0.1:8000` in your browser.
3. **Upload CSV**: Prepare your Excel/CSV with `title`, `keyword`, and `content` columns.
4. **Manual Login (First Time)**: 
   - The first time you start, the browser will open.
   - **Log in to your Google Account manually.**
   - Once logged in, the system will save the session in the `google_session` folder.
5. **Automation**: Click **"Start Automation"**. You can minimize the window or even lock your screen; the engine will keep working in the background.

---

## 📂 Project Structure

- `core/automation/google_sites.py`: The main automation engine (Playwright logic).
- `core/views.py`: Dashboard and CSV upload management.
- `.env`: Machine-specific configuration.
- `google_session/`: Local storage for your Google login session (Do not delete).

---

## ⚠️ Important Notes
- **Do not manually close the browser window** unless you want the system to switch to background mode.
- If you change the Google account, delete the `google_session` folder to re-trigger the login.
- Keep `SLUG_PREFIX` unique for every user to avoid "Slug Taken" errors.

---
**Developed for High-Speed Digital Asset Creation.**
