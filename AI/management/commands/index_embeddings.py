"""Build or catch up the index behind "search by meaning".

    python manage.py index_embeddings                 # everything not yet indexed
    python manage.py index_embeddings --user sokha    # one student
    python manage.py index_embeddings --limit 500     # at most 500 answers this run
    python manage.py index_embeddings --rebuild       # after changing EMBEDDING_MODEL
                                                      # or EMBEDDING_DIMENSIONS

The hourly ``send_reminders`` run and every search already index new answers a
few at a time; this is for the first run on an existing installation and for
a change of embedding model. Unlike those, it also re-checks every indexed
answer for edits, not just recently active chats.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from AI.models import User
from AI.services import embedding_service
from AI.services.embedding_service import EmbeddingError


class Command(BaseCommand):
    help = "Embed answers for search by meaning (new, edited, or made by another embedding model)."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Only this student's chats (username).")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many answers (0 = no limit).")
        parser.add_argument(
            "--rebuild", action="store_true",
            help="Delete vectors made by another model or size, then re-index.",
        )

    def handle(self, *args, **options):
        user = None
        if options.get("user"):
            user = User.objects.filter(username=options["user"]).first()
            if user is None:
                raise CommandError(f"No user called {options['user']!r}.")

        if not embedding_service.is_configured():
            raise CommandError(
                "Search by meaning is not configured: set EMBEDDING_API_KEY (or OPENAI_API_KEY) in .env."
            )
        if not embedding_service.is_switched_on():
            self.stdout.write(self.style.WARNING(
                "Search by meaning is switched off in the console settings — indexing anyway, "
                "so it is ready when it is switched on."
            ))

        scope = f" for {user.username}" if user else ""
        self.stdout.write(
            f"Indexing{scope} with {embedding_service.model_name()} "
            f"at {embedding_service.dimensions()} dimensions."
        )

        if options["rebuild"]:
            dropped = embedding_service.drop_foreign_vectors(user)
            self.stdout.write(f"  removed    {dropped} vector(s) from another model or size")

        stale = embedding_service.stale_message_ids(user, recent_only=False)
        missing = embedding_service.pending_ids(user)
        seen = set(stale)
        todo = stale + [pk for pk in missing if pk not in seen]
        limit = max(0, int(options["limit"] or 0))
        if limit:
            todo = todo[:limit]
        self.stdout.write(f"  to do      {len(todo)} answer(s) ({len(stale)} edited, {len(missing)} new)")

        done = 0
        step = embedding_service.BATCH_SIZE
        for start in range(0, len(todo), step):
            try:
                done += embedding_service.index_messages(todo[start:start + step])
            except EmbeddingError as exc:
                self.stdout.write(f"  indexed    {done} of {len(todo)}")
                raise CommandError(f"Stopped: {exc} ({exc.code}). Run the command again to carry on.")
            self.stdout.write(f"  indexed    {done} of {len(todo)}")

        pending = embedding_service.pending_count(user)
        self.stdout.write(self.style.SUCCESS(
            f"Done: {done} answer(s) indexed, {embedding_service.indexed_count(user)} searchable, "
            f"{pending} still pending."
        ))
