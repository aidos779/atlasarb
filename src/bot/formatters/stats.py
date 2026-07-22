"""Admin engine-diagnostics rendering — the /stats screen.

Deliberately untranslated: this is an operator diagnostic reporting the same field names
that appear in the ``detector_stats`` log line, so the Telegram view and the log can be
read against each other without a mental mapping.
"""
from __future__ import annotations

# Report key -> screen heading, in the order the detectors run.
_DETECTOR_LABELS: tuple[tuple[str, str], ...] = (
    ("cex_cex", "CEX↔CEX"),
    ("cex_dex", "CEX↔DEX"),
    ("dex_dex", "DEX↔DEX"),
    ("funding", "Funding"),
    ("cross_chain", "Cross-chain"),
)


def format_detector_stats(report: dict[str, dict[str, int]]) -> str:
    """Render a DetectorStats report as the /stats message body."""
    lines = ["<b>Engine Statistics (last minute)</b>"]
    for key, label in _DETECTOR_LABELS:
        counters = report.get(key, {})
        dropped = (counters.get("rejected_fees", 0)
                   + counters.get("rejected_spread", 0)
                   + counters.get("rejected_liquidity", 0)
                   + counters.get("rejected_validation", 0))
        asm_rejected = (counters.get("asm_rejected_fees", 0)
                        + counters.get("asm_rejected_spread", 0)
                        + counters.get("asm_rejected_liquidity", 0)
                        + counters.get("asm_rejected_validation", 0))
        lines += [
            "",
            f"<b>{label}</b>",
            f"Checked: {counters.get('checked', 0)}",
            f"Candidates: {counters.get('candidates', 0)}",
            f"Published: {counters.get('published', 0)}",
            # Detector-side drops (before a candidate is created).
            f"Detector-dropped: {dropped}",
            f"- fees {counters.get('rejected_fees', 0)}",
            f"- spread {counters.get('rejected_spread', 0)}",
            f"- liquidity {counters.get('rejected_liquidity', 0)}",
            f"- validation {counters.get('rejected_validation', 0)}",
            # Assembler-side rejects (emitted candidate dropped downstream).
            f"Assembler-rejected: {asm_rejected}",
            f"- fees {counters.get('asm_rejected_fees', 0)}",
            f"- spread {counters.get('asm_rejected_spread', 0)}",
            f"- liquidity {counters.get('asm_rejected_liquidity', 0)}",
            f"- validation {counters.get('asm_rejected_validation', 0)}",
        ]
    return "\n".join(lines)
