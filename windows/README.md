# Windows quick start

This folder runs the same audited bot code natively on Windows 10/11. The
default launcher is locked to `--paper`; it never sends a live order.

1. Install [Python 3.11+](https://www.python.org/downloads/windows/) and Git.
   During Python installation, select **Add python.exe to PATH**.
2. Open PowerShell in the project folder and run:

   ```powershell
   .\windows\setup-paper.ps1
   ```

3. Put your downloaded Kalshi private key in the project folder as
   `kalshi_private.key`. Open `.env` and set `KALSHI_API_KEY` and
   `KALSHI_PRIVATE_KEY_PATH=kalshi_private.key`. Keep both files private.
4. Double-click `windows\start-paper.cmd` (or run
   `./windows/start-paper.ps1`). Press Ctrl+C to stop it. Paper history and
   the latest snapshot are stored under `local\`.
5. Optional: in a second PowerShell window, run
   `./windows/start-viewer.ps1`, then open `http://127.0.0.1:8787`.

## Portable executable

On a Windows machine, run `./windows/build-exe.ps1` after setup. It produces
`dist\windows\KalshiBTCMonitor.exe`. Put `.env` and the private key beside the
executable, then launch it in paper mode:

```powershell
.\KalshiBTCMonitor.exe --paper --risk-pct 20 --starting-balance-cents 11500 --trade-log .\local\paper_trade_log.json --json-out .\local\paper_latest.json
```

Windows executables must be built on Windows; a Linux build cannot produce a
native `.exe`. The source launcher is the recommended route while testing.
