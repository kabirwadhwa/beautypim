import json
import logging

from app.services.product_research_logging import PREFIX, product_research_log


def test_product_research_log_is_structured_info_and_redacts_secrets(caplog):
    caplog.set_level(logging.INFO, logger="app.product_research")

    product_research_log(
        "review_extraction", job_id="job-1", product_id="product-1",
        gtin="123", url="https://shop.example/item?token=secret#reviews",
        extracted=12, accepted=10, persisted=10,
        api_key="must-not-appear", authorization="Bearer must-not-appear",
    )

    message = next(record.message for record in caplog.records if record.message.startswith(PREFIX))
    payload = json.loads(message.removeprefix(PREFIX).strip())
    assert payload["event"] == "review_extraction"
    assert payload["extracted"] == 12
    assert payload["url"] == "https://shop.example/item"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["authorization"] == "[REDACTED]"
    assert "must-not-appear" not in message


def test_product_research_log_never_emits_review_text(caplog):
    caplog.set_level(logging.INFO, logger="app.product_research")
    product_research_log(
        "review_extraction", job_id="job-2", review_text="private reviewer content",
        extracted=1, persisted=1,
    )
    message = caplog.records[-1].message
    assert "private reviewer content" not in message
    assert "[REDACTED]" in message
