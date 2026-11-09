"""Keep internal delivery annotations out of conversation prose, not code examples."""

import re

TOKENS = re.compile(r"(`+).*?\1|\[已发送表情[：:][^\]\r\n]{0,600}\]")


def strip_delivery_annotations(value):
    if not isinstance(value, str):
        return value
    lines = []
    fence = None
    for line in value.splitlines(keepends=True):
        boundary = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            lines.append(line)
            if boundary and boundary[1][0] == fence[0] and len(boundary[1]) >= fence[1]:
                fence = None
        elif boundary:
            fence = (boundary[1][0], len(boundary[1]))
            lines.append(line)
        else:
            lines.append(TOKENS.sub(lambda m: m[0] if m[1] else "", line))
    return "".join(lines).rstrip()
