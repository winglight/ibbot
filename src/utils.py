import re


def slugify_strategy_name(name: str) -> str:
    """Convert arbitrary text to a safe snake_case strategy name."""
    if not isinstance(name, str):
        return ""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", name.lower())
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("_")
