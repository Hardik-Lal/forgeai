"""Build the two demo document folders from the committed synthesis sources.

    python data/build_demo_data.py

* data/sources/**.md  -> PDFs (the policy / supplier documents)
* catalogue CSV       -> generated deterministically from part templates below
* HR v2 folder        -> the same HR policies with deliberate changes, used to
                         demonstrate regression mode
See data/SYNTHESIS.md for how the source texts were produced.
"""
from __future__ import annotations

import csv
import random
import re
import shutil
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "sources"
DEMOS = ROOT / "demos"


def md_to_pdf(md: str, out: Path) -> None:
    ss = getSampleStyleSheet()
    story, bullets = [], []

    def flush_bullets():
        nonlocal bullets
        if bullets:
            story.append(ListFlowable([ListItem(Paragraph(b, ss["BodyText"]), leftIndent=12) for b in bullets], bulletType="bullet"))
            bullets = []

    for line in md.splitlines():
        line = line.rstrip()
        esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if line.startswith("- "):
            bullets.append(esc[2:])
            continue
        flush_bullets()
        if not line.strip():
            story.append(Spacer(1, 4))
        elif line.startswith("# "):
            story.append(Paragraph(esc[2:], ss["Title"]))
        elif line.startswith("## "):
            story.append(Paragraph(esc[3:], ss["Heading2"]))
        elif line.startswith("### "):
            story.append(Paragraph(esc[4:], ss["Heading3"]))
        else:
            story.append(Paragraph(esc, ss["BodyText"]))
    flush_bullets()
    out.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(out), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm).build(story)


# --------------------------------------------------------------- catalogue
PART_TEMPLATES = [
    # (category, description, equipment, supplier, price range, lead range, warehouses, critical)
    ("Main engine", "Fuel injection valve nozzle", "MAN B&W S50MC-C", "NordMarine", (380, 520), (10, 21), ["Rotterdam", "Singapore"], True),
    ("Main engine", "Exhaust valve spindle", "MAN B&W S50MC-C", "NordMarine", (4200, 5600), (28, 45), ["Rotterdam"], True),
    ("Main engine", "Piston ring set (one cylinder)", "MAN B&W S50MC-C", "NordMarine", (1900, 2600), (21, 35), ["Rotterdam", "Singapore"], True),
    ("Main engine", "Cylinder liner O-ring kit", "MAN B&W S50MC-C", "NordMarine", (140, 210), (7, 14), ["Rotterdam", "Singapore", "Mumbai"], False),
    ("Main engine", "Fuel pump plunger and barrel", "Wartsila RT-flex50", "NordMarine", (6100, 7400), (35, 60), ["Rotterdam"], True),
    ("Main engine", "Starting air valve overhaul kit", "Wartsila RT-flex50", "NordMarine", (650, 900), (14, 28), ["Rotterdam", "Singapore"], False),
    ("Auxiliary engine", "Turbocharger cartridge", "Yanmar 6EY18", "NordMarine", (8800, 10500), (30, 50), ["Singapore"], True),
    ("Auxiliary engine", "Connecting rod bearing shell pair", "Yanmar 6EY18", "NordMarine", (310, 420), (10, 20), ["Singapore", "Mumbai"], True),
    ("Auxiliary engine", "Fuel filter element", "Yanmar 6EY18", "NordMarine", (28, 45), (3, 7), ["Singapore", "Mumbai", "Rotterdam"], False),
    ("Auxiliary engine", "Cylinder head gasket", "Daihatsu 6DK-20", "NordMarine", (95, 140), (7, 14), ["Singapore", "Mumbai"], False),
    ("Auxiliary engine", "Lube oil cooler tube stack", "Daihatsu 6DK-20", "NordMarine", (2300, 3100), (25, 40), ["Singapore"], False),
    ("Purifier", "Alfa Laval bowl disc set", "Alfa Laval MOPX 207", "NordMarine", (1500, 2100), (14, 28), ["Rotterdam", "Singapore"], False),
    ("Purifier", "Purifier major service kit", "Alfa Laval MOPX 207", "NordMarine", (900, 1250), (10, 18), ["Rotterdam", "Singapore", "Mumbai"], False),
    ("Compressor", "Air compressor valve plate assembly", "Sperre HV2/200", "NordMarine", (480, 690), (12, 24), ["Rotterdam", "Singapore"], False),
    ("Compressor", "Compressor piston ring set", "Sperre HV2/200", "NordMarine", (210, 300), (10, 20), ["Rotterdam", "Singapore"], False),
    ("Pumps", "Centrifugal pump mechanical seal", "Shinko SVS 200", "AsiaPac Marine", (320, 460), (7, 14), ["Singapore", "Mumbai"], True),
    ("Pumps", "Pump impeller bronze", "Shinko SVS 200", "AsiaPac Marine", (1100, 1500), (14, 30), ["Singapore"], False),
    ("Pumps", "Ballast pump wear ring set", "Naniwa FSA-250", "AsiaPac Marine", (260, 380), (10, 21), ["Singapore", "Mumbai"], False),
    ("Pumps", "Fire pump shaft sleeve", "Naniwa FSA-250", "AsiaPac Marine", (180, 260), (10, 18), ["Singapore"], True),
    ("Valves", "Butterfly valve DN200 seat", "Generic DN200", "AsiaPac Marine", (150, 230), (5, 12), ["Singapore", "Mumbai"], False),
    ("Valves", "Globe valve DN50 cast steel", "Generic DN50", "AsiaPac Marine", (210, 320), (7, 14), ["Singapore", "Mumbai"], False),
    ("Valves", "Safety relief valve 10 bar", "Generic", "AsiaPac Marine", (340, 480), (10, 21), ["Singapore"], True),
    ("Deck machinery", "Mooring winch brake lining set", "Kawasaki KWM-30", "AsiaPac Marine", (780, 1050), (21, 35), ["Singapore"], False),
    ("Deck machinery", "Hydraulic motor seal kit", "Kawasaki KWM-30", "AsiaPac Marine", (160, 240), (10, 20), ["Singapore", "Mumbai"], False),
    ("Safety", "Lifebuoy light with battery", "SOLAS", "AsiaPac Marine", (55, 80), (3, 7), ["Singapore", "Mumbai"], True),
    ("Safety", "Parachute rocket flare", "SOLAS", "AsiaPac Marine", (95, 130), (7, 14), ["Singapore"], True),
    ("Safety", "SCBA cylinder 6.8 L carbon", "Drager PSS", "AsiaPac Marine", (690, 880), (14, 25), ["Singapore"], True),
    ("Consumables", "Gasket sheet compressed fibre 1.5 mm", "General", "AsiaPac Marine", (65, 95), (3, 7), ["Singapore", "Mumbai"], False),
    ("Consumables", "Engine room cleaning rags (10 kg)", "General", "AsiaPac Marine", (18, 30), (2, 5), ["Singapore", "Mumbai"], False),
    ("Electrical", "Generator AVR module", "Taiyo FE-series", "AsiaPac Marine", (1300, 1750), (21, 40), ["Singapore"], True),
]


def build_catalogue(out: Path, seed: int = 2026) -> None:
    rng = random.Random(seed)
    rows = []
    n = 1
    for cat, desc, equip, sup, (plo, phi), (llo, lhi), whs, critical in PART_TEMPLATES:
        for wh in whs[:2]:
            prefix = {"NordMarine": "NM", "AsiaPac Marine": "AP"}[sup]
            rows.append({
                "part_no": f"BMS-{prefix}-{1000 + n * 7}",
                "description": desc,
                "category": cat,
                "compatible_equipment": equip,
                "supplier": sup,
                "unit_price_usd": f"{rng.uniform(plo, phi):.2f}",
                "stock_qty": str(rng.choice([0, 0, 1, 2, 3, 4, 6, 8, 10, 12, 20, 25, 40])),
                "warehouse": wh,
                "lead_time_days": str(rng.randint(llo, lhi)),
                "min_order_qty": str(rng.choice([1, 1, 1, 2, 5]) if plo < 300 else 1),
                "critical_spare": "yes" if critical else "no",
            })
            n += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"catalogue: {len(rows)} rows -> {out}")


# --------------------------------------------------------------- HR v2 (regression demo)
HR_V2_CHANGES = {
    "Leave_Policy.md": [
        ("Version 3.2, effective 1 April 2026", "Version 4.0, effective 1 October 2026"),
        ("entitled to 24 working days of paid annual leave", "entitled to 26 working days of paid annual leave"),
        ("Up to 10 days of unused annual leave may be carried forward", "Up to 12 days of unused annual leave may be carried forward"),
        ("entitled to 10 working days of paid paternity leave", "entitled to 15 working days of paid paternity leave"),
    ],
    "Travel_and_Expense_Policy.md": [
        ("within 30 days of returning from the trip", "within 21 days of returning from the trip"),
        ("Claims submitted between 31 and 60 days after return", "Claims submitted between 22 and 60 days after return"),
        ("Domestic travel in India: INR 2,500 per day", "Domestic travel in India: INR 3,000 per day"),
    ],
    "Hybrid_Work_and_Conduct_Policy.md": [],  # unchanged on purpose
}


def main() -> None:
    hr_docs = DEMOS / "hr_policy" / "docs"
    hr_v2 = DEMOS / "hr_policy" / "docs_v2"
    sp_docs = DEMOS / "ship_spares" / "docs"
    for d in (hr_docs, hr_v2, sp_docs):
        if d.exists():
            shutil.rmtree(d)
    for md in sorted((SRC / "hr").glob("*.md")):
        text = md.read_text()
        md_to_pdf(text, hr_docs / f"{md.stem}.pdf")
        for old, new in HR_V2_CHANGES.get(md.name, []):
            assert old in text, f"v2 change not applicable: {old}"
            text = text.replace(old, new)
        md_to_pdf(text, hr_v2 / f"{md.stem}.pdf")
    for md in sorted((SRC / "procurement").glob("*.md")):
        md_to_pdf(md.read_text(), sp_docs / f"{md.stem}.pdf")
    build_catalogue(sp_docs / "spares_catalogue.csv")
    print("built:", *[p.relative_to(ROOT) for p in sorted(DEMOS.rglob("*")) if p.is_file() and "runs" not in p.parts], sep="\n  ")


if __name__ == "__main__":
    main()
