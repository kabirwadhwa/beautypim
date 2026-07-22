from app.models import IngredientDefinition
from scripts.sync_cosing import build_http_session, upsert_page


def _result(inci_name, record_id, common_name=None):
    return {
        "reference": record_id,
        "metadata": {
            "inciName": [inci_name],
            "nameOfCommonIngredientsGlossary": [common_name or inci_name],
            "substanceId": [record_id],
            "status": ["Active"],
            "functionName": ["SKIN CONDITIONING"],
        },
    }


def test_upsert_page_deduplicates_normalized_names_and_is_repeatable(db):
    payload = {
        "results": [
            _result("ACETYL CYSTEINYL D-OCTAPEPTIDE-1 AMINE", "100"),
            _result("Acetyl Cysteinyl D-Octapeptide-1 Amine", "101"),
        ]
    }

    assert upsert_page(db, payload) == 1
    assert upsert_page(db, payload) == 1

    rows = db.query(IngredientDefinition).all()
    assert len(rows) == 1
    assert rows[0].source_record_id == "101"


def test_upsert_page_preserves_long_cosing_names(db):
    long_name = "A" * 300
    assert upsert_page(db, {"results": [_result(long_name, "200")]}) == 1

    row = db.query(IngredientDefinition).filter_by(source_record_id="200").one()
    assert row.name == long_name
    assert row.common_name == long_name
    assert len(row.normalized_name) == 300


def test_cosing_http_session_retries_post_requests():
    adapter = build_http_session().get_adapter("https://")
    assert adapter.max_retries.total == 5
    assert "POST" in adapter.max_retries.allowed_methods
    assert 429 in adapter.max_retries.status_forcelist
