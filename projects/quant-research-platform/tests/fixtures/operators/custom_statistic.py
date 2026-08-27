OPERATOR_API_VERSION = 1
SLOT = "statistic"


def apply(payload, parameters):
    values = payload["values"]
    return [None] + [values[index] - values[index - 1] for index in range(1, len(values))]
