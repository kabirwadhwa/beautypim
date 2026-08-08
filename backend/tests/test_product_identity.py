from app.services.product_identity import product_version_compatible, product_version_label


def test_product_version_label_prefers_specific_fragrance_concentrations():
    assert product_version_label("Sauvage Eau de Toilette") == "eau_de_toilette"
    assert product_version_label("Sauvage EDP") == "eau_de_parfum"
    assert product_version_label("Sauvage Parfum") == "parfum"


def test_product_version_guard_rejects_cross_edition_evidence():
    assert product_version_compatible("Eau de Toilette", "Sauvage Eau de Toilette 100ml")
    assert not product_version_compatible("Eau de Toilette", "Sauvage Parfum 100ml")
    assert not product_version_compatible("Eau de Parfum", "Sauvage Elixir")


def test_product_version_guard_allows_missing_edition_signal():
    assert product_version_compatible("Eau de Toilette", "Sauvage fragrance collection")
    assert product_version_compatible("Serum", "Niacinamide serum")
