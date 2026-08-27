OPERATOR_API_VERSION = 1
SLOT = "fit"


def apply(payload, parameters):
    values = payload["values"]
    window = parameters["window"]
    selected = values[-window:]
    return sum(selected) / len(selected)
