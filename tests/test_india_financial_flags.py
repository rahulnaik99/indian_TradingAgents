from tradingagents.india.financial_flags import evaluate_financial_flags


def view(periods, **metrics):
    sections = {"income": [], "balance": [], "cashflow": []}
    section_map = {
        "REVENUE": "income", "NET_INTEREST_INCOME": "income", "NET_INCOME": "income",
        "OPERATING_INCOME": "income", "EBIT": "income", "INTEREST_EXPENSE": "income",
        "TOTAL_ASSETS": "balance", "SHAREHOLDERS_EQUITY": "balance", "TOTAL_DEBT": "balance",
        "NET_DEBT": "balance", "CASH_AND_EQUIVALENTS": "balance",
        "OPERATING_CASH_FLOW": "cashflow", "FREE_CASH_FLOW": "cashflow",
    }
    for metric, values in metrics.items():
        sections[section_map[metric]].append({"metric": metric, "label": metric, "unit": "INR crore", "values": dict(zip(periods, values))})
    return {"periods": list(reversed(periods)), "sections": sections}


def codes(result, key="red_flags"):
    return {f["code"] for f in result[key]}


def test_three_quarter_revenue_decline_and_growth():
    q = view(["Q1", "Q2", "Q3"], REVENUE=[100, 90, 80], NET_INCOME=[10, 11, 12])
    result = evaluate_financial_flags({}, q)
    assert "revenue_declining_three_quarters" in codes(result)
    q2 = view(["Q1", "Q2", "Q3"], REVENUE=[80, 90, 100], NET_INCOME=[10, 11, 12])
    assert "revenue_growing_three_quarters" in codes(evaluate_financial_flags({}, q2), "green_flags")


def test_net_loss_profit_falling_and_profit_growing():
    q = view(["Q1", "Q2"], REVENUE=[100, 100], NET_INCOME=[20, -5])
    assert "latest_quarter_loss" in codes(evaluate_financial_flags({}, q))
    qfall = view(["Q1", "Q2"], REVENUE=[100, 100], NET_INCOME=[20, 10])
    assert "quarterly_profit_falling" in codes(evaluate_financial_flags({}, qfall))
    q = view(["Q1", "Q2"], REVENUE=[100, 100], NET_INCOME=[5, 10])
    assert "quarterly_profit_growing" in codes(evaluate_financial_flags({}, q), "green_flags")


def test_margin_shrink_and_expansion():
    q = view(["Q1", "Q2"], REVENUE=[100, 100], NET_INCOME=[20, 18])
    assert "net_margin_shrinking" in codes(evaluate_financial_flags({}, q))
    q = view(["Q1", "Q2"], REVENUE=[100, 100], NET_INCOME=[10, 15])
    assert "net_margin_expanding" in codes(evaluate_financial_flags({}, q), "green_flags")


def test_debt_leverage_interest_cover_and_cashflow_flags():
    a = view(["FY1", "FY2"], TOTAL_DEBT=[100, 150], SHAREHOLDERS_EQUITY=[100, 100], EBIT=[10, 10], INTEREST_EXPENSE=[2, 6], NET_DEBT=[20, 50])
    q = view(["Q1"], REVENUE=[100], NET_INCOME=[10], OPERATING_CASH_FLOW=[-2])
    r = evaluate_financial_flags(a, q)
    assert "debt_rising_high_leverage" in codes(r)
    assert "low_interest_coverage" in codes(r)
    assert "negative_operating_cash_flow" in codes(r)


def test_fcf_negative_multiple_years_and_debt_falling_green():
    a = view(["FY1", "FY2", "FY3"], TOTAL_DEBT=[30, 20, 10], SHAREHOLDERS_EQUITY=[100, 100, 100], FREE_CASH_FLOW=[-1, -2, -3])
    r = evaluate_financial_flags(a, {})
    assert "multi_year_negative_fcf" in codes(r)
    assert "debt_zero_or_falling" in codes(r, "green_flags")


def test_financial_rules_skip_debt_and_track_assets_equity():
    a = view(["FY1", "FY2"], TOTAL_DEBT=[30, 100], SHAREHOLDERS_EQUITY=[100, 120], TOTAL_ASSETS=[200, 250], NET_INTEREST_INCOME=[20, 25], NET_INCOME=[5, 8])
    r = evaluate_financial_flags(a, {}, is_financial=True)
    assert not any("debt" in f["code"] or "leverage" in f["code"] for f in r["red_flags"] + r["green_flags"])
    assert "financial_total_assets_growing" in codes(r, "green_flags")
    assert "financial_shareholders_equity_growing" in codes(r, "green_flags")


def test_possible_one_off_profit_spike():
    a = view(["FY1", "FY2", "FY3"], NET_INCOME=[10, 12, 40])
    assert "possible_one_off_profit_spike" in codes(evaluate_financial_flags(a, {}))


def test_missing_data_does_not_infer_flags_and_disclaimer_present():
    r = evaluate_financial_flags({}, {})
    assert not r["red_flags"] and not r["green_flags"]
    assert "not investment advice" in r["disclaimer"]
