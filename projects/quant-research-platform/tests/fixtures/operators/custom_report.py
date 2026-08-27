OPERATOR_API_VERSION = 1
SLOT = "report"


def apply(payload, parameters):
    return "<!doctype html><html><body><h1>" + payload["title"] + "</h1></body></html>"
