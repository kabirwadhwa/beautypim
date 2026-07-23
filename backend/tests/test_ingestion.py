import pytest
from app.services.ingestion import suggest_mapping, read_preview, detect_delimiter

def test_suggest_mapping():
    headers = ["sku", "Product Name", "Marque", "description_en", "INCI ingredients", "ean_code", "productPrice"]
    suggestions = suggest_mapping(headers)
    assert suggestions["product_name"] == "Product Name"
    assert suggestions["brand"] == "Marque"
    assert suggestions["ean"] == "ean_code"
    assert suggestions["ingredients"] == "INCI ingredients"
    assert suggestions["price"] == "productPrice"

def test_read_preview_csv():
    csv_content = b"product_name;brand;price\nCream;Cerave;12.99\nLotion;Clinique;24.00"
    headers, rows, count = read_preview(csv_content, "csv")
    assert headers == ["product_name", "brand", "price"]
    assert len(rows) == 2
    assert count == 2
    assert rows[0]["product_name"] == "Cream"
    assert rows[1]["brand"] == "Clinique"


def test_semicolon_csv_with_comma_heavy_json_is_not_misdetected():
    history = (
        '[{"field":"texture","value":"cream","source":"ai"},'
        '{"field":"benefits","value":["hydration","softness","comfort"],"source":"ai"},'
        '{"field":"ingredients","value":["aqua","glycerin","squalane"],"source":"source"}]'
    )
    csv_content = (
        'product_id;product_name;brand;provenance_history\n'
        f'ICN-001;Cloud Cream;Test Brand;"{history.replace(chr(34), chr(34) * 2)}"\n'
        f'ICN-002;Night Serum;Test Brand;"{history.replace(chr(34), chr(34) * 2)}"\n'
    ).encode("utf-8")

    assert detect_delimiter(csv_content) == ";"
    headers, rows, count = read_preview(csv_content, "csv")
    assert headers == ["product_id", "product_name", "brand", "provenance_history"]
    assert count == 2
    assert rows[0]["product_name"] == "Cloud Cream"
    assert '"hydration"' in rows[0]["provenance_history"]
