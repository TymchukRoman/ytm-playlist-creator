"""Resumable batched classification: the caller persists progress after every batch."""
from collections.abc import Callable

from .models import Decision, Song

BATCH_SIZE = 100


def classify_pending(
    description: str,
    songs: list[Song],
    decisions: dict[str, Decision],
    classifier,
    on_batch_done: Callable[[int, int], None],
) -> None:
    """Classify songs not yet in `decisions`, in input order, BATCH_SIZE at a time.

    After each batch the results are merged into `decisions` and
    `on_batch_done(batches_done, batches_total)` is called. Totals count only the
    songs still pending at the start of the call. Classifier errors propagate after
    earlier batches have been merged and reported.
    """
    pending = [s for s in songs if s.video_id not in decisions]
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    total = len(batches)
    for done, batch in enumerate(batches, start=1):
        for decision in classifier.classify_batch(description, batch):
            decisions[decision.video_id] = decision
        on_batch_done(done, total)
