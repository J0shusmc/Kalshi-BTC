# Windows terminal quick start

This is a terminal app for Windows 10/11. After setup, the normal commands are
`--paper` and `--live`.

1. Install [Python 3.11+](https://www.python.org/downloads/windows/) and Git.
   During Python installation, select **Add python.exe to PATH**.

2. Open PowerShell and enter:

   ```powershell
   git clone https://github.com/J0shusmc/Kalshi-BTC.git
   cd .\Kalshi-BTC
   py -3 -m venv venv
   .\venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

3. In the `Kalshi-BTC` folder, open `.env.example`:

   ```powershell
   notepad .env.example
   ```

4. At [Kalshi API Keys](https://kalshi.com/account/api), create an API key and
   copy the displayed **API Key ID** into this line:

   ```dotenv
   KALSHI_API_KEY=your-api-key-id
   ```

   Kalshi also supplies a PEM private-key file. Save it as
   `kalshi_private.key` in this folder and leave this line unchanged:

   ```dotenv
   KALSHI_PRIVATE_KEY_PATH=kalshi_private.key
   ```

   Set the starting-balance baseline in cents. For example, this starts paper
   trading at $115; use `100000` for $1,000. In live mode, it is the P/L
   baseline while Kalshi supplies the actual account balance:

   ```dotenv
   BTC15_STARTING_BALANCE_CENTS=11500
   ```

   The private key is required to sign API requests; never paste it into chat
   or commit it to Git. Kalshi makes the private key available only when the
   API key is created, so save it securely.

5. Save the edited file, then rename it from `.env.example` to `.env`:

   ```powershell
   Rename-Item .env.example .env
   ```

6. Start paper trading:

   ```powershell
   python .\scripts\btc15_live_monitor.py --paper
   ```

   The terminal must show `PAPER`. Press Ctrl+C to stop it.

7. To trade live, stop the paper bot first, then run:

   ```powershell
   python .\scripts\btc15_live_monitor.py --live
   ```

   `--live` can place real orders. Do not use it unless you intend to trade
   real money.

## Optional executable

Run `./windows/build-exe.ps1` after setup to build
`dist\windows\KalshiBTCMonitor.exe`. Put `.env` and `kalshi_private.key` next
to the executable, then run its normal `--paper` or `--live` command.
