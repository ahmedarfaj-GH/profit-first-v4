"""
Liquidity Engine
----------------
Computes the Estimated Available Liquidity for a single entity/period from
normalized financial inputs.

Formula:
    Net Operating Cash = Opening Balance + Collections - Operating Expenses Paid
    Total Due Obligations = Payroll + VAT + Royalty + Suppliers + Other Short-Term
    Estimated Available Liquidity = Net Operating Cash - Total Due Obligations - Operational Reserve
"""


def compute_liquidity(inputs: dict) -> dict:
    opening = float(inputs.get("opening_cash_balance", 0) or 0)
    collections = float(inputs.get("total_collections", 0) or 0)
    expenses = float(inputs.get("total_operating_expenses_paid", 0) or 0)

    payroll = float(inputs.get("payroll_due", 0) or 0)
    vat = float(inputs.get("vat_due", 0) or 0)
    royalty = float(inputs.get("royalty_due", 0) or 0)
    suppliers = float(inputs.get("suppliers_due", 0) or 0)
    other_short_term = float(inputs.get("other_short_term_due", 0) or 0)

    reserve = float(inputs.get("operational_reserve_target", 0) or 0)

    net_operating_cash = opening + collections - expenses
    total_due_obligations = payroll + vat + royalty + suppliers + other_short_term
    estimated_available_liquidity = net_operating_cash - total_due_obligations - reserve

    return {
        "opening_cash_balance": round(opening, 2),
        "total_collections": round(collections, 2),
        "total_operating_expenses_paid": round(expenses, 2),
        "net_operating_cash": round(net_operating_cash, 2),
        "payroll_due": round(payroll, 2),
        "vat_due": round(vat, 2),
        "royalty_due": round(royalty, 2),
        "suppliers_due": round(suppliers, 2),
        "other_short_term_due": round(other_short_term, 2),
        "total_due_obligations": round(total_due_obligations, 2),
        "operational_reserve_target": round(reserve, 2),
        "estimated_available_liquidity": round(estimated_available_liquidity, 2),
    }
