OPERATOR_API_VERSION = 1
SLOT = "decision"


def apply(payload, parameters):
    return [
        {"action": "HOLD", "reason": "CUSTOM_FIXTURE"}
        for statistic in payload["statistics"]
    ]
