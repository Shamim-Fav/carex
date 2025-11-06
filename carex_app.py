import streamlit as st
import pandas as pd
import requests
import json
import re
import os
import time
import urllib.parse
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- Streamlit UI setup ---
st.set_page_config(page_title="Carex Scraper", layout="wide")
st.title("🛒 Carex Product Scraper App")
st.write("Scrape product variants and check stock status from carex.com")

headers = {"User-Agent": "Mozilla/5.0"}

# --- Chrome / Selenium setup ---
def init_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    # ✅ Detect environment
    if os.path.exists("/usr/bin/google-chrome"):
        chrome_options.binary_location = "/usr/bin/google-chrome"
        service = Service("/usr/bin/chromedriver")
    else:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver


# --- Helper functions ---
def extract_variants_from_script(html):
    pattern = r'var meta = ({.*?});\s*for \(var attr in meta\)'
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        return []
    meta_json = match.group(1)
    meta_data = json.loads(meta_json)
    return meta_data.get("products", [])


def extract_product_urls_from_collection_page(html):
    pattern = r'<link rel="prefetch" href="([^"]+/products/[^"/]+)"'
    return re.findall(pattern, html)


def flatten_product_variant(product, variant, product_url):
    flat_data = {}
    for k, v in product.items():
        if k != "variants":
            flat_data[f"product_{k}"] = v
    for k, v in variant.items():
        flat_data[f"variant_{k}"] = v
    flat_data["product_url"] = product_url
    flat_data["variant_url"] = (
        f"{product_url}?variant={variant.get('id')}" if product_url else None
    )
    return flat_data


# --- Step 1: Scrape product variants ---
def scrape_variants():
    all_rows = []
    page = 1
    progress = st.progress(0)
    st.write("🔎 Starting variant scrape from collection pages...")

    while True:
        url = f"https://carex.com/collections/all?page={page}"
        res = requests.get(url, headers=headers)

        if res.status_code != 200:
            st.error(f"❌ Failed to load page {page}")
            break

        products = extract_variants_from_script(res.text)
        if not products:
            st.success("✅ No more products found.")
            break

        product_urls = extract_product_urls_from_collection_page(res.text)

        for i, product in enumerate(products):
            product_url = product_urls[i] if i < len(product_urls) else None
            variants = product.get("variants", [])
            for variant in variants:
                row = flatten_product_variant(product, variant, product_url)
                all_rows.append(row)

        progress.progress(min(page * 10, 100))
        page += 1
        time.sleep(1)

    df = pd.DataFrame(all_rows)
    if "variant_price" in df.columns:
        df["variant_price_usd"] = df["variant_price"] / 100

    keep_columns = [
        "variant_name",
        "variant_sku",
        "variant_price_usd",
        "variant_public_title",
    ]
    df = df[keep_columns]

    output_file = "carex_variants_raw.xlsx"
    df.to_excel(output_file, index=False)
    st.success(f"✅ Done scraping variants. Saved to {output_file}")

    st.dataframe(df.head(10))
    st.download_button(
        label="⬇️ Download Variants Excel",
        data=df.to_excel(index=False, engine="openpyxl"),
        file_name="carex_variants_raw.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    return df


# --- Step 2: Check stock status ---
def extract_first_product_info(driver, search_url, retries=5, wait_time=10):
    for attempt in range(retries):
        try:
            driver.get(search_url)
            WebDriverWait(driver, wait_time).until(
                EC.presence_of_element_located((By.CLASS_NAME, "snize-product"))
            )
            product = driver.find_element(By.CLASS_NAME, "snize-product")
            classes = product.get_attribute("class")

            stock_status = (
                "In Stock"
                if "snize-product-in-stock" in classes
                else "Out of Stock"
                if "snize-product-out-of-stock" in classes
                else "Unknown"
            )

            try:
                link = product.find_element(By.CLASS_NAME, "snize-view-link")
                href = link.get_attribute("href")
                base_url = "https://carex.com"
                product_url = (
                    base_url + href if href and href.startswith("/products/") else href
                )
            except:
                product_url = None

            return product_url, stock_status
        except Exception:
            time.sleep(1)
    return None, "Retry Failed"


def scrape_search_results():
    input_file = "carex_variants_raw.xlsx"
    if not os.path.exists(input_file):
        st.error("⚠️ You must run 'Scrape Variants' first.")
        return

    df_input = pd.read_excel(input_file)
    base_search_url = "https://carex.com/pages/search-results-page?q="

    def make_search_url(row):
        query = row["variant_sku"] if pd.notna(row["variant_sku"]) else row["variant_name"]
        return base_search_url + urllib.parse.quote(str(query)) if pd.notna(query) else None

    df_input["search_url"] = df_input.apply(make_search_url, axis=1)

    driver = init_driver()
    st.write("🔍 Checking stock status...")
    results = []
    progress = st.progress(0)

    for idx, row in df_input.iterrows():
        search_url = row["search_url"]
        product_url, stock_status = extract_first_product_info(driver, search_url)
        row_data = dict(row)
        row_data["product_page_url"] = product_url
        row_data["stock_status"] = stock_status
        results.append(row_data)
        progress.progress(int(((idx + 1) / len(df_input)) * 100))

    driver.quit()

    df_out = pd.DataFrame(results)
    output_file = "carex_variants_checked.xlsx"
    df_out.to_excel(output_file, index=False)
    st.success(f"✅ Stock status checked. Saved to {output_file}")

    st.dataframe(df_out.head(10))
    st.download_button(
        label="⬇️ Download Checked Excel",
        data=df_out.to_excel(index=False, engine="openpyxl"),
        file_name="carex_variants_checked.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    return df_out


# --- Streamlit UI Controls ---
st.divider()
st.header("🔹 Actions")

if st.button("Step 1️⃣  Scrape Product Variants"):
    scrape_variants()

if st.button("Step 2️⃣  Check Stock Status"):
    scrape_search_results()

st.info("💡 Tip: Run Step 1 first, then Step 2.")
