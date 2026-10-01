"""One chat_turns event per existing non-failed ask (DECISIONS D103).

Before this, the quota was a count of AskQuery rows; from here it is the
ledger. Each event takes its ask's user and created_at, so this month's
usage is the same number before and after. Failed asks never counted and
get no event. An answered ask's provider, model and token counts are
copied, as the task now records them on new events (D133); a pending or
floor-answered one has none. An ask that already has an event is skipped,
so running it twice adds nothing.
"""

from django.db import migrations

BATCH = 2000


def backfill(apps, schema_editor):
    AskQuery = apps.get_model("assistant", "AskQuery")
    UsageEvent = apps.get_model("limits", "UsageEvent")

    asks = (
        AskQuery.objects.exclude(status="failed")
        .filter(usage_events__isnull=True)
        .values_list(
            "pk", "user_id", "created_at", "provider", "model", "input_tokens", "output_tokens"
        )
        .order_by("pk")
    )
    batch = []
    for pk, user_id, created_at, provider, model, tokens_in, tokens_out in asks.iterator(
        chunk_size=BATCH
    ):
        called = bool(provider)  # AskQuery stores 0 tokens when no provider was called
        batch.append(
            UsageEvent(
                user_id=user_id,
                key="chat_turns",
                amount=1,
                ask_id=pk,
                created_at=created_at,
                provider=provider,
                model=model,
                input_tokens=tokens_in if called else None,
                output_tokens=tokens_out if called else None,
            )
        )
        if len(batch) >= BATCH:
            UsageEvent.objects.bulk_create(batch)
            batch = []
    UsageEvent.objects.bulk_create(batch)


def unfill(apps, schema_editor):
    # Back to counting rows: the ledger's ask events have no other reader.
    apps.get_model("limits", "UsageEvent").objects.filter(
        key="chat_turns", ask__isnull=False
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0001_initial"),
        ("limits", "0001_initial"),
    ]

    operations = [migrations.RunPython(backfill, unfill)]
