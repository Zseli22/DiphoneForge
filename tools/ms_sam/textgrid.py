"""Minimal reader for the MFA TextGrids (interval tiers only)."""
import re


def read(path):
    """-> {tier name: [(xmin, xmax, text), ...]}"""
    txt = open(path, encoding="utf-8").read()
    tiers = {}
    for item in re.split(r"\n\s*item \[\d+\]:", txt)[1:]:
        name = re.search(r'name = "([^"]*)"', item).group(1)
        tiers[name] = [(float(a), float(b), t) for a, b, t in re.findall(
            r"intervals \[\d+\]:\s*\n\s*xmin = ([-\d.e]+)\s*\n\s*xmax = ([-\d.e]+)\s*\n\s*text = \"([^\"]*)\"",
            item)]
    return tiers
