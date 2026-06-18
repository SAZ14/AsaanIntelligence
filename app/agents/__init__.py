from app.agents.customer import TAGLINE, link_qr_scan, run_customer_agent


def run_merchant_customer_agent(*args, **kwargs):
    from app.agents.merchant_customer import run_merchant_customer_agent as _run

    return _run(*args, **kwargs)


__all__ = [
    "run_customer_agent",
    "run_merchant_customer_agent",
    "link_qr_scan",
    "TAGLINE",
]
