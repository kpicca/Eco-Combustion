#!/usr/bin/env python3
"""
WEMOB log → clean dataset (English, Claude-ready)
-------------------------------------------------
Ingest WEMOB *.log files (optionally inside *.zip), parse key telemetry fields,
optionally use Claude as a fallback parser, and export a tidy table for dashboards.

OUTPUT COLUMNS (English)
- timestamp_iso (UTC ISO8601)
- unix_ts (int)
- vehicle_id (IMEI if that's what's present)
- tag (RFID / segment ID)
- latitude, longitude (float; null if unavailable)
- speed_kmh (float)
- fuel_used_l (float; cumulative)
- idle_fuel_l (float; cumulative if present)
- idle_time_s (int; cumulative if present)
- distance_km (float; cumulative if present)
- weight_total_kg (float; Gross Combination Vehicle Weight if present)
- axle_weight_1_kg, axle_weight_2_kg, ... (float; if present)
- co2_kg (float; from log or computed from fuel_used_l * 2.68 when missing)
- source (file::line pointer)
- line_hash (stable hash of raw line)

USAGE EXAMPLES
    python wemob_pipeline_en.py --input ./data/logs --out ./data/processed \
        --use-claude false

    python wemob_pipeline_en.py --input ./data/logs --out ./data/processed \
        --use-claude true --claude-model claude-3-5-sonnet-20240620

NOTES
- Requires: pandas, pyarrow (optional), anthropic (if using Claude)
- Set ANTHROPIC_API_KEY in your environment if --use-claude true
- Designed to be re-runnable: we deduplicate by (vehicle_id, unix_ts, line_hash)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

try:
    import pandas as pd
except ImportError:
    print("Please `pip install pandas pyarrow anthropic`", file=sys.stderr)
    raise

# ---------------------------
# Canonical columns (English)
# ---------------------------
CANON_COLS = [
    "timestamp_iso",
    "unix_ts",
    "vehicle_id",
    "tag",
    "latitude",
    "longitude",
    "speed_kmh",
    "fuel_used_l",
    "idle_fuel_l",
    "idle_time_s",
    "distance_km",
    "weight_total_kg",
    "co2_kg",
    # dynamic axle weights will be added at runtime if found
    "source",
    "line_hash",
]

# ---------------------------
# Patterns (deterministic first)
# ---------------------------
PAT_UNIXTS = re.compile(r"\\bCodec13, timestamp:\s*(?P<unix>\\d+)\\b")
PAT_SPEED1 = re.compile(r"\\[Code8\\].*?speed:\s*(?P<spd>\\d+)\\s*km/h")
PAT_SPEED_CAN = re.compile(r"CAN Speed\\(km/h\\):\\s*(?P<canspd>\\d+)")
PAT_TAG_RFID = re.compile(r"Filtering repeated RFID tag ID:\s*'(?P<tag>\\d+)'")
PAT_TAG_SERIAL = re.compile(r"Serial port data\\[\\d+\\]:\s*'[^']*(?P<tag>BBE\\d+)[^']*'")
PAT_VEHICLE = re.compile(r"\\bDISP1\\s+(?P<imei>\\d{12,17})\\b")
PAT_VEHICLE_WECOM = re.compile(r"\bWECOM\s+addr\s*=\s*[^,]*,[^,]*,[^,]*,(?P<imei>\d{12,17})\b")
PAT_FUEL_TOTAL = re.compile(r"Engine total fuel used(?:\\[\\d+\\])?:\\s*(?P<fuel>\\d+)\\s*L")
PAT_GVW = re.compile(r"Gross Combination Vehicle Weight\\s*(?P<gcw>\\d+)\\s*kg")
PAT_AXLE = re.compile(r"Axle weight\\s*(?P<axle>\\d+):\\s*(?P<kg>\\d+)\\s*kg")
PAT_DATE_ARROW = re.compile(r"->\\s*(?P<date>\\d{4}\\.\\d{2}\\.\\d{2}-\\d{2}\\.\\d{2}\\.\\d{2})")

CSV_SPLIT = re.compile(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)")

# ---------------------------
# Claude configuration
# ---------------------------
@dataclass
class ClaudeCfg:
    enabled: bool = False
    model: str = "claude-3-5-sonnet-20240620"
    batch_size: int = 40
    temperature: float = 0.0

CLAUDE_PROMPT = (
    "You are a strict log-to-JSON parser. For each telemetry line, extract only these keys if present: "
    "timestamp_iso (UTC ISO8601), unix_ts (int), vehicle_id, tag, latitude (number), longitude (number), "
    "speed_kmh (number), fuel_used_l (number), idle_fuel_l (number), idle_time_s (int), distance_km (number), "
    "weight_total_kg (number), co2_kg (number). Use null for missing fields. Return ONLY a JSON array."
)

# ---------------------------
# Utilities
# ---------------------------

def line_hash(line: str) -> str:
    return blake2b(line.encode("utf-8", "ignore"), digest_size=10).hexdigest()


def unix_to_iso(u: Optional[int]) -> Optional[str]:
    if u is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(u)).replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


def arrow_to_iso(s: str) -> Optional[str]:
    # Format: YYYY.MM.DD-HH.MM.SS
    try:
        dt = datetime.strptime(s, "%Y.%m.%d-%H.%M.%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None

# ---------------------------
# Deterministic parse of a single line
# ---------------------------

def parse_line_det(line: str) -> Dict[str, Optional[object]]:
    out: Dict[str, Optional[object]] = {k: None for k in CANON_COLS}

    # vehicle id (IMEI) if present
    m = PAT_VEHICLE.search(line)
    if m:
        out["vehicle_id"] = m.group("imei")
    else:
        # NEW: also try WECOM addr pattern
        mw = PAT_VEHICLE_WECOM.search(line)
        if mw:
            out["vehicle_id"] = mw.group("imei")

    # unix timestamp
    mu = PAT_UNIXTS.search(line)
    if mu:
        u = int(mu.group("unix"))
        out["unix_ts"] = u
        out["timestamp_iso"] = unix_to_iso(u)

    # pretty timestamp arrow → ISO (as a fallback if unix missing)
    md = PAT_DATE_ARROW.search(line)
    if md and out.get("timestamp_iso") is None:
        out["timestamp_iso"] = arrow_to_iso(md.group("date"))

    # speeds
    mcan = PAT_SPEED_CAN.search(line)
    if mcan:
        out["speed_kmh"] = float(mcan.group("canspd"))
    else:
        ms = PAT_SPEED1.search(line)
        if ms:
            out["speed_kmh"] = float(ms.group("spd"))

    # tags
    mt = PAT_TAG_RFID.search(line)
    if mt:
        out["tag"] = mt.group("tag")
    else:
        ms2 = PAT_TAG_SERIAL.search(line)
        if ms2:
            out["tag"] = ms2.group("tag")

    # totals
    mf = PAT_FUEL_TOTAL.search(line)
    if mf:
        out["fuel_used_l"] = float(mf.group("fuel"))

    mg = PAT_GVW.search(line)
    if mg:
        out["weight_total_kg"] = float(mg.group("gcw"))

    # axle weights (dynamic keys will be handled later)
    maxle = PAT_AXLE.search(line)
    if maxle:
        axle_no = int(maxle.group("axle"))
        kg = float(maxle.group("kg"))
        out[f"axle_weight_{axle_no}_kg"] = kg

    # derive co2 from fuel if present & co2 not explicitly present in logs (not seen in sample)
    # we'll fill after aggregation step

    # The minimal requirement for a row is: we only finalize rows later when we can pair unix_ts or timestamp.
    return out

# ---------------------------
# Claude fallback (optional)
# ---------------------------

def parse_lines_claude(lines: List[str], cfg: ClaudeCfg) -> List[Dict[str, Optional[object]]]:
    if not cfg.enabled:
        return [ {} for _ in lines ]
    try:
        import anthropic
    except Exception:
        print("Install `anthropic` and set ANTHROPIC_API_KEY to use --use-claude true", file=sys.stderr)
        return [ {} for _ in lines ]

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = CLAUDE_PROMPT + "\n\nLines to parse:\n" + "\n".join(lines)

    resp = client.messages.create(
        model=cfg.model,
        max_tokens=4096,
        temperature=cfg.temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    # Expect the first text block to be a JSON array
    text_parts = [c.text for c in resp.content if getattr(c, 'type', '') == 'text']
    raw = text_parts[0] if text_parts else "[]"
    try:
        data = json.loads(raw)
        # ensure one output per input line (if Claude returns fewer, pad with {})
        if isinstance(data, list):
            while len(data) < len(lines):
                data.append({})
            return data[:len(lines)]
    except Exception:
        pass
    return [ {} for _ in lines ]

# ---------------------------
# Iterate files (supports both loose .log and .zip containing .log)
# ---------------------------

def iter_sources(input_path: Path) -> Iterator[Tuple[str, Iterator[str]]]:
    """Yield (source_id, line_iter). source_id is a human-readable file id."""
    if input_path.is_dir():
        for p in sorted(input_path.rglob("*")):
            if p.suffix.lower() == ".log":
                def _iter_file(pp=p):
                    with pp.open("r", encoding="utf-8", errors="ignore") as fh:
                        for ln in fh:
                            yield ln.rstrip("\n")
                yield (str(p), _iter_file())
            elif p.suffix.lower() == ".zip":
                zpath = p
                with zipfile.ZipFile(zpath) as zf:
                    for name in zf.namelist():
                        if not name.lower().endswith(".log"):
                            continue
                        def _iter_zip(n=name, z=zpath):
                            with zipfile.ZipFile(z) as zfi:
                                with zfi.open(n) as fh:
                                    for ln in io.TextIOWrapper(fh, encoding="utf-8", errors="ignore"):
                                        yield ln.rstrip("\n")
                        yield (f"{zpath}::{name}", _iter_zip())
    else:
        # single file
        p = input_path
        def _iter_single():
            with p.open("r", encoding="utf-8", errors="ignore") as fh:
                for ln in fh:
                    yield ln.rstrip("\n")
        yield (str(p), _iter_single())

# ---------------------------
# Main processing
# ---------------------------

def process(input_path: Path, out_dir: Path, claude_cfg: ClaudeCfg) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, Optional[object]]] = []
    pending_for_claude: List[str] = []
    pending_sources: List[str] = []

    #Remember the last seen vehicle per source file
    last_vehicle_by_source: dict[str, str] = {}

    # We capture per-line deterministic fields and aggregate by (vehicle_id, unix_ts) later.
    for src_id, it in iter_sources(input_path):
        for i, line in enumerate(it, start=1):
            det = parse_line_det(line)
            veh = det.get("vehicle_id")
            if veh:
                # remember for this source
                last_vehicle_by_source[src_id] = str(veh)
            else:
                # reuse last seen for this source if available
                prev = last_vehicle_by_source.get(src_id)
                if prev and (det.get("vehicle_id") is None):
                    det["vehicle_id"] = prev
            has_any = any(v is not None for v in det.values())

            if has_any:
                det["source"] = f"{src_id}::{i}"
                det["line_hash"] = line_hash(line)
                rows.append(det)
            else:
                # keep for Claude batch if enabled
                if claude_cfg.enabled:
                    pending_for_claude.append(line)
                    pending_sources.append(f"{src_id}::{i}")

            # flush Claude batches to keep memory in check
            if claude_cfg.enabled and len(pending_for_claude) >= claude_cfg.batch_size:
                _flush_claude(pending_for_claude, pending_sources, rows, claude_cfg)

    if claude_cfg.enabled and pending_for_claude:
        _flush_claude(pending_for_claude, pending_sources, rows, claude_cfg)

    # Convert list of partial rows (many per ts) to consolidated records keyed by (vehicle_id, unix_ts)
    df = consolidate_rows(rows)

    # Derivations & validation
    df = derive_fields(df)
    df = validate_flags(df)

    # Export
    master_csv = out_dir / "wemob_master_en.csv"
    master_parquet = out_dir / "wemob_master_en.parquet"

    df.to_csv(master_csv, index=False, quoting=csv.QUOTE_MINIMAL)
    try:
        df.to_parquet(master_parquet, index=False)
    except Exception:
        pass

    print(f"Wrote {len(df):,} rows → {master_csv}")
    return master_csv


def _flush_claude(pending_lines: List[str], pending_srcs: List[str], rows: List[Dict], cfg: ClaudeCfg) -> None:
    parsed = parse_lines_claude(pending_lines, cfg)
    for line, src, obj in zip(pending_lines, pending_srcs, parsed):
        if isinstance(obj, dict) and any(obj.values()):
            obj["source"] = src
            obj["line_hash"] = line_hash(line)
            rows.append(obj)
    pending_lines.clear()
    pending_srcs.clear()

# ---------------------------
# Consolidation & derivations
# ---------------------------

def consolidate_rows(rows: List[Dict[str, Optional[object]]]) -> pd.DataFrame:
    # Turn dynamic axle keys into columns and group by (vehicle_id, unix_ts)
    # We also allow timestamp-only rows (without unix) by deriving unix from timestamp if possible.
    for r in rows:
        # backfill unix from timestamp if needed
        if r.get("unix_ts") is None and r.get("timestamp_iso"):
            try:
                dt = datetime.fromisoformat(str(r["timestamp_iso"]))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                r["unix_ts"] = int(dt.timestamp())
            except Exception:
                pass

    # Filter rows that at least have a unix_ts and vehicle_id; otherwise cannot key them
    keyed = [r for r in rows if r.get("unix_ts") is not None and r.get("vehicle_id") is not None]

    # Build DataFrame and aggregate by taking the "richest" non-null values
    df = pd.DataFrame(keyed)

    # Failure message
    if df.empty or "vehicle_id" not in df.columns or "unix_ts" not in df.columns:
    raise RuntimeError(
        "No keyable rows: parser didn’t find both vehicle_id and unix_ts. "
        "Check that your logs contain either 'DISP1 <IMEI>' or "
        "'WECOM  addr = ...,<IMEI>' lines AND 'Codec13, timestamp: <unix>' lines."
    )

    # Bring in all axle columns dynamically
    axle_cols = sorted([c for c in df.columns if c.startswith("axle_weight_")])
    base_cols = CANON_COLS[:-2] + axle_cols + ["source", "line_hash"]

    # group & aggregate: prefer max non-null per column (for numeric) or first non-null for strings
    def prefer(a):
        # for numeric: max; for strings: first non-null
        if a.dtype.kind in "biufc":
            return a.dropna().max() if a.dropna().size else None
        else:
            return a.dropna().iloc[0] if a.dropna().size else None

    agg = {c: prefer for c in set(df.columns) if c not in {"source", "line_hash"}}
    df_grouped = df.groupby(["vehicle_id", "unix_ts"], as_index=False).agg(agg)

    # Recompute ISO timestamp from unix_ts (standardized UTC)
    df_grouped["timestamp_iso"] = df_grouped["unix_ts"].apply(unix_to_iso)

    # Ensure columns exist
    for c in CANON_COLS:
        if c not in df_grouped.columns:
            df_grouped[c] = None
    for c in axle_cols:
        if c not in df_grouped.columns:
            df_grouped[c] = None

    # Order columns: base + axle + source/line_hash if they exist
    ordered = [
        "timestamp_iso","unix_ts","vehicle_id","tag","latitude","longitude","speed_kmh",
        "fuel_used_l","idle_fuel_l","idle_time_s","distance_km","weight_total_kg","co2_kg"
    ] + axle_cols + [c for c in ["source","line_hash"] if c in df_grouped.columns]

    return df_grouped[ordered]


def derive_fields(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # CO2 from diesel if missing but fuel present
    mask = df["co2_kg"].isna() & df["fuel_used_l"].notna()
    df.loc[mask, "co2_kg"] = df.loc[mask, "fuel_used_l"] * 2.68

    # Type coercions to sensible dtypes
    for col in ["latitude","longitude","speed_kmh","fuel_used_l","idle_fuel_l","distance_km","weight_total_kg","co2_kg"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["idle_time_s","unix_ts"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    return df


def validate_flags(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # simple sanity checks
    if "latitude" in df.columns:
        df["flag_bad_gps"] = ~df["latitude"].between(-90, 90) | ~df["longitude"].between(-180, 180)
    else:
        df["flag_bad_gps"] = False

    df["flag_speed_spike"] = df["speed_kmh"].fillna(0) > 130
    # For cumulative fields, we could check monotonicity per vehicle if needed in a later pass.
    return df

# ---------------------------
# CLI
# ---------------------------

def main():
    ap = argparse.ArgumentParser(description="WEMOB log → clean dataset (English, Claude-ready)")
    ap.add_argument("--input", required=True, help="Folder or file: *.log and/or *.zip containing *.log")
    ap.add_argument("--out", required=True, help="Output folder")
    ap.add_argument("--use-claude", default="false", help="Use Claude fallback (true/false)")
    ap.add_argument("--claude-model", default="claude-3-5-sonnet-20240620")
    ap.add_argument("--claude-batch", type=int, default=40)
    args = ap.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.out)

    cfg = ClaudeCfg(
        enabled=str(args["use_claude"]) if isinstance(args, dict) else str(args.use_claude),
        model=args.claude_model,
        batch_size=args.claude_batch,
    )
    cfg.enabled = str(cfg.enabled).lower() in {"1","true","yes","y"}

    process(input_path, out_dir, cfg)

if __name__ == "__main__":
    main()
