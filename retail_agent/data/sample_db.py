"""Build a small, synthetic, thelook-shaped DuckDB database for offline runs and tests.

Same tables, columns and categorical values as bigquery-public-data.thelook_ecommerce,
deterministic (seeded), a few MB. The PII columns are filled with obviously fake
values so the PII protections can be tested end to end. It is NOT thelook data:
numbers from offline mode say nothing about the real dataset.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BRANDS = [
    "Calvin Klein", "Levi's", "Carhartt", "Columbia", "Nike", "Hanes", "Tommy Hilfiger", "Ralph Lauren",
    "Diesel", "Allegra K", "Champion", "Under Armour", "Quiksilver", "Speedo", "Patagonia", "The North Face",
    "Lucky Brand", "Joe's Jeans", "7 For All Mankind", "Dockers", "Wrangler", "Volcom", "Billabong", "Roxy",
]
CATEGORIES = {
    "Women": ["Dresses", "Jeans", "Tops & Tees", "Sweaters", "Intimates", "Swim", "Outerwear & Coats",
              "Accessories", "Active", "Shorts", "Skirts"],
    "Men": ["Jeans", "Outerwear & Coats", "Tops & Tees", "Sweaters", "Active", "Shorts", "Swim",
            "Accessories", "Suits & Sport Coats", "Socks"],
}
PRICE_BAND = {
    "Dresses": (30, 160), "Jeans": (35, 180), "Tops & Tees": (10, 60), "Sweaters": (30, 140),
    "Intimates": (8, 45), "Swim": (20, 110), "Outerwear & Coats": (60, 420), "Accessories": (8, 90),
    "Active": (15, 90), "Shorts": (15, 70), "Skirts": (20, 90), "Suits & Sport Coats": (120, 600), "Socks": (5, 25),
}
RETURN_PROPENSITY = {"Jeans": 1.6, "Dresses": 1.4, "Swim": 1.3, "Outerwear & Coats": 1.1}
GEO = [  # (country, state, city, weight, order-frequency multiplier)
    ("United States", "California", "Los Angeles", 9, 1.15),
    ("United States", "Texas", "Houston", 7, 0.7),
    ("United States", "New York", "New York", 6, 1.2),
    ("United States", "Florida", "Miami", 5, 0.95),
    ("United States", "Illinois", "Chicago", 4, 1.0),
    ("United States", "Washington", "Seattle", 3, 1.1),
    ("China", "Guangdong", "Shenzhen", 8, 1.0),
    ("China", "Shanghai", "Shanghai", 6, 1.05),
    ("Brazil", "São Paulo", "São Paulo", 5, 0.9),
    ("United Kingdom", "England", "London", 4, 1.0),
    ("Germany", "Bayern", "Munich", 3, 1.0),
    ("France", "Île-de-France", "Paris", 3, 1.0),
    ("Spain", "Madrid", "Madrid", 2, 0.95),
    ("South Korea", "Seoul", "Seoul", 3, 1.1),
    ("Japan", "Tokyo", "Tokyo", 2, 1.0),
    ("Australia", "New South Wales", "Sydney", 2, 1.0),
    ("Poland", "Mazowieckie", "Warsaw", 1, 0.9),
]
SOURCES = [("Search", 70), ("Organic", 15), ("Facebook", 6), ("Email", 5), ("Display", 4)]
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew", "Robin"]
LAST = ["Smith", "Garcia", "Chen", "Kowalski", "Müller", "Silva", "Kim", "Brown", "Dubois", "Rossi", "Novak", "Sato"]
STREETS = ["Oak Street", "Maple Avenue", "Pine Road", "Cedar Lane", "Elm Drive", "Lake Boulevard"]


def _ts(d: datetime) -> pd.Timestamp:
    return pd.Timestamp(d)


def build(path: Path, *, users: int = 4000, seed: int = 7, start: date = date(2023, 1, 1), end: date | None = None) -> dict[str, int]:
    import duckdb

    rng = random.Random(seed)
    end = end or date.today()
    span_days = (end - start).days
    t0 = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    # Nothing happens after this moment: now when the data ends today, else the end of `end`.
    horizon = min(datetime.now(timezone.utc), datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1))

    products = []
    pid = 0
    for dept, cats in CATEGORIES.items():
        for cat in cats:
            for _ in range(rng.randint(14, 22)):
                pid += 1
                brand = rng.choice(BRANDS)
                lo, hi = PRICE_BAND[cat]
                retail = round(rng.uniform(lo, hi), 2)
                products.append({
                    "id": pid, "cost": round(retail * rng.uniform(0.38, 0.62), 2), "category": cat,
                    "name": f"{brand} {rng.choice(['Classic', 'Slim', 'Relaxed', 'Essential', 'Vintage'])} {cat}",
                    "brand": brand, "retail_price": retail, "department": dept,
                    "sku": f"{rng.getrandbits(64):016X}", "distribution_center_id": rng.randint(1, 10),
                })
    by_dept = {d: [p for p in products if p["department"] == d] for d in CATEGORIES}

    geo_weights = [g[3] for g in GEO]
    src_names, src_weights = zip(*SOURCES)
    user_rows, order_rows, item_rows = [], [], []
    order_id = item_id = inventory_id = 0
    for uid in range(1, users + 1):
        country, state, city, _, freq = rng.choices(GEO, weights=geo_weights)[0]
        gender = rng.choice("MF")
        first, last = rng.choice(FIRST), rng.choice(LAST)
        signup = min(t0 + timedelta(days=rng.uniform(0, span_days), hours=rng.uniform(0, 24)), horizon)
        user_rows.append({
            "id": uid, "first_name": first, "last_name": last,
            "email": f"{first.lower()}.{last.lower()}{uid}@example.com", "age": rng.randint(12, 70),
            "gender": gender, "state": state, "street_address": f"{rng.randint(1, 9999)} {rng.choice(STREETS)}",
            "postal_code": f"{rng.randint(10000, 99999)}", "city": city, "country": country,
            "latitude": round(rng.uniform(-40, 60), 6), "longitude": round(rng.uniform(-120, 140), 6),
            "traffic_source": rng.choices(src_names, weights=src_weights)[0], "created_at": _ts(signup),
            "user_geom": None,
        })
        n_orders = rng.choices([0, 1, 2, 3, 4, 5], weights=[8, 50, 22, 11, 6, 3])[0]
        n_orders = max(0, round(n_orders * freq)) if n_orders else 0
        when = signup
        dept = "Women" if gender == "F" else "Men"
        for _ in range(n_orders):
            when = when + timedelta(days=rng.expovariate(1 / 75), hours=rng.uniform(0, 24))
            if when > horizon:
                break
            order_id += 1
            age_days = (end - when.date()).days
            if age_days < 4:
                status = rng.choices(["Processing", "Shipped", "Cancelled"], weights=[60, 30, 10])[0]
            else:
                status = rng.choices(["Complete", "Shipped", "Processing", "Cancelled", "Returned"],
                                     weights=[30, 30, 15, 15, 10])[0]
            # Seasonality in volume: Q4 orders carry more items.
            n_items = rng.choices([1, 2, 3, 4], weights=[60, 25, 10, 5] if when.month < 10 else [45, 30, 15, 10])[0]
            picks = [rng.choice(by_dept[dept]) for _ in range(n_items)]
            if status == "Returned" and not any(p["category"] in RETURN_PROPENSITY for p in picks) and rng.random() < 0.4:
                status = "Complete"
            shipped = when + timedelta(hours=rng.uniform(6, 72)) if status in ("Shipped", "Complete", "Returned") else None
            delivered = shipped + timedelta(days=rng.uniform(1, 6)) if shipped and status in ("Complete", "Returned") else None
            returned = delivered + timedelta(days=rng.uniform(1, 12)) if delivered and status == "Returned" else None
            # An order is only as far along as the clock allows: no event after the horizon.
            if returned and returned > horizon:
                status, returned = "Complete", None
            if delivered and delivered > horizon:
                status, delivered = "Shipped", None
            if shipped and shipped > horizon:
                status, shipped = "Processing", None
            order_rows.append({
                "order_id": order_id, "user_id": uid, "status": status, "gender": gender,
                "created_at": _ts(when), "returned_at": _ts(returned) if returned else None,
                "shipped_at": _ts(shipped) if shipped else None, "delivered_at": _ts(delivered) if delivered else None,
                "num_of_item": n_items,
            })
            for product in picks:
                item_id += 1
                inventory_id += 1
                item_rows.append({
                    "id": item_id, "order_id": order_id, "user_id": uid, "product_id": product["id"],
                    "inventory_item_id": inventory_id, "status": status, "created_at": _ts(when),
                    "shipped_at": _ts(shipped) if shipped else None,
                    "delivered_at": _ts(delivered) if delivered else None,
                    "returned_at": _ts(returned) if returned else None,
                    "sale_price": round(product["retail_price"] * rng.choice([1, 1, 1, 0.9, 0.8]), 2),
                })

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = duckdb.connect(str(path))
    frames = {
        "products": pd.DataFrame(products),
        "users": pd.DataFrame(user_rows),
        "orders": pd.DataFrame(order_rows),
        "order_items": pd.DataFrame(item_rows),
    }
    for name, frame in frames.items():
        for col in frame.columns:
            if col.endswith("_at"):
                frame[col] = pd.to_datetime(frame[col], utc=True)
        conn.register(f"_{name}", frame)
        conn.execute(f"CREATE TABLE {name} AS SELECT * FROM _{name}")
    conn.execute("ALTER TABLE users ALTER COLUMN user_geom TYPE VARCHAR")
    conn.close()
    return {name: len(frame) for name, frame in frames.items()}
