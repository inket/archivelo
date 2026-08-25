MIN_NAME_LENGTH = 4

# The site's own video titles sometimes use a colloquial name that doesn't
# match the category naming in its nav (e.g. articles call it "La Vuelta"
# but the category slug/name is just "Vuelta"). Add more pairs here as
# other mismatches turn up.
TITLE_ALIASES = {
    "la vuelta": "vuelta",
}


def build_sorted_categories(categories) -> list[tuple[str, int]]:
    """Longest-name-first (name_lower, id) pairs, for prefix matching against
    video titles. Longest-first so a specific match (e.g. "tour de france
    femmes 2026") wins over a more generic one (e.g. "tour de france")."""
    pairs = [(c.name.lower(), c.id) for c in categories if len(c.name) >= MIN_NAME_LENGTH]
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def guess_category_id(title: str, sorted_categories: list[tuple[str, int]]) -> int | None:
    """Video titles on the site consistently start with "<Race> <Year>", the
    same shape as our category names, so the longest category name that's a
    literal prefix of the title is almost always the right one."""
    title_lower = title.lower()
    for alias, canonical in TITLE_ALIASES.items():
        if title_lower.startswith(alias):
            title_lower = canonical + title_lower[len(alias):]
            break
    for name_lower, category_id in sorted_categories:
        if title_lower.startswith(name_lower):
            return category_id
    return None
