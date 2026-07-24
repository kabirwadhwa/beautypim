from __future__ import annotations

import re


def split_inci(raw: str | None) -> list[str]:
    if not raw:
        return []
    values, current, depth = [], [], 0
    for character in raw:
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        if character in ",;" and depth == 0:
            value = re.sub(r"\s+", " ", "".join(current)).strip(" .")
            if value:
                values.append(value)
            current = []
        else:
            current.append(character)
    value = re.sub(r"\s+", " ", "".join(current)).strip(" .")
    if value:
        values.append(value)
    return values


def normalize_ingredient(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
