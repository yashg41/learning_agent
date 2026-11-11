"""CSS rules whose absence broke rendering in a way no test could see.

The derivation pane's body is a column flexbox that scrolls. Its children
defaulted to flex-shrink:1, and a child that is itself a scroll container —
.dv-latex, which sets overflow-x — has an automatic min-height of 0. So in any
derivation taller than the pane, every standalone latex block was squeezed to
a few pixels with its own scrollbar: the SVM worked example's objective and
Lagrangian showed only the tops of "1" and "6". Text, tables and derivation
steps were unaffected, because none of them is a scroll container at the top
level — which is why it looked like a KaTeX problem and was not one.

This only pins the rule in place; the rendering itself is checked by eye.
"""

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "frontend" / "derivation.css"


def test_derivation_blocks_never_shrink_to_fit_the_pane():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    rule = re.search(r"\.derivation-body\s*>\s*\*\s*\{([^}]*)\}", css)

    assert rule, ".derivation-body > * rule is missing"
    assert re.search(r"flex-shrink\s*:\s*0", rule.group(1)), (
        "derivation blocks must not shrink — latex blocks collapse to a sliver"
    )
