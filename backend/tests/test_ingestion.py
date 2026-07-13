import pytest
from app.services.ingestion import suggest_mapping, read_preview

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
