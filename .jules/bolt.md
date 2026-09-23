## 2023-10-27 - [String Methods vs Regex]
**Learning:** For simple integer matching patterns (e.g. `^-?\d+$`), native string methods (`isdigit()`, `startswith('-')`) are approximately 2x faster than using `re.match()` in Python 3.14. When these functions are called millions of times during batch parsing (like `parse_heppa_int`), the regex overhead becomes a measurable bottleneck.
**Action:** Use native string methods for simple type validation instead of regex when performance is critical.
