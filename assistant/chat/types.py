"""The one shape every provider returns, and the app ever sees.

Frozen and stdlib-only on purpose: the ask task stores these fields on the
AskQuery without knowing which vendor produced them, and nothing can change
them after the provider builds the result.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
