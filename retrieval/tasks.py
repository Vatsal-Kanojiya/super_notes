import logging

from celery import shared_task

from .embeddings import EmbeddingError, EmbeddingTransientError
from .indexing import index_attachment, index_note

logger = logging.getLogger(__name__)


@shared_task(
    # A rate limit or an outage is worth waiting out; backoff with jitter
    # keeps a provider recovering from being hit by every retry at once.
    autoretry_for=(EmbeddingTransientError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def index_note_task(note_id: int, version: int) -> None:
    """Index one note at one version (see retrieval/indexing.py).

    A permanent EmbeddingError is logged and dropped: retrying cannot fix a
    bad key or a wrong-sized vector, and ``index_status`` shows the note as
    lagging until a re-index. shared_task so this module never imports the
    Celery app.
    """
    try:
        index_note(note_id, version)
    except EmbeddingError:
        logger.exception("Giving up indexing note %s v%s", note_id, version)


@shared_task(
    autoretry_for=(EmbeddingTransientError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def index_attachment_task(attachment_id: int) -> None:
    """Re-embed one ready attachment's stored text (reindex_notes, after a model change)."""
    try:
        index_attachment(attachment_id)
    except EmbeddingError:
        logger.exception("Giving up indexing attachment %s", attachment_id)
