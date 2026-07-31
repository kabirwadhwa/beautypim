import base64
import json

from scripts.anonymize_retail_sources import decode_mappings, replace_value


def encoded(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_retail_source_anonymization_is_recursive_and_case_insensitive():
    mappings = decode_mappings(encoded({"private-shop.example": "retail-data.invalid", "Private Shop": "Retail Data"}))
    value = {
        "source": "PRIVATE SHOP",
        "urls": ["https://private-shop.example/product/1"],
    }
    assert replace_value(value, mappings) == {
        "source": "Retail Data",
        "urls": ["https://retail-data.invalid/product/1"],
    }
