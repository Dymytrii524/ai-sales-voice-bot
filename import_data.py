"""Builds the bot's CSV files in data/ from the Berry Farm database export.

Usage:
    python import_data.py [EXPORT_DIR] [FAQ_CSV]

Re-run it whenever you get a fresh export. It overwrites
data/catalog.csv, data/faq.csv and data/info.csv.
data/script.csv and data/texts.csv are written by hand and are not touched.
"""
import csv
import html
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

csv.field_size_limit(10**9)

DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
EXPORT_DIR = os.path.join(DOWNLOADS, "Telegram Desktop", "berry-csv", "berry-csv")
FAQ_CSV = os.path.join(
    DOWNLOADS,
    "3c67889f-f746-4a88-98c0-474ff05ba972_ExportBlock-ab2300ef-b755-446a-bdf7-07cfbb45af9a",
    "Berry_Farm_Faq_With_Videos_Reordered 1e6fce64d9cb80cb98ece8c67e494c95.csv",
)
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOCALE = "uk"

# Category is guessed from the product title first: many products in the
# database have no category or only the parent one ("Плодово-ягідні").
CATEGORY_KEYWORDS = [
    ("лохин", "Лохина"), ("гортенз", "Гортензія"), ("павловн", "Павловнія"),
    ("ківі", "Ківі"), ("жимолост", "Жимолость"), ("ожин", "Ожина"),
    ("фундук", "Фундук"), ("журавлин", "Журавлина"), ("банан", "Банан"),
    ("лаванд", "Лаванда"), ("тополі", "Тополя"), ("троянд", "Троянда"),
    ("хризантем", "Хризантема"), ("орхіде", "Квіти"), ("добриво", "Добрива"),
]


def read(export_dir, name):
    with open(os.path.join(export_dir, name + ".csv"), encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write(name, fieldnames, rows):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path}: {len(rows)} rows")


def guess_category(title, db_category):
    lower = title.lower()
    for keyword, category in CATEGORY_KEYWORDS:
        if keyword in lower:
            return category
    return db_category or "Інше"


def clean_text(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def clean_html(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def translations(rows, key):
    return {r[key]: r for r in rows if r["locale"] == LOCALE}


def build_catalog(export_dir):
    products = read(export_dir, "products")
    titles = translations(read(export_dir, "product_translations"), "product_id")
    categories = translations(read(export_dir, "product_category_translations"), "product_category_id")
    attr_titles = translations(read(export_dir, "attribute_translations"), "attribute_id")
    attr_values = translations(read(export_dir, "pa_translations"), "product_attribute_id")

    features = {}
    for pa in read(export_dir, "product_attributes"):
        value = attr_values.get(pa["id"], {}).get("value", "").strip()
        title = attr_titles.get(pa["attribute_id"], {}).get("title", "").strip()
        if pa["active"] == "1" and value and title:
            features.setdefault(pa["product_id"], []).append(f"{title}: {clean_text(value)}")

    rows, seen = [], set()
    for p in products:
        if p["active"] != "1" or p["id"] not in titles or float(p["price"]) <= 0:
            continue
        title = clean_text(titles[p["id"]]["title"])
        if (title, p["price"]) in seen:
            continue
        seen.add((title, p["price"]))
        old_price = p["crosed_out_price"]
        db_category = categories.get(p["category_id"], {}).get("title", "")
        rows.append({
            "id": p["id"],
            "category": guess_category(title, db_category),
            "title": title,
            "price_uah": p["price"],
            "old_price_uah": "" if old_price in ("", "NULL", "0.00") else old_price,
            "in_stock": p["quantity"],
            "is_hit": p["is_hit"],
            "features": "; ".join(features.get(p["id"], [])),
            "description": clean_html(titles[p["id"]]["content"])[:600],
            "url": "",
            "photo": "",
        })
    add_photos(rows)
    rows.sort(key=lambda r: (r["category"], r["title"]))
    write("catalog.csv", list(rows[0].keys()), rows)


def fetch(url):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def add_photos(rows):
    """Adds each product's page link and main photo from the live site.

    The export's slugs and images.csv are outdated, so all product pages are taken
    from the site's sitemap. Each page's photo (og:image) has the product's database
    id in its path (/storage/images/product/<id>/...), which links it to the catalog."""
    sitemap = fetch("https://berry-farm.com/sitemap.xml")
    pages = sorted(set(re.findall(r"<loc>(https://berry-farm\.com/uk/catalog/[^<]+)</loc>", sitemap)))
    if not pages:
        print("photos: could not read the site's sitemap, catalog saved without photos")
        return

    def photo(page_url):
        match = re.search(r'property="og:image"\s+content="([^"]+/product/(\d+)/[^"]+)"', fetch(page_url))
        return (match.group(2), page_url, match.group(1)) if match else None

    found = {}
    with ThreadPoolExecutor(8) as pool:
        for result in pool.map(photo, pages):
            if result:
                product_id, page_url, image = result
                found.setdefault(product_id, (page_url, image))  # first page wins for duplicates

    for row in rows:
        if row["id"] in found:
            row["url"], row["photo"] = found[row["id"]]
    print(f"photos found for {sum(bool(r['photo']) for r in rows)} of {len(rows)} products "
          f"({len(pages)} product pages on the site)")


def build_faq(faq_csv):
    with open(faq_csv, encoding="utf-8-sig") as f:
        rows = [{
            "category": r["Категорія"].strip(),
            "subcategory": r["Підкатегорія"].strip(),
            "question": r["Запитання"].strip(),
            "answer": r["Відповідь"].strip(),
            "video": r["Посилання на відео"].strip(),
        } for r in csv.DictReader(f) if r["Запитання"].strip()]
    write("faq.csv", list(rows[0].keys()), rows)


def format_discounts(data, unit):
    parts = []
    for d in json.loads(data):
        amount = abs(d["amount"])
        sign = "%" if d["type"] == "minus_procent" else " грн/шт"
        parts.append(f"від {d['from']} {unit}: -{amount}{sign}")
    return "; ".join(parts)


def build_info(export_dir):
    settings = {s["key"]: s["data"] for s in read(export_dir, "settings")}
    pages = {p["id"]: p["slug"] for p in read(export_dir, "pages")}
    about = next(
        clean_html(t["content"]) for t in read(export_dir, "page_translations")
        if t["locale"] == LOCALE and pages.get(t["page_id"]) == "o-nas"
    )
    rows = [
        {"key": "company_name", "value": "Беррі Фарм Юкрейн (Berry-Farm Ukraine)"},
        {"key": "website", "value": "https://berry-farm.com/"},
        {"key": "email", "value": settings.get("site_email", "")},
        {"key": "min_order_uah", "value": settings.get("min_sum_uah", "")},
        {"key": "discount_by_quantity", "value": format_discounts(settings["product_count"], "шт")},
        {"key": "discount_by_order_sum", "value": format_discounts(settings["order_amount"], "грн")},
        {"key": "delivery", "value": "Доставка по всій Україні (Нова Пошта)"},
        {"key": "about", "value": re.sub(r"\s*Тест$", "", about)},
    ]
    write("info.csv", ["key", "value"], rows)


if __name__ == "__main__":
    export_dir = sys.argv[1] if len(sys.argv) > 1 else EXPORT_DIR
    faq_csv = sys.argv[2] if len(sys.argv) > 2 else FAQ_CSV
    os.makedirs(OUT_DIR, exist_ok=True)
    build_catalog(export_dir)
    build_faq(faq_csv)
    build_info(export_dir)
