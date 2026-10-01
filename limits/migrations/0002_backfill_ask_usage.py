"""One chat_turns event per existing non-failed ask (DECISIONS D103).

Before this, the quota was a count of AskQuery rows; from here it is the
ledger. Each event takes its ask's user and created_at, so this month's
usage is the same number before and after. Failed asks never counted and
get no event. An ask that already has an event is skipped, so running it
twice adds nothing.
"""

from django.db import migrations

BATCH = 2000


def backfill(apps, schema_editor):
    AskQuery = apps.get_model("assistant", "AskQuery")
    UsageEvent = apps.get_model("limits", "UsageEvent")

    asks = (
        AskQuery.objects.exclude(status="failed")
        .filter(usage_events__isnull=True)
        .values_list("pk", "user_id", "created_at")
        .order_by("pk")
    )
    batch = []
    for pk, user_id, created_at in asks.iterator(chunk_size=BATCH):
        batch.append(
            UsageEvent(user_id=user_id, key="chat_turns", amount=1, ask_id=pk, created_at=created_at)
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
