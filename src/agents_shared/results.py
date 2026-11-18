"""Keep worker results inside the message contract without corrupting scan reports."""

import json


MAX_RESULT_LENGTH = 20_000


def bounded_result_output(output: str) -> str:
    if len(output) <= MAX_RESULT_LENGTH:
        return output

    try:
        report = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        report = None

    if isinstance(report, dict) and isinstance(report.get("findings"), list):
        findings = report["findings"]
        summary = report | {"total_findings": len(findings), "truncated": True}
        low, high = 0, len(findings)
        best = None
        while low <= high:
            count = (low + high) // 2
            candidate = json.dumps(
                summary | {"findings": findings[:count]},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if len(candidate) <= MAX_RESULT_LENGTH:
                best = candidate
                low = count + 1
            else:
                high = count - 1
        if best is not None:
            return best

    suffix = "\n[output truncated]"
    return output[: MAX_RESULT_LENGTH - len(suffix)] + suffix
