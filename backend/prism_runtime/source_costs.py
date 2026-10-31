"""Accounting for declared acquisition receipts, including confirmed zero cost."""
import math


def source_cost(sources):
    costs, complete = {}, True
    for source in sources:
        run_id = source.get('acquisition_cost_id')
        amount = source.get('acquisition_cost_usd')
        try:
            valid_amount = (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                            and math.isfinite(amount) and amount >= 0)
        except OverflowError:
            valid_amount = False
        valid_id = isinstance(run_id, str) and bool(run_id.strip())
        # An explicit zero can describe a confirmed free source without a run;
        # a positive cost must identify the run so that shared costs deduplicate.
        valid = (source.get('acquisition_cost_complete') is True and valid_amount
                 and (valid_id or (run_id is None and amount == 0)))
        complete = complete and valid
        if valid and valid_id:
            costs[run_id] = max(float(amount), costs.get(run_id, 0.))
    total = sum(costs.values())
    if not math.isfinite(total):
        raise ValueError('Acquisition cost total exceeds finite accounting bounds')
    return total, complete
