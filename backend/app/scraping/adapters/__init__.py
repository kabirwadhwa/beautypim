from app.scraping.adapters.generic import GenericJsonLdAdapter
from app.scraping.adapters.retail_site import RetailSiteAdapter


def adapter_for(domain: str):
    if domain == "retail-data.invalid" or domain.endswith(".retail-data.invalid"):
        return RetailSiteAdapter()
    return GenericJsonLdAdapter()
