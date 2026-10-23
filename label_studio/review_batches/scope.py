import re

from .service import fail


def requested_batch(request):
    values = request.query_params.getlist("review_batch")
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r"[1-9][0-9]{0,18}", values[0]) or int(values[0]) > 2**63 - 1:
        fail("REVIEW_BATCH_INVALID_INPUT", 400)
    return int(values[0])
