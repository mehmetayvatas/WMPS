# WMPS 3 — Cashless Laundry Payment System (Home Assistant Add-on)
**Beginner-friendly, step-by-step guide** — no YAML edits required.

This add-on lets tenants pay for washer/dryer cycles using **6-digit codes**. It talks to your **ADAM-6050** (relays + inputs), plays **voice prompts**, logs every transaction to **CSV** files, and provides a clean **web panel** inside Home Assistant (Ingress).

---

## What you’ll have at the end
- A “WMPS” tile under **Settings → Add-ons** with a button **Open Web UI**.  
- Inside the panel:
  - **Machines** list (status, default minutes, price)
  - **Transaction History** (last 50)
  - **Add / Update User** (code, name, balance)
  - **Quick Charge** (manual test)
  - **Settings** (read-only summary; you change add-on options from HA UI)
  - **CSV Files** (download accounts & transactions)
- **Keypad flow**: `123456#` → choose machine `1..6` → press `#` to confirm.  
- **Safety**: System verifies machine availability via DI input, and **does not charge** if activation fails.

---

## Before you start (checklist)
1. **Home Assistant OS** running on a **Raspberry Pi 4** (or similar) with the **Supervisor** and **Add-on Store**.
2. **ADAM-6050** reachable on your network (you can change IP later).
3. **USB numeric keypad** plugged into the Raspberry Pi.
4. **Speaker / TTS** working in Home Assistant (e.g., Google TTS) and a **media_player** entity (e.g., `media_player.vlc_telnet`).  
   *If unsure: Settings → Devices & Services → search “media player” → note the entity id.*
5. (Recommended) **Nabu Casa Remote UI** is optional. We do **not** need your Nabu Casa password.
6. **You do not need to edit YAML**; everything is through the Add-on options and the WMPS UI.

---

## Part A — Put the add-on folder into Home Assistant
You received a folder named `wmps3` (containing `Dockerfile`, `config.yaml`, `requirements.txt`, `main.py`, etc.).  
We’ll place this folder into Home Assistant’s **Local add-ons** directory using the **Samba share** add-on (easiest).

### A1) Install the Samba share add-on (one-time)
1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Search for **“Samba share”** (by Home Assistant), click it.
3. Click **Install**, then **Start**.  
4. Click **Open Web UI** (or **Configuration**) to set a simple username/password if asked (e.g., `ha` / `ha1234`).

### A2) Copy the `wmps3` folder via your computer
- **Windows:** Press **Win + R**, type `\\homeassistant` and press Enter. Open the **addons** share.  
- **macOS:** Finder → **Go** → **Connect to Server** → enter `smb://homeassistant` → Connect. Open the **addons** share.  
- **Linux:** File Manager → **Connect to Server** → `smb://homeassistant` → open **addons**.

Inside the **addons** share, **copy the entire `wmps3` folder** (not just the files) so the path looks like:
```
\\homeassistant\addons\wmps3\
  Dockerfile
  config.yaml
  requirements.txt
  main.py
  (and other files, if present)
```

> Tip: If your network name is different, try the Pi’s IP, e.g., `\\192.168.1.50` or `smb://192.168.1.50`.

### A3) Refresh the Add-on Store
1. Back in Home Assistant: **Settings → Add-ons → Add-on Store**.
2. Click the **⋮ (three dots)** in the top-right → **Check for updates**.  
3. Scroll down — you should now see a **“Local add-ons”** section with **WMPS 3** listed.
   - If you don’t see it yet, go to **Settings → System → Restart**, wait 30-60s, then check again.

---

## Part B — Install & Configure the add-on
### B1) Install
1. Click **WMPS 3** under **Local add-ons**.
2. Click **Install** (wait 1–3 minutes).

### B2) Generate a Home Assistant Long-Lived Access Token (LLAT)
We need a token so the add-on can talk to Home Assistant (turn switches on/off, read sensors, play TTS).  
1. In HA, click your user avatar (bottom-left) → **My Profile**.  
2. Scroll to **Long-Lived Access Tokens**. Click **Create token**.  
3. Name it `wmps` and click **OK**.  
4. **Copy the token** (it shows only once). Keep it safe; you can delete it later.

### B3) Fill in the Add-on Options
1. Go back to **Settings → Add-ons → WMPS 3**. Click the **Configuration** tab.
2. Fill these fields (leave others as default unless instructed):
   - **`ha_url`**: `http://supervisor/core`  *(already set, best for add-ons)*
   - **`ha_token`**: **paste the token you created**.
   - **`tts_service`**: `tts.speak`  *(or your TTS, e.g., `tts.google_translate_say`)*
   - **`media_player`**: your media player entity, e.g. `media_player.vlc_telnet`
   - **`simulate`**: `true` for the first test (no real relays are triggered)
   - **`washing_minutes`**: `30` (default)
   - **`dryer_minutes`**: `60` (default)
   - **`price_washing`**: `5`
   - **`price_dryer`**: `5`
3. **Machine mapping** (default assumes you already have these in HA):
   - `switch.washer_1_control` / `binary_sensor.washer_1_status`
   - `switch.washer_2_control` / `binary_sensor.washer_2_status`
   - `switch.washer_3_control` / `binary_sensor.washer_3_status`
   - `switch.dryer_4_control`  / `binary_sensor.dryer_4_status`
   - `switch.dryer_5_control`  / `binary_sensor.dryer_5_status`
   - `switch.dryer_6_control`  / `binary_sensor.dryer_6_status`  
   If your entity names are different, edit them here to match your setup.  
   *(HA → Settings → Devices & Services → Entities → search “washer” / “dryer” to see the exact names.)*

Click **Save** on the Configuration page when done.

### B4) Start the add-on
1. In **WMPS 3**, open the **Info** tab.  
2. Toggle **Start on boot** (recommended), **Watchdog** (recommended).  
3. Click **Start**. Wait 5–15 seconds.  
4. Click **Open Web UI** — the WMPS panel opens inside Home Assistant.

---

## Part C — First-run test (no hardware movement)
We’ll keep **simulate = true** for a safe test.

### C1) Add a test user
1. In the WMPS panel, find **Add / Update User**.  
2. Enter:
   - `tenant_code`: `123456`
   - `name`: `Test`
   - `balance`: `20`
3. Click **Save**. You should see a success message below.

### C2) Simulate a charge from the UI
1. Go to **Quick Charge**.  
2. Enter:
   - `tenant_code`: `123456`
   - `machine`: `1`
   - leave **price** empty (uses default `$5`)
   - leave **minutes** empty (uses default `30` or `60`)  
3. Click **Simulate/Charge**.  
4. You should hear a TTS message and see a success response.  
5. In **Transaction History**, a new line should appear (Success = True).

> If you don’t hear TTS, double-check `media_player` and `tts_service` values in add-on Configuration.

### C3) Try the USB keypad (optional at this stage)
1. Plug the keypad into the Raspberry Pi (USB).  
2. In the WMPS panel → **Machines**, click **Refresh** (button at top of the page).  
3. On the keypad, type: `1 2 3 4 5 6 #` → **“Code accepted. Please select machine one to six.”**  
4. Press `2` → **“Washer 2 selected. Press enter to confirm.”**  
5. Press `#` (or Enter) → charge is simulated, history updates.

If keypad doesn’t respond, see **Troubleshooting → Keypad not detected**.

---

## Part D — Go live (real relays)
When your machine wiring is ready and ADAM-6050 is online:

### D1) Turn simulation OFF
1. WMPS add-on → **Configuration** → set **`simulate`** to `false` → **Save**.  
2. Restart the add-on (Info tab → **Restart**).

### D2) Confirm the machine inputs (DI)
- The system checks **binary_sensor.….status** to decide if a machine is **busy** (ON) or **available** (OFF).  
- After turning a relay **ON**, the add-on waits a few seconds for the DI to confirm.  
  - If DI **doesn’t confirm** in time, the charge is **aborted** and a “failed” record is logged.
  - If DI **confirms**, the charge is completed and the **OFF** timer is scheduled automatically.

### D3) Live test
1. Ensure at least one user has balance (e.g., `123456` with `$20`).  
2. On keypad: `123456#` → select machine `1..6` → press `#`.  
3. Verify the machine turns ON, then later turns OFF automatically after the default minutes.

> If a machine stays ON too long, verify the entity id in the mapping and your relay wiring. You can always turn it OFF from HA directly.

---

## How to change prices, minutes, or disable a machine
- Go to **Settings → Add-ons → WMPS 3 → Configuration**.  
- Edit:
  - `price_washing` / `price_dryer` (global defaults)
  - `washing_minutes` / `dryer_minutes` (global defaults)
  - You can also **disable** a machine: set `enabled: false` in the corresponding machine entry.  
- **Save**, then **Restart** the add-on.

> You can override a particular machine’s price using the `price_map` (advanced; ask your developer if needed).

---

## Where are my CSV files?
- Live data: `/data/accounts.csv` and `/data/transactions.csv` (inside the add-on).  
- Mirrored copies for you to download: **/share/wmps/accounts.csv** and **/share/wmps/transactions.csv**.  
  - From the WMPS panel (**CSV Files** card), click **Download accounts.csv** / **Download transactions.csv**.

Make regular backups of the `/share/wmps` folder (e.g., via the **Samba** share or Home Assistant Backups).

---

## Troubleshooting (common issues)
**1) I don’t see “WMPS 3” in Add-on Store → Local add-ons**  
- Confirm the folder path is exactly `addons/wmps3/` (not nested twice like `addons/wmps3/wmps3`).  
- In Add-on Store, click **⋮ → Check for updates**. If still missing, **Settings → System → Restart**.

**2) The add-on starts but “Open Web UI” fails**  
- Wait 20–30 seconds after starting the add-on.  
- Open **Logs** tab and look for errors.  
- If you changed the add-on files, click **Rebuild** (⋮ menu) and start again.

**3) No voice / TTS is silent**  
- In Configuration: set `tts_service` to `tts.speak` (or your installed TTS), and `media_player` to your real entity id.  
- Test TTS in HA Developer Tools → Services → call `tts.speak` with a short message.

**4) Keypad not detected**  
- Unplug/replug the keypad, then **Restart** the add-on.  
- Make sure the add-on has access to input devices (it does by default).  
- If still no luck, try a different USB port or a powered USB hub.

**5) “Insufficient funds”**  
- Add or increase the user’s balance in **Add / Update User** (or upload a CSV with higher balances).

**6) “Machine busy” even when it looks free**  
- The **status binary_sensor** might read **ON**. Check the entity in HA (Developer Tools → States).  
- Fix the wiring/logic so that “available” = OFF, or adjust the sensor you mapped in the add-on config.

**7) Charge fails with “Activation failed”**  
- The relay turned ON but the DI did not confirm in time. Check: wiring, DI mapping, and your input entity name.  
- You can increase the “activation confirm timeout” in the add-on config if needed.

**8) I changed options but nothing happened**  
- After changing Configuration, **Save** and **Restart** the add-on.

**9) I want to remove access later**  
- Go to **My Profile → Long-Lived Tokens** and **Delete** the token you used.  
- The add-on will no longer be able to control HA until you paste a new token.

**10) Still stuck?**  
- Open the add-on **Logs** and copy the last 30–50 lines to your developer.  
- Export `/share/wmps/accounts.csv` and `/share/wmps/transactions.csv` for inspection.

---

## Uninstall (if you ever need to)
1. **Stop** the add-on (Info tab).  
2. Click **Uninstall**.  
3. Optional: remove the folder from the **addons** share.  
4. Your CSV backups under `/share/wmps` are safe unless you delete them.

---

## Quick reference (values you’ll likely touch)
- **Token**: **ha_token** (paste once; rotate any time)
- **Voice**: **tts_service** (e.g., `tts.speak`), **media_player** (e.g., `media_player.vlc_telnet`)
- **Prices**: `price_washing`, `price_dryer` (default `$5`)
- **Durations**: `washing_minutes` (default `30`), `dryer_minutes` (default `60`)
- **Simulation**: `simulate` (`true` for tests, `false` in production)
- **Machines**: adjust **ha_switch** and **ha_sensor** names to match your entities

You’re done — enjoy your cashless laundry system! 🎉
