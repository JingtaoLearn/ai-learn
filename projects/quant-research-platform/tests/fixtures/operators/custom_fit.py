OPERATOR_API_VERSION = 1
SLOT = "fit"


def apply(values, parameters):
    window = parameters["window"]
    selected = values[-window:]
    return sum(selected) / len(selected)
