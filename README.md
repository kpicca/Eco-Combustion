# WEMOB Pipeline README

## Overview
The WEMOB Pipeline is a daily automation system that processes municipal fleet telemetry logs stored in Google Drive.  
It performs the following tasks:

1. Downloads a daily ZIP of `.log` files from the **LogsWemob** Google Drive folder.  
2. Extracts and validates exactly **24 log files**.  
3. Runs **detec.py** to extract RFID tags and telematics metrics (speed, fuel, RPM, etc.).  
4. Generates five structured output files:
   - `wemob_metrics_{date}.csv` (all extracted telematics metrics)
   - `plain_tag_counts.csv`
   - `tag_counts.csv`
   - `unique_tags.txt`
   - `summary.txt`
5. Uploads these outputs to a dated review folder in Google Drive.  
6. Generates a `manifest_<month_day>.json` audit file.  
7. Cleans the workspace and repeats for the next ZIP.

---

## Repository Structure
```
wemob-pipeline/
├── automate.py
├── detec.py
├── creds/
│   └── client_secret.json   ← Google API OAuth credentials
├── data/
│   ├── processed/           ← Output files from detec.py
│   └── state/               ← OAuth token, run logs, manifests
├── tests/                   ← Temporary extracted log files
```

---

## Core Scripts

### detec.py
Extracts:
- All RFID tags (`BBE000` + digits)
- Vehicle + RFID reader metrics from DISP1 lines and JSON objects  
Outputs written to `data/processed/`.

Example:
```bash
python detec.py tests -r --digits 18 --include-plain
```

### automate.py
Full end-to-end automation:
- Downloads ZIPs from Drive
- Validates log count
- Runs detec.py
- Uploads outputs
- Creates manifest and cleans temporary files

Example:
```bash
python automate.py --since 5d
```

---

## Daily Workflow Summary
1. ZIP appears in Drive → LogsWemob  
2. Extract into `tests/`  
3. Validate 24 logs  
4. Run regex extraction via `detec.py`  
5. Upload **5 output files** → Drive reviews/<month day>/  
6. Save manifest JSON  
7. Wipe and repeat  

---

## Google Drive Credentials
- Stored at: `creds/client_secret.json`
- OAuth token (`token.json`) auto-saved to `data/state/` after first run  

If authentication breaks, delete `token.json` and rerun.

---

## Windows Task Scheduler

**Program/script:**
```
C:\Path\To\Python\python.exe
```

**Arguments:**
```
automate.py --limit 1 --force
```

**Start in:**
```
C:\Path\To\wemob-pipeline
```

Runs daily with no user interaction required.

---

## Outputs

| Location | Contents |
|---------|----------|
| `data/processed/` | All generated CSV + TXT outputs |
| Google Drive `reviews/<month day>/` | Same 5 outputs + manifest.json |

---

## Troubleshooting

| Problem | Fix |
|--------|----|
| Missing outputs | Check `data/state/last_run.log` |
| ZIP skipped | Wrong log count or already processed folder exists |
| OAuth errors | Delete `data/state/token.json` and rerun |
| Task Scheduler says “path not found” | Ensure correct **Start in** directory |
| Slow download issues | Try rerunning with `--force` |

---

## Contact
Maintainer: Kate Picca  
Organization: Eco-Combustion  
