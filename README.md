# angel_auto

Angel One (SmartAPI) par **Nifty options** ka trading system. Aap dashboard par batate ho
market kis taraf jayega (Long/Short) aur option khareedna hai ya bechna (Buying/Selling).
App MACD se sahi time dekh kar **2 options ka spread** lagati hai, aur SL / target / trailing /
square-off par khud nikal jaati hai. Paper (nakli paisa) aur live (asli paisa), dono mode hain.

> Ye README aapke liye hai - shuru se aakhir tak sab kuch khud kar sako, isliye har cheez
> kadam-kadam likhi hai. Kuch samajh na aaye to us section ka naam leke poochh lena.

---

## Vishay-suchi

1. [Zaroori cheezein](#1-zaroori-cheezein)
2. [Pehli baar setup](#2-pehli-baar-setup-ek-hi-baar-karna-hai)
3. [Roz kaise chalayein](#3-roz-kaise-chalayein)
4. [Dashboard kaise use karein](#4-dashboard-kaise-use-karein)
5. [Strategy kaise kaam karti hai](#5-strategy-kaise-kaam-karti-hai)
6. [Orders kaise jaate hain (suraksha)](#6-orders-kaise-jaate-hain-suraksha)
7. [Settings kaise badlein](#7-settings-kaise-badlein)
8. [App band ho jaye / restart](#8-app-band-ho-jaye--computer-band-ho-jaye--restart)
9. [Live (asli paisa) jaane se pehle](#9-live-asli-paisa-jaane-se-pehle)
10. [Kuch galat ho to kya karein](#10-kuch-galat-ho-to-kya-karein)
11. [Madad wali scripts](#11-madad-wali-scripts)
12. [Files aur folders](#12-files-aur-folders-kya-kahan-hai)
13. [Code update, backup, tests](#13-code-update-backup-tests)
14. [VPS par 24 ghante chalana (Docker)](#14-vps-par-24-ghante-chalana-docker)
15. [Suraksha ke niyam](#15-suraksha-ke-niyam-hamesha-yaad-rakhein)

---

## 1. Zaroori cheezein

| Cheez | Kyun |
|---|---|
| **Angel One trading account** (F&O chalu) | Options trade ke liye |
| **SmartAPI app** (smartapi.angelone.in par) | API key milti hai |
| **TOTP chalu** | App bina OTP pooche login karti hai |
| **Static IP registered** | SEBI niyam (April 2026 se): order sirf registered IP se jaate hain |
| **Windows computer + Python 3.11 ya naya** | App chalane ke liye |
| **Internet bina VPN / Cloudflare WARP ke** | VPN se IP badal jaata hai aur order reject hote hain |

---

## 2. Pehli baar setup (ek hi baar karna hai)

### 2.1 Python install
python.org se **Python 3.12** download karo. Install karte waqt **"Add Python to PATH"** zaroor tick karo.

### 2.2 Project laao
Project folder `C:\Users\<aap>\Downloads\angel_auto` jaisi jagah rakho. GitHub se naya laana ho to:
```powershell
git clone https://github.com/sumitug1950/angel_auto.git
cd angel_auto
```

### 2.3 Python ka alag mahaul (venv) aur packages
Project folder mein VS Code kholo, **Ctrl + `** se terminal kholo, aur ye chalao:
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
Ye ek hi baar karna hai. Baad mein saari commands `.venv\Scripts\python.exe` se chalti hain.

### 2.4 Angel One ki details (`config\.env`)
```powershell
Copy-Item config\.env.example config\.env
```
Ab `config\.env` kholo aur bharo:

| Setting | Kahan milega |
|---|---|
| `ANGEL_API_KEY` | smartapi.angelone.in par login - apni app - **API Key** |
| `ANGEL_CLIENT_CODE` | Aapka Angel One client ID (jaise `AAAA119972`) |
| `ANGEL_MPIN` | Angel One ka 4 digit MPIN |
| `ANGEL_TOTP_SECRET` | SmartAPI par **Enable TOTP** karte waqt jo QR ke saath lamba **secret key** milta hai (QR nahi, wo text) |
| `ANGEL_MARKET_API_KEY` | Khali chhod sakte ho |

> **`config\.env` kabhi kisi ko mat bhejo, GitHub par nahi jaati** (gitignore mein hai).

### 2.5 Static IP register karo (bahut zaroori)
1. **Cloudflare WARP / 1.1.1.1 / koi VPN band karo.**
2. Browser mein "what is my ip" search karo - jo IP aaye (jaise `103.173.208.38`) note karo.
3. **smartapi.angelone.in** - login - apni app - **Primary Static IP** mein wo IP daalo - save.
4. Dhyan: Angel One IP **hafte mein sirf ek baar** badalne deta hai. Ek **Secondary IP** bhi rakh sakte ho.
5. Ghar ka IP router restart par badal sakta hai. Badle to naya IP daalna padega. Pakka hal: ISP se static IP, ya VPS ([section 14](#14-vps-par-24-ghante-chalana-docker)).

### 2.6 Login jaancho
```powershell
.venv\Scripts\python.exe scripts\dry_run_auth.py
```
`Login OK` aur aapka naam aaye to sahi hai. Ye koi order nahi bhejta.

### 2.7 Settings jaancho
```powershell
.venv\Scripts\python.exe scripts\check_config.py
```
"Config sahi hai" aaye aur neeche aapki settings dikhen. Galti ho to wo line batayega.

---

## 3. Roz kaise chalayein

### Sabse aasaan: double-click
Windows Explorer mein project folder kholo:

| File | Kab |
|---|---|
| **`start_paper.bat`** | Nakli paisa (testing). `config.yaml` mein `mode: paper` hona chahiye |
| **`start_live.bat`** | Asli paisa. `mode: live` hona chahiye. Poochega - **`YES`** (bade aksharon mein) likh kar Enter |

Kaali window khulegi, config check hogi, login hoga, ~12 sec mein browser apne aap
**http://127.0.0.1:8000** khol dega.

> VS Code mein `.bat` file kholne se wo sirf text dikhti hai, chalti nahi.
> VS Code terminal se chalana ho to `.\start_live.bat` (shuru ka `.\` zaroori).

### Command se (ye bhi chalta hai)
```powershell
# Paper
.venv\Scripts\python.exe scripts\run_dashboard.py

# Live - dono line usi terminal mein, har baar
$env:ANGEL_LIVE_TRADING_CONFIRMED="YES_I_UNDERSTAND_THE_RISK"
.venv\Scripts\python.exe scripts\run_dashboard.py
```
Window mein `Uvicorn running on http://0.0.0.0:8000` aane ke baad browser mein `http://127.0.0.1:8000` kholo.

### Roz ka time-table
| Time | Kya |
|---|---|
| **~9:00** | App start karo (market se pehle, taaki MACD ke liye candles ban sakein) |
| 9:15 - 15:15 | Dashboard se trade |
| **15:15** | App khud square-off karti hai (expiry wale din **15:00**) |
| 15:30 ke baad | Kaali window mein **Ctrl + C** se band karo |

### Band karna
Kaali window / terminal mein **Ctrl + C**. **Browser band karne se app band nahi hoti.**
**Trade chalte waqt window band mat karo aur computer ko sleep mein mat jaane do.**

---

## 4. Dashboard kaise use karein

### Upar ki patti
| Pill | Matlab |
|---|---|
| **PAPER** (peela) / **LIVE · asli paisa** (laal) | Kaunsa mode chal raha hai |
| Market khula / band | NSE market time |
| **Data live** (hara) / purana (peela) / **ruka** (laal) | Angel One se price aa rahe hain ya nahi. Laal ho to app khud dobara jodti hai |
| Nifty, VIX, IST | Live daam aur ghadi |
| **Live / Disconnected** | Browser app se juda hai ya nahi |

### Chart (baayein)
- **15-second candles** (hari = upar, laal = neeche), neeche **MACD** (neeli), **Signal** (narangi), histogram.
- Button: **15m / 30m / 1h / Poora din**. Mouse se zoom / scroll.
- Mouse le jao to upar O/H/L/C aur MACD values dikhti hain.
- Trade ke **entry (tir) aur exit (gola)** nishaan chart par.

### Trade control (daayein)
1. **Kadam 1 - Buying / Selling** chuno. Neeche likha aata hai kya hoga aur kaunsi expiry.
2. **Kadam 2 - Long / Short** dabao. Button par **CALL / PUT** saaf likha hota hai.
3. MACD sahi na ho to **peela "Pending"** dikhega - intezaar karo ya **Cancel**.
4. **Laal message** aaye to padho - usme wajah likhi hoti hai (margin kam, market band, price nahi mile...).
5. **Exit now** - khuli position turant band. **Kill switch** - position band + aaj ke liye naye trade band.
6. Sabse upar **⛔ laal patti** aaye (order ka status pakka nahi hua) - Angel One app mein check karo, phir app restart karo.

### Position
Live P&L, **SL se Target tak ki patti** (safed gola = abhi kahan ho), trailing ki jaankari, har leg ka Entry / LTP / P&L.

### Aaj ka hisaab
Trades (x / 2), realized P&L, lagatar loss, aur "Trading chalu / band".

### Neeche - Trades / Logs
- **Trades**: har band trade - time, Call/Put, expiry, legs ka entry→exit daam, exit kyun hua, gross, charges, net.
- **Logs**: app kya kar rahi hai. "Sirf warning / error" filter se sirf gadbad dikhegi.

---

## 5. Strategy kaise kaam karti hai

### 5.1 Chaar tarah ke trade
| Aap dabate ho | Option | ITM leg (main) | OTM leg (hedge) | Expiry (default) |
|---|---|---|---|---|
| **Buying + Long** | CALL | **BUY** (delta ~0.7) | SELL (delta ~0.1) | Monthly (kam se kam 10 din baaki) |
| **Buying + Short** | PUT | **BUY** | SELL | Monthly |
| **Selling + Long** | PUT | **SELL** | BUY | Weekly (current) |
| **Selling + Short** | CALL | **SELL** | BUY | Weekly |

Dono legs hamesha **ek hi expiry aur ek hi type** (Call+Call ya Put+Put) ki hoti hain.

### 5.2 MACD se entry
- Nifty ki **15-second candles** par MACD (12/26/9).
- Long dabaya aur MACD Signal ke **upar** hai - trade turant. Short ke liye **neeche**.
- MACD khilaaf ho - request **Pending**. Jab MACD aapki taraf aa jaye - trade lagta hai.
- Price na mile to Pending bani rehti hai aur har 15 sec dobara koshish hoti hai.
- Pending agli subah 8:30 par apne aap cancel hoti hai. Square-off time ke baad bhi cancel.

### 5.3 Strike kaise chuni jaati hai
Nifty se ±2500 point tak ke options ke live price aate hain, har option ka **delta** uski apni expiry se nikalta hai, aur
ITM ke liye delta ~0.7, OTM ke liye ~0.1 wali strike chuni jaati hai (100 point ke gap wali).

### 5.4 VIX ka override
India VIX kal ke close se **3% ya zyada badha** - Buying hi lagega (darr ka din). **3% ya zyada ghata** - Selling hi.
(Settings mein badal ya band kar sakte ho.)

### 5.5 Exit kab hota hai
Dono legs ka **total P&L** dekha jaata hai (har 15 sec):

| Situation | Kya hoga |
|---|---|
| Loss **₹4,000** | Stop-loss exit |
| Profit **₹4,800** (target) tak pahuncha | Trailing shuru - exit nahi |
| Trailing ke baad sabse zyada profit se **₹1,000** gira | Exit (profit lock) |
| **Exit now** / **Kill switch** | Exit |
| **15:15** (expiry din 15:00) | Zabardasti exit (square-off) - fail ho to har cycle dobara |
| Broker wala backup SL laga | Bachi leg band |
| MACD ulta | Exit **nahi** (default band hai) |

Example trailing: profit 4,800 → 6,500 (stop 5,500) → 5,500 par exit = ₹5,500 profit.

### 5.6 Din ki suraksha
| Niyam | Default |
|---|---|
| Din mein max trade | 2 |
| Ek time par | 1 position |
| Lagatar loss par band | 2 |
| Din ka loss limit | ₹8,000 |
| Lot | 1 (65 qty) |

Koi bhi limit poori hui to us din naye trade band (restart karne par bhi band rehta hai).

---

## 6. Orders kaise jaate hain (suraksha)

| Cheez | Kaise |
|---|---|
| **Entry order** | LIMIT - buy LTP + ₹1, sell LTP - ₹1 (lagbhag market rate) |
| **Entry ka kram** | Selling: pehle hedge (OTM buy), phir ITM sell. Buying: pehle ITM buy, phir OTM sell. Buy hamesha pehle - margin ka fayda |
| **Exit order** | MARKET |
| **Exit ka kram** | Pehle **sell leg wapas kharidi**, phir buy leg bechi - sell kabhi akeli (naked) nahi |
| **Fill ki jaanch** | Order bhej kar har 2 sec Angel One se status. 10 sec mein fill na ho - cancel, phir dobara status (cancel ke beech fill hua to wo bhi pakda) |
| **Adhura fill** | Entry mein adhura hissa wapas band. Exit mein bachi qty dobara |
| **Status pata na chale** | App **sab naye orders rok deti hai** + trading band + laal patti. Guess karke dobara order kabhi nahi |
| **Margin** | Live mein har entry se pehle Angel One margin API se check. Kam ho to trade nahi + message |
| **Broker backup SL** | Selling trade mein sell leg par Angel One ke paas **STOPLOSS order** (SL ka 1.5 guna loss = ₹6,000). App band ho tab bhi bachaav. App khud exit kare to pehle ye cancel. Buying trade mein nahi lagta (buy leg ka SL sell leg ko naked chhod deta) |
| **Market band** | 9:15 - 15:30 ke bahar **koi order nahi jaata** (warna Angel One use AMO bana kar agli subah chala deta) |
| **Price feed ruka** | Naya trade nahi. Exit (MARKET) phir bhi jaata hai |
| **Ek saath do order** | Dashboard, loop aur scheduler ke beech taala - exit do baar nahi ja sakta |
| **Pehchaan** | Har order par `AA<number>` tag - crash ke baad Angel One mein dhoondhne ke liye |

---

## 7. Settings kaise badlein

Do files hain, dono mein har setting ke saath Hinglish comment likha hai:

- **`config\config.yaml`** - app ki settings
- **`config\strategies.yaml`** - strategy ki settings

**Tareeka:** value badlo - save - `.venv\Scripts\python.exe scripts\check_config.py` - app restart.
Spelling galat ya galat value hui to app start **nahi** hogi aur check_config batayega kaunsi line.

### `config.yaml` - khaas settings
| Setting | Default | Matlab |
|---|---|---|
| `mode` | `paper` | `paper` = nakli, `live` = asli |
| `lot_size` | 65 | NSE Nifty lot |
| `square_off.normal_time` / `expiry_day_time` | 15:15 / 15:00 | Zabardasti exit ka time |
| `scheduler.daily_relogin_time` | 08:45 | Roz ka login |
| `risk.daily_loss_limit_rs` | 8000 | Din ka loss limit |
| `risk.max_consecutive_losses` | 2 | Lagatar loss par band |
| `risk.max_trades_per_day` | 2 | Din mein max trade |
| `oms.entry_slippage_buffer_pts` | 1.0 | Entry LIMIT = LTP ± itna |
| `oms.fill_timeout_sec` | 10 | Fill ka intezaar, phir cancel |
| `oms.fill_poll_interval_sec` | 2 | Status poochne ka antar (1 se kam mat karna - Angel One rok deta hai) |
| `oms.exit_attempts` | 3 | Adhure exit ki bachi qty kitni baar |
| `oms.sl_limit_buffer_pts` | 5.0 | Broker SL: limit = trigger + itna |
| `paper_trading.starting_capital_rs` | 20000 | Paper ka nakli paisa |
| `paper_trading.simulate_margin_check` | false | Paper mein bhi margin check |
| `charges.*` | | Brokerage/tax ka andaaza - apne contract note se milayein |
| `tick_recorder.enabled` | true | Har tick save (restart par chart/MACD wapas banane ke liye zaroori) |

### `strategies.yaml` - khaas settings
| Setting | Default | Matlab |
|---|---|---|
| `flagship_enabled` | true | false = strategy band |
| `candle_interval_sec` | 15 | Candle kitne second ki |
| `macd.fast/slow/signal_period` | 12/26/9 | MACD |
| `macd.min_candles_before_entry` | 0 | Itni candles tak trade nahi. **35** rakho to shuru ke ~9 min kachcha MACD skip |
| `check_interval_sec` | 15 | SL/target/pending har itne sec check. Kam = SL jaldi |
| `start_with` | BUYING | Koi button na dabaya ho to |
| `buying.expiry` / `selling.expiry` | MONTHLY / WEEKLY | Expiry |
| `buying.min_days_to_expiry` / `selling...` | 10 / 0 | Kam se kam itne din baaki |
| `*.strike_grid` | 100 | 100 ya 50 point ke strikes |
| `*.itm_delta` / `*.otm_delta` | 0.7 / 0.1 | Strike ka delta |
| `option_band_points` | 2500 | Nifty se itne point tak ke option prices |
| `vix_override.threshold_pct` | 3.0 | VIX kitna % badle |
| `vix_override.on_rise` / `on_fall` | BUYING / SELLING | Kya force kare (`OFF` = kuch nahi) |
| `sizing.lots` | 1 | Har trade kitne lot |
| `exit.sl_amount_rs` | 4000 | Stop-loss (₹) |
| `exit.risk_reward_ratio` | 1.2 | Target = SL × ye |
| `exit.trail_gap_rs` | 1000 | Trailing gap |
| `exit.exit_on_opposite_macd` | false | MACD ulta hone par exit |
| `exit.broker_backup_sl` | true | Selling mein broker par SL |
| `exit.broker_backup_sl_multiple` | 1.5 | Broker SL = SL × ye |

---

## 8. App band ho jaye / computer band ho jaye / restart

**Bas dobara `start_live.bat` (ya paper) chalao.** App khud:

1. Aaj ke recorded ticks se **chart aur MACD wapas banati hai**.
2. Beech mein **atke orders Angel One mein dhoondhti hai** (order ID ya `AA` tag se). Chalu order ho to cancel, fill hua ho to record karti hai.
3. **Adhuri entry** (koi leg fill ho chuki thi) - position khuli maan kar sambhalti hai. Kuch fill nahi hua tha - cancel.
4. **Adhura exit** - bachi legs agli check par band. Poora ho chuka tha - trade band karke record.
5. **Live mode mein Angel One ki asli positions se milaan**:
   - Sab mile - trade wahin se chalu (SL, trailing sab).
   - Angel One par position pehle hi band (broker ne square-off kiya / aapne band ki) - record band + message ("asli P&L Angel One app mein dekhein").
   - **Na mile** - **trading ruk jaati hai + laal patti**. Angel One app mein check karo, theek karo, phir restart.
6. Jo bhi hua, dashboard par message mein likha aata hai.

**Dhyan rahe:**
- App band rehte **app ka SL nahi chalta**. Selling trade par Angel One ka backup SL chalta rehta hai. Buying trade par sirf Angel One ka apna intraday auto square-off.
- Restart ke baad jab tak kisi leg ka **taaza price na aaye**, SL/trailing ka faisla nahi hota (galat exit se bachne ke liye).
- Market ke beech internet toota to app **30 sec baad khud dobara judti hai** (zaroorat pade to login bhi).

---

## 9. Live (asli paisa) jaane se pehle

### Checklist
- [ ] Kuch din / hafte **paper mode** mein chala kar dekha
- [ ] `scripts\check_config.py` - "Config sahi hai", settings sahi
- [ ] **Static IP registered**, WARP/VPN band
- [ ] Market time mein **live smoke test PASS** (neeche)
- [ ] `sizing.lots: 1`
- [ ] Angel One app phone/computer par khuli
- [ ] Pehle din poore market time screen ke saamne

### Live smoke test (asli chhota trade, ~₹100)
Market time (9:15 - 15:30) mein:
```powershell
.venv\Scripts\python.exe scripts\live_smoke_test.py
```
`YES` likhne par ye **asli** karta hai:
1. ₹1-5 wala far-OTM CALL **khareedta** hai (app ki entry jaisa LIMIT) - fill jaanch
2. Broker **STOPLOSS SELL** lagata aur cancel karta hai
3. **MARKET** se bechta hai (app ke exit jaisa) - fill jaanch
4. Pakka karta hai ki position band

Har kadam par **PASS / FAIL**. FAIL aaye to live mat karo - message padh kar theek karo.
Kuch bhi khula reh jaye to script bada warning deti hai - **Angel One app se khud becho**.

### Live chalu karna
1. `config\config.yaml` mein `mode: live`
2. `start_live.bat` - `YES`
3. Dashboard par **LIVE · asli paisa** (laal) dikhna chahiye

### Wapas paper
`mode: paper` karo aur `start_paper.bat`.

---

## 10. Kuch galat ho to kya karein

| Dikhe | Wajah | Kya karein |
|---|---|---|
| `LOGIN FAILED` | `.env` galat / TOTP secret galat | `config\.env` jaancho, `scripts\dry_run_auth.py` chalao |
| `... is not a registered IP` (AG7002) | IP registered nahi / badal gaya / WARP chalu | WARP band, "what is my ip" - SmartAPI par IP update |
| `exceeding access rate` (logs mein) | Angel One ko bahut jaldi poocha | Apne aap theek hota hai. `fill_poll_interval_sec` 2 se kam mat karo |
| `start_live.bat is not recognized` | Terminal mein `.\` nahi likha | `.\start_live.bat` ya Explorer se double-click |
| Browser mein page nahi khulta | App shuru hi nahi hui / window YES ka intezaar kar rahi | Kaali window dhoondho, `YES` likho, "Uvicorn running" ka intezaar |
| `address already in use` / port 8000 | App pehle se kisi aur window mein chal rahi | Us window mein Ctrl+C |
| `mode: live requires the environment variable...` | Live taala nahi lagaya | `start_live.bat` se chalao |
| `GALTI config.yaml / strategies.yaml` | Setting galat | Batayi line theek karo |
| Chart khaali | Market band / app abhi shuru hui | Market time mein data aayega |
| **Data ruka** (laal) | Angel One feed toota | App khud jodti hai. Baar-baar ho to internet jaancho |
| Laal: "Market band hai - order nahi bheja" | 9:15 - 15:30 ke bahar | Sahi hai - market time mein dabao |
| Laal: "Margin kam hai: chahiye ₹X..." | Account mein paisa kam | Funds daalo ya lot kam |
| Laal: "MACD ne ... confirm kiya, par prices nahi mile" | Option ka price nahi aaya | Apne aap koshish hoti hai, ya Cancel |
| Laal: "Trade nahi laga: daily trade cap..." | Din ki limit poori | Kal |
| **⛔ Upar laal patti** ("status pakka nahi hua") | Angel One se order ka jawab nahi mila | **Angel One app mein order + position check**, zaroorat ho to wahan se band karo, phir app restart |
| Laal: "positions app ke record se nahi milti" (restart par) | Angel One aur app ka record alag | Angel One app dekho, positions theek karo, restart |
| Trading "band" dikhe | Loss limit / lagatar loss / kill switch | Us din naya trade nahi. Kal apne aap chalu |

Zyada jaankari: dashboard **Logs** tab, ya `data_store\logs\angel_auto.log`.

---

## 11. Madad wali scripts

Sab project folder se `.venv\Scripts\python.exe scripts\<naam>` se chalti hain.

| Script | Kya karti hai | Order? |
|---|---|---|
| `run_dashboard.py` | Poori app + dashboard (roz yahi) | Mode ke hisaab se |
| `check_config.py` | Settings jaanch + summary | Nahi |
| `dry_run_auth.py` | Sirf login jaanch | Nahi |
| `dry_run_ws.py 30` | 30 sec live Nifty price | Nahi |
| `dry_run_app.py 30` | Poori app 30 sec (paper broker) | Nahi |
| `live_smoke_test.py` | Market time mein asli chhota trade jaanch | **Haan, asli** |
| `run_backtest.py 2026-03-25 2026-08-14` | Purane daily data par strategy ki jaanch (andaaza, asli replay nahi) | Nahi |
| `backup_db.py` / `backup_db.py --keep 30` | Database ka backup `data_store\backups\` mein | Nahi |

---

## 12. Files aur folders (kya kahan hai)

```
angel_auto/
├─ start_live.bat / start_paper.bat   double-click se chalana
├─ config/
│  ├─ .env                 Angel One details (secret, git mein nahi)
│  ├─ .env.example         .env ka namuna
│  ├─ config.yaml          app settings
│  └─ strategies.yaml      strategy settings
├─ angel_auto/             app ka code
│  ├─ broker/              Angel One login, orders, WebSocket, paper broker
│  ├─ strategy/            Flagship strategy (MACD, strike, exit)
│  ├─ oms/                 Order manager (fill jaanch, backup SL, restart milaan)
│  ├─ risk/                Din ki limit, kill switch
│  ├─ data/                Instruments, live feed, candles, tick recorder
│  ├─ analytics/           MACD, Black-Scholes, IV, charges
│  ├─ dashboard/           Web dashboard
│  ├─ scheduler/           Square-off, roz login, reset
│  ├─ persistence/         Database (trades, orders, ticks)
│  └─ core/app.py          Sab jodne wala hissa
├─ scripts/                madad wali scripts (section 11)
├─ tests/                  automatic tests
├─ data_store/             database, logs, backups (git mein nahi)
├─ logs/                   Angel One library ke logs - API key ho sakti hai, share mat karo
└─ docker/                 VPS ke liye
```

---

## 13. Code update, backup, tests

**Naya code laana (GitHub se):**
```powershell
git pull
pip install -e ".[dev]"
.venv\Scripts\python.exe scripts\check_config.py
```
Phir app restart. (Trade chalte waqt update mat karo.)

**Backup** (roz market ke baad accha hai):
```powershell
.venv\Scripts\python.exe scripts\backup_db.py --keep 30
```

**Tests** (code badla ho to):
```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```
Sab `passed` aane chahiye. Tests koi asli order nahi bhejte, aur hamesha paper maan kar chalte hain.

---

## 14. VPS par 24 ghante chalana (Docker)

Kiraye ka Linux server (VPS) lo - uska IP pakka rehta hai aur computer band ho tab bhi app chalti hai.

1. VPS ka IP **SmartAPI par Primary/Secondary Static IP** mein register karo.
2. VPS par:
   ```bash
   git clone https://github.com/sumitug1950/angel_auto.git && cd angel_auto
   cp config/.env.example config/.env
   nano config/.env                 # Angel One details bharo
   nano config/config.yaml          # pehle mode: paper rakho
   cd docker
   docker compose build
   docker compose up -d
   docker compose logs -f           # login, WS connect dekho
   curl http://localhost:8000/health
   ```
3. Live ke liye (har baar, usi SSH session mein):
   ```bash
   export ANGEL_LIVE_TRADING_CONFIRMED=YES_I_UNDERSTAND_THE_RISK
   docker compose up -d
   ```
4. Update: `git pull && docker compose up -d --build`. Data (`angel_auto_data` volume) nahi jaata.
5. **Dashboard ko internet par bina password mat kholo** - live mein wahan se asli order jaate hain.
   `docker/nginx.conf.example` mein HTTPS + password ka tareeka hai.

> Docker setup Windows par build-test nahi hua - VPS par pehle paper mein chala kar dekhna.

---

## 15. Suraksha ke niyam (hamesha yaad rakhein)

1. **Pehle paper, phir live** - aur live ke pehle din sirf 1 lot.
2. **`config\.env` aur `logs\` kisi ko mat do.**
3. **WARP/VPN band** - warna orders reject.
4. **Trade chalte waqt app band mat karo, computer sleep mein nahi.**
5. **Laal patti / laal message ko ignore mat karo** - Angel One app mein check karo.
6. **Angel One app saath mein khuli rakho** - app par pura bharosa karne se pehle har order milao.
7. Live taala (`ANGEL_LIVE_TRADING_CONFIRMED`) **kabhi `.env` ya kisi file mein mat likho** - har baar khud lagao (`start_live.bat` yahi karta hai).
8. Settings badlo to **`check_config.py`** zaroor chalao.
