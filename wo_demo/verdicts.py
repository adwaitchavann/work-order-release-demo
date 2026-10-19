"""Core verdict definitions.

The four verdicts below are used exactly as given in the exercise brief and are
not extended. Nothing else lives in this module: execution outcomes and refusal
causes are defined in ``executor.py`` so an execution result can never be
mistaken for a verdict.

If Coherence's canonical one-line definitions differ from the comments below,
replace the comments with that wording. The four names must not change.
"""

from enum import Enum


class Verdict(str, Enum):
    ADMIT = "ADMIT"  # Execution is permitted. It does not mean execution succeeded.
    HALT = "HALT"    # The action must not proceed.
    DEFER = "DEFER"  # The action cannot be decided yet; required evidence is missing.
    REFER = "REFER"  # The decision belongs to another authority.
