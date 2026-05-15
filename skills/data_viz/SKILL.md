---
name: data_viz
description: Build a data visualization script with matplotlib or Plotly producing a saved figure
when_to_use:
  - User asks for a "chart" or "graph" or "plot"
  - User wants data analysis with visual output
  - User mentions matplotlib, seaborn, plotly, or pandas plotting
languages:
  - python
must_have_features:
  - Read input data (CSV / JSON / inline list of dicts)
  - At least one chart type (line / bar / scatter / hist)
  - Title + axis labels + legend
  - Save to disk as PNG (matplotlib) or HTML (plotly)
  - Print summary statistics to stdout
  - argparse for --input / --output flags
---

# data_viz — ground rules

Single Python file.  Default backend is matplotlib (smaller dep,
better for static PNG output).  Switch to Plotly only if the user
explicitly asks for interactive or if the chart is genuinely 3D.

## File layout

```python
#!/usr/bin/env python3
"""<docstring>"""
import argparse, json, csv, statistics, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")  # MUST come before pyplot for headless
import matplotlib.pyplot as plt

def load_data(path: Path) -> list[dict]: ...
def render(data: list[dict], out: Path) -> None: ...
def summarize(data: list[dict]) -> None: ...
def main() -> int: ...
```

## Chart standards

* Title at top (large, bold).
* X / Y axis labels (always present, even if obvious).
* Legend ONLY when ≥ 2 series.
* `tight_layout()` before saving so labels aren't clipped.
* DPI ≥ 100 for PNG output.
* Color palette: avoid pure red/green pairs (color-blind unfriendly);
  default to matplotlib's "tab10" or "viridis" for sequential data.

## Summary statistics

Print to stdout BEFORE saving the chart:

```
Data summary:
  rows         : N
  numeric cols : [...]
  mean(<col>)  : X.XX
  median(<col>): X.XX
  stdev(<col>) : X.XX
  min/max      : X.XX / Y.YY
```

## Anti-patterns

* DON'T use `plt.show()` — this is a headless script.
* DON'T forget `matplotlib.use("Agg")` BEFORE importing pyplot.
* DON'T silently drop NaN rows; warn on stderr.
* DON'T hardcode a file path — argparse `--input` is required.
