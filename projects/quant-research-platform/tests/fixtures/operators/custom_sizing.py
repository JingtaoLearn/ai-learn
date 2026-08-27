OPERATOR_API_VERSION = 1
SLOT = "sizing"


def apply(payload, parameters):
    if payload["side"] == "SELL":
        return payload["holdings"]
    return 100
