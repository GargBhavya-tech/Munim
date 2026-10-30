"""Generate the 20 SYNTHETIC payment screenshots for Scam Shield (#86) and their manifest. Deterministic; no real data.

    python scripts/make_synthetic_screenshots.py

Each screenshot is an SVG "UPI success" screen with labelled text fields (amount, time, reference, app). ``setup`` in the manifest says
which payments the test world must contain (the merchant's own, another merchant's, or none), relative to the sim clock's first minute.
"""
from __future__ import annotations

from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "fixtures" / "screenshots"
MID, OTHER = "M-0003", "M-0004"
APPS = ["PhonePe", "Google Pay", "BHIM", "Paytm"]

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640" viewBox="0 0 360 640">
  <rect width="360" height="640" fill="#f4f6fb"/>
  <text x="180" y="60" text-anchor="middle" font-size="14" fill="#555">SIMULATED - synthetic test screen</text>
  <circle cx="180" cy="150" r="44" fill="#16a34a"/>
  <text x="180" y="160" text-anchor="middle" font-size="40" fill="#fff">✓</text>
  <text x="180" y="240" text-anchor="middle" font-size="20" fill="#111">Payment successful</text>
  <text id="amount" x="180" y="300" text-anchor="middle" font-size="38" fill="#111"{blur_amount}>₹{amount}</text>
  <text x="40" y="380" font-size="14" fill="#555">Paid at</text>
  <text id="time" x="320" y="380" text-anchor="end" font-size="14" fill="#111"{blur_time}>{time}</text>
  <text x="40" y="420" font-size="14" fill="#555">UPI Ref</text>
  <text id="reference" x="320" y="420" text-anchor="end" font-size="14" fill="#111"{blur_reference}>{reference}</text>
  <text x="40" y="460" font-size="14" fill="#555">App</text>
  <text id="app" x="320" y="460" text-anchor="end" font-size="14" fill="#111">{app}</text>
</svg>
"""


def svg(amount_paise: int, minute: int, reference: str, app: str, blur: tuple[str, ...] = ()) -> str:
    rupees = f"{amount_paise // 100:,}" + (f".{amount_paise % 100:02d}" if amount_paise % 100 else "")
    hh, mm = divmod(9 * 60 + minute, 60)                 # the sim clock opens at 09:00 IST
    b = {f"blur_{k}": (f' data-blur="{k}"' if k in blur else "") for k in ("amount", "time", "reference")}
    return SVG.format(amount=rupees, time=f"{hh:02d}:{mm:02d}", reference=reference, app=app, **b)


def cases() -> list[dict]:
    out = []
    for i in range(8):                                    # real: the merchant's own successful payment
        amt, ref = (12000 + 4700 * i) * (1 if i % 2 else 10), f"5{i}11223344{i}{i}"
        out.append({"file": f"real_{i + 1:02d}.svg", "kind": "real", "expected": "MATCH", "amount_paise": amt, "minute": 2 + i, "reference": ref,
                    "setup": [{"merchant_id": MID, "amount_paise": amt, "reference": ref, "status": "SUCCESS", "minute": 2 + i}]})
    for i in range(6):                                    # fake: a made-up reference and nothing paid
        amt = 200000 + 15000 * i
        out.append({"file": f"fake_{i + 1:02d}.svg", "kind": "fake", "expected": "NO_MATCH", "amount_paise": amt, "minute": 3 + i,
                    "reference": f"9{i}00000000{i}{i}", "setup": []})
    for i in range(3):                                    # misread: the amount is smudged, so it is not trusted
        amt, ref = 50000 + 1000 * i, f"7{i}55667788{i}{i}"
        out.append({"file": f"misread_{i + 1:02d}.svg", "kind": "misread", "expected": "AMBIGUOUS", "amount_paise": amt, "minute": 4 + i,
                    "reference": ref, "blur": ["amount"], "setup": [{"merchant_id": MID, "amount_paise": amt, "reference": ref, "status": "SUCCESS", "minute": 4 + i}]})
    out.append({"file": "edited_amount.svg", "kind": "edited", "expected": "MISMATCH", "amount_paise": 200000, "minute": 5, "reference": "611223344556",
                "setup": [{"merchant_id": MID, "amount_paise": 20000, "reference": "611223344556", "status": "SUCCESS", "minute": 5}]})
    out.append({"file": "other_merchant.svg", "kind": "reused", "expected": "MISMATCH", "amount_paise": 150000, "minute": 6, "reference": "622334455667",
                "setup": [{"merchant_id": OTHER, "amount_paise": 150000, "reference": "622334455667", "status": "SUCCESS", "minute": 6}]})
    out.append({"file": "pending.svg", "kind": "pending", "expected": "PENDING", "amount_paise": 88800, "minute": 7, "reference": "633445566778",
                "setup": [{"merchant_id": MID, "amount_paise": 88800, "reference": "633445566778", "status": "PENDING", "minute": 7}]})
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = cases()
    for n, c in enumerate(rows):
        (OUT / c["file"]).write_text(svg(c["amount_paise"], c["minute"], c["reference"], APPS[n % len(APPS)], tuple(c.get("blur", ()))), encoding="utf-8")
    manifest = {"provenance": "SYNTHETIC", "merchant_id": MID, "other_merchant_id": OTHER, "clock_opens": "09:00", "screenshots": rows}
    (OUT / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"wrote {len(rows)} screenshots to {OUT}")


if __name__ == "__main__":
    main()
