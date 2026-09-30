class EmbeddingError(Exception):
    """Text could not be embedded, and trying again will not help.

    A bad key, a model name the vendor does not know, a request it rejects
    outright, a response of the wrong shape or size. Every provider
    translates its vendor's failures into this or into
    EmbeddingTransientError, so nothing upstream of the package boundary
    needs to know which vendor is behind it.
    """


class EmbeddingTransientError(Exception):
    """The vendor could not answer *right now*: a rate limit, a 5xx, a
    timeout, a dropped connection. The same call later will likely work.

    Deliberately not a subclass of EmbeddingError (D38): code that gives up
    on EmbeddingError must not swallow this one too. The indexing task
    lists it in ``autoretry_for`` so Celery retries with backoff. The
    reference let the vendor SDK's own exceptions through for this; with
    plain ``requests`` there is no vendor class to let through, so this one
    stands in for them.
    """
