"""The default provider. No network, no keys, no cost.

Every environment that has not set EMBEDDING_PROVIDER runs on this, and the
test runner forces it, which is what keeps indexing and search working in
development and in CI with nothing configured.

Not random noise but a hashed bag of words (D39), so that it is a little
useful: each lowercased word is hashed to one of ``dimensions`` slots and a
sign (the "hashing trick"), the counts are summed and the vector is
normalised. Two texts that share words therefore point in similar
directions, and a smoke test of search -- or the eval command run without a
key -- ranks the note that mentions "passport" above the one that does not.
It knows nothing of meaning: "car" and "automobile" are unrelated to it.
"""

import hashlib
import re

from ._common import l2_normalise

_WORD = re.compile(r"\w+")


class FakeProvider:
    name = "fake"

    def embed(self, texts, model="", dimensions=1536, task="document"):
        # model and task are ignored: a query and a document vector must be
        # comparable, and there is only one "model".
        return [_vector(text, dimensions) for text in texts]


def _vector(text, dimensions):
    vector = [0.0] * dimensions
    words = _WORD.findall(text.lower()) or [text]
    for word in words:
        # sha256, not hash(): Python salts str hashes per process, and these
        # vectors must be identical across runs and machines.
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:8], "big") % dimensions
        # A sign per word keeps collisions from only ever adding up.
        vector[slot] += 1.0 if digest[8] & 1 else -1.0
    if not any(vector):
        # Every word cancelled out (two words, one slot, opposite signs).
        # A zero vector has no cosine distance to anything -- pgvector
        # would answer NaN -- so fall back to one slot for the whole text.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:8], "big") % dimensions] = 1.0
    return l2_normalise(vector)
