OPERATOR_API_VERSION = 1
SLOT = "cost"


def apply(payload, parameters):
    return {
        "commission_cny": 0.0,
        "transfer_fee_cny": 0.0,
        "stamp_tax_cny": 0.0,
        "slippage_cny": 0.0,
        "total_cost_cny": 0.0,
    }
