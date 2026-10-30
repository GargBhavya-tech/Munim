"""SIMULATED lead sources for the demo and tests: deterministic stand-ins for the GST / MCA / Udyam / FSSAI / ONDC / Places APIs and a permitted
public page. They return records the way a real page would, INCLUDING the personal data a real page tends to carry (an owner's name, a mobile
number, an email, an ID), so the business-only filter has something real to remove. All values are synthetic; nothing here is a real person."""
from __future__ import annotations

import hashlib
from typing import Any, Callable

NAME_A = ("Sri", "Om", "New", "Royal", "Metro", "City", "Green", "Blue Star", "Sai", "Annapurna")
NAME_B = {"kirana": ("Provision Store", "General Stores"), "restaurant": ("Meals", "Tiffin Room"), "chemist": ("Medicals", "Pharmacy"), "d2c": ("Threads", "Naturals")}
OWNERS = ("Asha Rao", "Ravi Gowda", "Meera Iyer", "Imran Khan", "Divya Hegde")
CATEGORIES = tuple(NAME_B)


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:12], 16)


def make_fetcher(source_id: str, per_pincode: int = 4) -> Callable[[list[str]], list[dict[str, Any]]]:
    def fetch(pincodes: list[str]) -> list[dict[str, Any]]:
        out = []
        for pin in pincodes:
            for i in range(per_pincode):
                h = _h(source_id, pin, str(i))
                cat = CATEGORIES[h % len(CATEGORIES)]
                rec = {"name": f"{NAME_A[(h >> 4) % len(NAME_A)]} {NAME_B[cat][(h >> 8) % 2]} {pin[-2:]}{i}", "category": cat,
                       "address": f"{h % 400 + 1}, {pin} Main Road", "pincode": pin,
                       "lat": round(12.9 + (h % 1000) / 10000, 5), "lng": round(77.5 + ((h >> 10) % 1000) / 10000, 5),
                       "phone": f"080{20000000 + h % 9999999}", "phone_kind": "business", "gst_slab": "20-40L",
                       # what a real page also carries, and must NOT be stored:
                       "owner_name": OWNERS[h % len(OWNERS)], "owner_mobile": f"98{h % 100000000:08d}", "email": f"owner{h % 1000}@simulated.invalid",
                       "aadhaar": f"{h % 10**12:012d}", "pan": "ABCDE1234F"}
                if i == 3:                                    # a sole proprietor listed by a personal mobile only
                    rec["phone"], rec["phone_kind"] = f"97{h % 100000000:08d}", "personal"
                out.append(rec)
        return out
    return fetch
