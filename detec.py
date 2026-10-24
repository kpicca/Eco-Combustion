#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import re
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime
import json


def normalize_tag(tag):
    """
    Extracts numeric digits from tag strings and pads to 5–6 digits.
    This avoids issues where logs contain weird formatting around the tag.
    """
    if tag is None:
        return None
    s = str(tag).strip()
    m = re.search(r"(\d+)", s)
    return m.group(1) if m else None


def to_int(x):
    """Safe conversion of strings/numbers to int."""
    try:
        return int(x)
    except Exception:
        try:
            return int(float(str(x).strip()))
        except Exception:
            return None


def _get(obj, keys, default=None):
    """
    From a JSON object, return the first matching key from a list
    (case-insensitive). Much more resilient than direct indexing.
    """
    lower = {k.lower(): v for k, v in obj.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None:
            return v
    return default


def iter_json_objects_fast(line: str, max_objs_per_line: int = 50, max_obj_len: int = 20000):
    """
    Extract up to 50 lightweight JSON objects from a single log line.
    Your coworker's method — extremely reliable for messy logs.
    """
    if "{" not in line or "}" not in line:
        return
    depth = 0
    start = None
    count = 0

    for i, ch in enumerate(line):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = line[start:i + 1]
                    if len(chunk) <= max_obj_len:
                        try:
                            yield json.loads(chunk)
                            count += 1
                            if count >= max_objs_per_line:
                                return
                        except Exception:
                            pass
                    start = None


def build_regex(digits_after_bbe000: int) -> re.Pattern:
    """
    Builds the STRICT regex used to extract RFID tags.

    Important details:
    - (?<![0-9A-Fa-f]) ensures the match does NOT have a hex digit before it.
      This prevents catching partial fragments inside larger hex strings.
    - We look for 'BBE000' followed by EXACTLY N digits.
    - (?![0-9A-Fa-f]) ensures the match is not followed by another hex digit.
      Again prevents partial accident matches.
    """
    pattern = rf"(?<![0-9A-Fa-f])BBE000[0-9]{{{digits_after_bbe000}}}(?![0-9A-Fa-f])"
    return re.compile(pattern)

# Regex to optionally capture "plain" tag IDs printed by some logs.
# These are different from the BBE000 tags and come from filtering lines.
RE_PLAIN = re.compile(r"Filtering (?:repeated )?RFID tag ID:\s*'([0-9]{6,})'")
#IMEI appears after DISP1 IMEI-value
RE_IMEI = re.compile(r"DISP1\s+(\d{9,18})")
#timestamp at beginning of line like: "01.00.00"
RE_TIME = re.compile(r"(\d{2}\.\d{2}\.\d{2})")

def iter_log_files(base: Path, recursive: bool):
    """Yields .log files in the folder. Uses **/*.log if recursive."""
    pattern = "**/*.log" if recursive else "*.log"
    yield from base.glob(pattern)

def safe_open_text(path: Path):
    """Opens a file in UTF-8 but ignores decoding errors (logs can be messy)."""
    return path.open("r", encoding="utf-8", errors="ignore")

def main():
    gross_weights = []  # List of dicts: {"ts": ..., "imei": ..., "kg": ...}
    metrics_records = []

    # Argument parser: defines the CLI interface for the extraction script.
    ap = argparse.ArgumentParser(
        description="Extrae tags RFID completos tipo BBE000... de ficheros .log",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    ap.add_argument("folder", type=str, help="Carpeta con .log")
    ap.add_argument("-r", "--recursive", action="store_true", help="Buscar en subcarpetas")
    ap.add_argument("-o", "--outdir", type=str, default="data/processed", help="Carpeta de salida")
    ap.add_argument("--digits", type=int, default=18,
                    help="Número de dígitos EXACTOS tras 'BBE000'")
    ap.add_argument("--include-plain", action="store_true",
                    help="También extrae IDs planos encontrados en texto")
    args = ap.parse_args()

    # Resolve the input/output directories
    base = Path(args.folder).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    # Validate that input folder exists
    if not base.is_dir():
        print(f"ERROR: no existe la carpeta: {base}", file=sys.stderr)
        sys.exit(1)

    # Build regex for BBE000 + N digits
    RE_HEX_STRICT = build_regex(args.digits)

    # Counters accumulate occurrences of each tag
    bbe_tags = Counter()
    plain_tags = Counter() if args.include_plain else None

    files_scanned = 0
    lines_scanned = 0
    run_date = None #date (YYYY-MM-DD) inferred from first log filename

    # Iterate through all log files found
    for f in iter_log_files(base, args.recursive):
        try:
            fname = f.name
            mdate = re.search(r"(\d{4})[_-](\d{2})[_-](\d{2})", fname)
            if mdate:
                file_date = f"{mdate.group(1)}-{mdate.group(2)}-{mdate.group(3)}"
            else:
                file_date = datetime.utcnow().strftime("%Y-%m-%d")
                print(f"[WARN] No date in filename: {fname} -> usando {file_date}")
            if run_date is None:
                run_date = file_date

            with safe_open_text(f) as fh:
                files_scanned += 1
                for line in fh:
                    lines_scanned += 1

                    # -----------------------------
                    # 1) Try JSON objects in line
                    # -----------------------------
                    parsed_any = False
                    for obj in iter_json_objects_fast(line):
                        parsed_any = True

                        # (a) Extract gross weight from JSON when PTO is ON
                        try:
                            if obj.get("ptoStatus") == 1 and "grossWeightKg" in obj:
                                kg = to_int(obj.get("grossWeightKg"))
                                if kg is not None:
                                    imei_raw = obj.get("imei") or "unknown"
                                    imei = "".join(c for c in str(imei_raw) if c.isdigit())

                                    device_type = (
                                        "vehicle" if imei.startswith("35")
                                        else "rfid_reader" if imei.startswith("86")
                                        else "unknown"
                                    )

                                    ts = obj.get("timestamp") or obj.get("ts") or None
                                    if ts is None:
                                        ts = datetime.utcnow().isoformat(timespec="seconds")

                                    gross_weights.append({
                                        "ts": ts,
                                        "imei": imei,
                                        "device_type": device_type,
                                        "kg": kg,
                                        "source": "JSON",
                                    })

                                    metrics_records.append({
                                        "ts": ts,
                                        "imei": imei,
                                        "device_type": device_type,
                                        "metric_type": "gross_weight_kg",
                                        "value": kg,
                                    })
                        except Exception:
                            # Skip any broken JSON entries silently
                            pass

                        # (b) Extract tag from JSON (ID_TAG / TAG / etc.)
                        raw_tag = _get(obj, ["ID_TAG", "id_tag", "TAG", "tag", "ID", "id"])
                        tag = normalize_tag(raw_tag)
                        if tag:
                            bbe_tags[tag] += 1

                    # ----------------------------------------
                    # 2) Fallback: strict regex for BBE000...
                    # ----------------------------------------
                    if not parsed_any:
                        for m in RE_HEX_STRICT.finditer(line):
                            raw = m.group(0)
                            tag = normalize_tag(raw)
                            if tag:
                                bbe_tags[tag] += 1

                    # ----------------------------------------
                    # 2b) Parse DISP1 telematics lines
                    # ----------------------------------------
                    if "[D] DISP1" in line:
                        try:
                            m_imei = RE_IMEI.search(line)
                            m_time = RE_TIME.match(line)
                            if m_imei and m_time:
                                imei = "".join(c for c in m_imei.group(1) if c.isdigit())
                                hh, mm, ss = m_time.group(1).split(".")
                                ts = f"{file_date} {hh}:{mm}:{ss}"

                                device_type = (
                                    "vehicle" if imei.startswith("35")
                                    else "rfid_reader" if imei.startswith("86")
                                    else "unknown"
                                )

                                line_low = line.lower()

                                def add_metric(metric_type, value):
                                    if value is None:
                                        return
                                    metrics_records.append({
                                        "ts": ts,
                                        "imei": imei,
                                        "device_type": device_type,
                                        "metric_type": metric_type,
                                        "value": value,
                                    })

                                # Speed
                                if "can speed" in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    add_metric("speed_kmh", to_int(mv.group(1)) if mv else None)

                                # RPM
                                elif "rpm" in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    add_metric("rpm", to_int(mv.group(1)) if mv else None)

                                # Fuel level (litres or percent, but NOT counted totals)
                                elif "fuel level" in line_low and "(counted)" not in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    val = to_int(mv.group(1)) if mv else None
                                    if "percent" in line_low or "(%)" in line_low:
                                        add_metric("fuel_percent", val)
                                    elif "litre" in line_low:
                                        add_metric("fuel_liters", val)

                                # Engine temperature
                                elif "engine temperature" in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    add_metric("engine_temp_c", to_int(mv.group(1)) if mv else None)

                                # Engine worktime
                                elif "engine worktime" in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    add_metric("engine_hours", to_int(mv.group(1)) if mv else None)

                                # CAN odometer
                                elif "can odometer" in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    add_metric("can_odometer_km", to_int(mv.group(1)) if mv else None)

                                # Total mileage
                                elif "total mileage" in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    add_metric("total_mileage_km", to_int(mv.group(1)) if mv else None)

                                # Engine total fuel used
                                elif "engine total fuel used" in line_low:
                                    mv = re.search(r"(\d+)", line)
                                    add_metric("total_fuel_used_l", to_int(mv.group(1)) if mv else None)

                        except Exception:
                            # Don't let a single bad line crash the run
                            pass

                    # ----------------------------------------
                    # 4) Optional: plain tag IDs from text
                    # ----------------------------------------
                    if plain_tags is not None:
                        m2 = RE_PLAIN.search(line)
                        if m2:
                            plain_tags[m2.group(1)] += 1

        except Exception as e:
            # Warn but continue scanning other files
            print(f"WARNING: no se pudo leer {f}: {e}", file=sys.stderr)

    # Sort unique tags for stable output
    unique_bbe = sorted(bbe_tags.keys())

    # Write unique tags output file
    (outdir / "unique_tags.txt").write_text(
        "\n".join(unique_bbe) + ("\n" if unique_bbe else ""), encoding="utf-8"
    )

    # Write tag count CSV
    with (outdir / "tag_counts.csv").open("w", encoding="utf-8") as csvf:
        csvf.write("tag,count\n")
        for tag in sorted(bbe_tags):
            csvf.write(f"{tag},{bbe_tags[tag]}\n")

    # Optional plain tag counts
    if plain_tags is not None:
        with (outdir / "plain_tag_counts.csv").open("w", encoding="utf-8") as csvf:
            csvf.write("plain_tag,count\n")
            for tag in sorted(plain_tags):
                csvf.write(f"{tag},{plain_tags[tag]}\n")

    # Human-readable summary file
    summary = [
        f"Folder:          {base}",
        f"Recursive:       {args.recursive}",
        f"Files scanned:   {files_scanned}",
        f"Lines scanned:   {lines_scanned}",
        f"Regex:           (?<![0-9A-Fa-f])BBE000[0-9]{{{args.digits}}}(?![0-9A-Fa-f])",
        f"Unique BBE tags: {len(unique_bbe)}",
        f"Outputs:",
        f"  - {outdir / 'unique_tags.txt'}",
        f"  - {outdir / 'tag_counts.csv'}",
    ]
    if plain_tags is not None:
        summary.append(f"  - {outdir / 'plain_tag_counts.csv'}")

    (outdir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")

    # Write gross weight CSV (from JSON or AI fallback if kg available)
    if gross_weights:
        with (outdir / "gross_weights.csv").open("w", encoding="utf-8") as f:
            f.write("timestamp,imei,weight_kg\n")
            for row in gross_weights:
                f.write(f"{row['ts']},{row['imei']},{row['kg']}\n")

    # -----------------------------------------------
    # Write telematics metrics CSV (JSON + regex only)
    # -----------------------------------------------
    if metrics_records :
        suffix = run_date if run_date is not None else "unknown_date"
        out_csv = outdir / f"wemob_metrics_{suffix}.csv"

        with out_csv.open("w", encoding="utf-8") as f:
            # Gather all possible keys across records
            keys = sorted({k for row in metrics_records for k in row.keys()})
            f.write(",".join(keys) + "\n")

            for row in metrics_records:
                f.write(",".join(str(row.get(k, "")) for k in keys) + "\n")

        print(f"[OK] Telematics CSV generated: {out_csv}")
    else:
        print("[WARN] No telematics rows extracted — JSON/regex found nothing.")


    # Print quick terminal summary
    print(f"Unique BBE000 + {args.digits} dígitos: {len(unique_bbe)}")
    if unique_bbe:
        print("Primeros 10 (ordenados):")
        for t in unique_bbe[:10]:
            print("  ", t)
    print(f"\nGuardado en: {outdir}")

if __name__ == "__main__":
    main()
