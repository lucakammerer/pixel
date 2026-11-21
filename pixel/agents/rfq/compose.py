from pixel.contracts import RateOption, Rfq


def render(rfq: Rfq, option: RateOption) -> tuple[str, str]:
    """Subject and body of the quote: one option, one all-in total with its currency, transit time,
    in the customer's language and tone. Numbers are formatted by code from `option`, never by a model."""
    raise NotImplementedError
