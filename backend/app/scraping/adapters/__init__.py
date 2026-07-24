from app.scraping.adapters.generic import GenericJsonLdAdapter
from app.scraping.adapters.retail_data import Retail DataAdapter


def adapter_for(domain: str):
    if domain == "retail-data.invalid" or domain.endswith(".retail-data.invalid"):
        return Retail DataAdapter()
    # These retailers intentionally use the generic contract until a tested
    # site-specific subclass is added; the crawler core is never coupled to
    # one retailer.
    generic_retailers = ("retail_data.", "retail_data.", "retail_data.", "retail_data.")
    if any(name in domain for name in generic_retailers):
        return GenericJsonLdAdapter()
    return GenericJsonLdAdapter()
