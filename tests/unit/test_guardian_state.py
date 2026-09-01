"""Tests for durable, revision-aware PR guardian state."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import sqlite3

import pytest

from localize.guardian import FeedbackEvent, GuardianMode
from localize.guardian import state as guardian_state
from localize.guardian.state import GuardianState


UTC = timezone.utc


def _event(
    *,
    body: str = "Please use the glossary term.",
    head_sha: str = "a" * 40,
    base_sha: str = "b" * 40,
    repository: str = "acme/widgets",
    pr_number: int = 123,
) -> FeedbackEvent:
    return FeedbackEvent(
        repository=repository,
        pr_number=pr_number,
        kind="review-comment",
        event_id="98765",
        author="coderabbitai[bot]",
        author_id=202,
        author_type="Bot",
        body=body,
        head_sha=head_sha,
        base_sha=base_sha,
        locale="ru",
        updated_at="2026-08-30T08:00:00Z",
        path="l10n/messages_ru.properties",
        line=17,
        html_url="https://github.test/acme/widgets/pull/123#discussion_r98765",
    )


def test_exact_duplicate_is_not_pending_but_edits_and_sha_changes_are(
    tmp_path: Path,
) -> None:
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        first = state.record_feedback_event(_event())
        assert first.is_new is True
        assert [item.revision_id for item in state.pending_event_revisions()] == [
            first.revision_id
        ]
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
        )
        state.record_action(
            run_id=run_id,
            event_revision_id=first.revision_id,
            action="report",
            status="completed",
            details={"result": "reported"},
        )

        duplicate = state.record_feedback_event(_event())
        assert duplicate.revision_id == first.revision_id
        assert duplicate.is_new is False
        assert state.pending_event_revisions() == ()

        edited = state.record_feedback_event(_event(body="Use a different term."))
        moved_head = state.record_feedback_event(_event(head_sha="c" * 40))
        moved_base = state.record_feedback_event(_event(base_sha="d" * 40))

        assert edited.is_new is True
        assert moved_head.is_new is True
        assert moved_base.is_new is True
        assert [item.revision_id for item in state.pending_event_revisions()] == [
            edited.revision_id,
            moved_head.revision_id,
            moved_base.revision_id,
        ]


def test_pending_query_derives_terminal_status_bindings_from_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        revision = state.record_feedback_event(_event())
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
        )
        state.record_action(
            run_id=run_id,
            event_revision_id=revision.revision_id,
            action="observe",
            status="failed",
        )
        assert tuple(
            item.revision_id for item in state.pending_event_revisions()
        ) == (revision.revision_id,)

        monkeypatch.setattr(
            guardian_state,
            "_TERMINAL_ACTION_STATUSES",
            frozenset({"completed", "failed", "skipped"}),
        )

        assert state.pending_event_revisions() == ()


def test_state_database_is_private_even_when_created_under_a_permissive_umask(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guardian.sqlite3"
    previous_umask = os.umask(0)
    try:
        with GuardianState(path):
            pass
    finally:
        os.umask(previous_umask)

    assert path.stat().st_mode & 0o777 == 0o600


def test_state_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    target = tmp_path / "foreign.sqlite3"
    target.write_text("do not modify", encoding="utf-8")
    target.chmod(0o644)
    state_path = tmp_path / "guardian.sqlite3"
    state_path.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        GuardianState(state_path)

    assert target.read_text(encoding="utf-8") == "do not modify"
    assert target.stat().st_mode & 0o777 == 0o644


def test_state_refuses_insecure_existing_files_and_directories(tmp_path: Path) -> None:
    insecure_file = tmp_path / "guardian.sqlite3"
    insecure_file.write_bytes(b"")
    insecure_file.chmod(0o644)

    with pytest.raises(ValueError, match="mode 0600"):
        GuardianState(insecure_file)
    assert insecure_file.stat().st_mode & 0o777 == 0o644

    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        GuardianState(private_root / "guardian.sqlite3")
    assert not (private_root / "guardian.sqlite3").exists()


def test_mode_escalation_reconsiders_feedback_but_never_repeats_same_authority(
    tmp_path: Path,
) -> None:
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        revision = state.record_feedback_event(_event())
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
        )
        state.record_action(
            run_id=run_id,
            event_revision_id=revision.revision_id,
            action="observe",
            status="completed",
        )

        assert state.pending_event_revisions(mode=GuardianMode.OBSERVE) == ()
        assert [
            item.revision_id
            for item in state.pending_event_revisions(mode=GuardianMode.PREPARE)
        ] == [revision.revision_id]
        assert [
            item.revision_id
            for item in state.pending_event_revisions(
                mode=GuardianMode.APPLY_OWNED_TRANSLATIONS
            )
        ] == [revision.revision_id]


def test_revision_identity_is_scoped_to_repository_and_pr(tmp_path: Path) -> None:
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        first = state.record_feedback_event(_event())
        other_pr = state.record_feedback_event(_event(pr_number=124))
        other_repo = state.record_feedback_event(
            _event(repository="example/translations")
        )

        assert len({first.revision_id, other_pr.revision_id, other_repo.revision_id}) == 3
        assert len(state.pending_event_revisions()) == 3


def test_runs_actions_costs_and_health_are_persisted(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
    database_path = tmp_path / "guardian.sqlite3"

    with GuardianState(database_path) as state:
        revision = state.record_feedback_event(_event(), observed_at=now)
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.PREPARE,
            started_at=now,
        )
        state.record_action(
            run_id=run_id,
            event_revision_id=revision.revision_id,
            action="prepare-replacement",
            status="completed",
            details={"edits": 1},
            occurred_at=now,
        )
        state.record_cost(
            run_id=run_id,
            amount_usd=Decimal("1.234567"),
            model="gpt-test",
            input_tokens=100,
            output_tokens=25,
            incurred_at=now,
        )
        state.finish_run(
            run_id,
            status="completed",
            summary="Prepared one replacement.",
            finished_at=now,
        )
        state.record_health(
            component="github-intake",
            status="healthy",
            message="Intake completed.",
            details={"events": 1},
            checked_at=now,
        )

    with GuardianState(database_path) as state:
        run = state.get_run(run_id)
        assert run.status == "completed"
        assert run.summary == "Prepared one replacement."
        assert state.cost_for_day(date(2026, 8, 30)) == Decimal("1.234567")
        health = state.latest_health("github-intake")
        assert health is not None
        assert health.status == "healthy"
        assert health.details == {"events": 1}
        assert state.pending_event_revisions() == ()


def test_raw_event_body_can_expire_without_deleting_immutable_revision(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)

    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        revision = state.record_feedback_event(_event(), observed_at=observed_at)

        deleted = state.purge_raw_event_bodies(
            before=datetime(2026, 4, 1, tzinfo=UTC)
        )

        assert deleted == 1
        retained = state.get_event_revision(revision.revision_id)
        assert retained is not None
        assert retained.body is None
        assert retained.body_hash == revision.body_hash
        assert retained.author_id == 202
        assert retained.author_type == "Bot"
        assert retained.path == "l10n/messages_ru.properties"
        assert retained.line == 17
        assert retained.html_url.endswith("#discussion_r98765")
        assert retained.updated_at == "2026-08-30T08:00:00Z"


def test_assessment_cache_and_cost_settlement_are_one_transaction(tmp_path: Path) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    cache_key = "c" * 64
    result_json = '{"schema_version":1}'

    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=created_at,
        )
        reservation = state.try_reserve_budget(
            run_id=run_id,
            amount_usd=1,
            daily_limit_usd=2,
            model="gpt-test",
            reserved_at=created_at,
        )
        assert reservation is not None

        state.cache_assessment_and_settle_budget(
            cache_key=cache_key,
            repository="acme/widgets",
            pr_number=12,
            head_sha="a" * 40,
            base_sha="b" * 40,
            model="gpt-test",
            reasoning_effort="max",
            result_json=result_json,
            reservation_id=reservation,
            actual_cost_usd=0.25,
            input_tokens=100,
            output_tokens=20,
            created_at=created_at,
        )

        assert state.cached_assessment_result(
            cache_key=cache_key,
            repository="acme/widgets",
            pr_number=12,
            head_sha="a" * 40,
            base_sha="b" * 40,
            model="gpt-test",
            reasoning_effort="max",
        ) == result_json
        assert state.cost_for_day(created_at.date()) == Decimal("0.25")

        second_reservation = state.try_reserve_budget(
            run_id=run_id,
            amount_usd=1,
            daily_limit_usd=2,
            model="gpt-test",
            reserved_at=created_at,
        )
        assert second_reservation is not None
        with pytest.raises(RuntimeError, match="collision"):
            state.cache_assessment_and_settle_budget(
                cache_key=cache_key,
                repository="acme/widgets",
                pr_number=12,
                head_sha="a" * 40,
                base_sha="b" * 40,
                model="gpt-test",
                reasoning_effort="max",
                result_json='{"different":true}',
                reservation_id=second_reservation,
                actual_cost_usd=0.75,
                input_tokens=1,
                output_tokens=1,
                created_at=created_at,
            )
        assert state.cost_for_day(created_at.date()) == Decimal("0.25")

        assert state.purge_assessment_results(
            before=datetime(2026, 2, 1, tzinfo=UTC)
        ) == 1


def test_latest_revisions_support_deleted_comment_reconciliation(tmp_path: Path) -> None:
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        original = state.record_feedback_event(_event())
        metadata_edit = state.record_feedback_event(
            FeedbackEvent(
                **{
                    **_event().__dict__,
                    "line": 19,
                    "updated_at": "2026-08-30T08:30:00Z",
                }
            )
        )
        deleted = state.record_feedback_event(
            FeedbackEvent(
                **{
                    **_event().__dict__,
                    "body": "",
                    "deleted": True,
                    "updated_at": "2026-08-30T09:00:00Z",
                }
            )
        )

        latest = state.latest_event_revisions(repository="acme/widgets")

        assert len(latest) == 1
        assert latest[0].revision_id == deleted.revision_id
        assert latest[0].deleted is True
        assert latest[0].revision_id != original.revision_id
        assert metadata_edit.is_new is True
        assert metadata_edit.revision_id not in {
            original.revision_id,
            deleted.revision_id,
        }


def test_process_lease_is_exclusive_refreshable_and_recovers_after_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guardian.sqlite3"
    now = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
    with GuardianState(path) as first, GuardianState(path) as second:
        assert first.acquire_lease(
            name="guardian-run",
            owner="one",
            ttl_seconds=60,
            now=now,
        )
        assert not second.acquire_lease(
            name="guardian-run",
            owner="two",
            ttl_seconds=60,
            now=now + timedelta(seconds=30),
        )
        assert first.refresh_lease(
            name="guardian-run",
            owner="one",
            ttl_seconds=60,
            now=now + timedelta(seconds=30),
        )
        assert not second.release_lease(name="guardian-run", owner="two")
        assert first.release_lease(name="guardian-run", owner="one")
        assert second.acquire_lease(
            name="guardian-run",
            owner="two",
            ttl_seconds=60,
            now=now + timedelta(seconds=31),
        )
        assert first.acquire_lease(
            name="guardian-run",
            owner="three",
            ttl_seconds=60,
            now=now + timedelta(seconds=92),
        )


def test_reconciles_crashed_runs_without_resolving_their_events(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        revision = state.record_feedback_event(_event(), observed_at=now)
        stale_run = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.PREPARE,
            started_at=now,
        )
        state.record_action(
            run_id=stale_run,
            event_revision_id=revision.revision_id,
            action="prepare",
            status="pending",
            occurred_at=now,
        )
        fresh_run = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=now + timedelta(hours=2),
        )

        recovered = state.reconcile_incomplete_runs(
            before=now + timedelta(hours=1),
            reconciled_at=now + timedelta(hours=3),
        )

        assert recovered == (stale_run,)
        assert state.get_run(stale_run).status == "failed"
        assert state.get_run(fresh_run).status == "running"
        assert tuple(
            item.revision_id for item in state.pending_event_revisions()
        ) == (revision.revision_id,)


def test_budget_reservation_is_atomic_and_settles_to_actual_cost(tmp_path: Path) -> None:
    path = tmp_path / "guardian.sqlite3"
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    with GuardianState(path) as first, GuardianState(path) as second:
        run_one = first.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=now,
        )
        run_two = second.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=now,
        )
        reservation = first.try_reserve_budget(
            run_id=run_one,
            amount_usd="4.00",
            daily_limit_usd="5.00",
            model="gpt-test",
            reserved_at=now,
        )
        assert reservation is not None
        assert second.try_reserve_budget(
            run_id=run_two,
            amount_usd="2.00",
            daily_limit_usd="5.00",
            model="gpt-test",
            reserved_at=now,
        ) is None
        assert first.budget_committed_for_day(now.date()) == Decimal("4")

        statements: list[str] = []
        first._connection.set_trace_callback(statements.append)
        first.settle_budget_reservation(
            reservation,
            actual_cost_usd="0.75",
            input_tokens=100,
            output_tokens=20,
            settled_at=now,
        )
        first._connection.set_trace_callback(None)

        assert "BEGIN IMMEDIATE" in statements
        assert first.cost_for_day(now.date()) == Decimal("0.75")
        assert first.budget_committed_for_day(now.date()) == Decimal("0.75")
        assert second.try_reserve_budget(
            run_id=run_two,
            amount_usd="4.00",
            daily_limit_usd="5.00",
            model="gpt-test",
            reserved_at=now,
        ) is not None


def test_budget_reservation_rejects_naive_timestamp(tmp_path: Path) -> None:
    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            state.try_reserve_budget(
                run_id=run_id,
                amount_usd=1,
                daily_limit_usd=2,
                model="test-model",
                reserved_at=datetime(2026, 8, 30, 12, 0),
            )


def test_unknown_model_cost_keeps_conservative_reservation(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=now,
        )
        reservation = state.try_reserve_budget(
            run_id=run_id,
            amount_usd="5.00",
            daily_limit_usd="5.00",
            model="gpt-test",
            reserved_at=now,
        )
        assert reservation is not None

        state.mark_budget_reservation_unknown(reservation, marked_at=now)

        assert state.cost_for_day(now.date()) == Decimal("0")
        assert state.budget_committed_for_day(now.date()) == Decimal("5")


def test_daily_model_call_reservations_are_atomic_and_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guardian.sqlite3"
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    with GuardianState(path) as first, GuardianState(path) as second:
        run_one = first.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=now,
        )
        run_two = second.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=now,
        )

        first_call = first.try_reserve_model_call(
            run_id=run_one,
            daily_limit=1,
            model="gpt-test",
            purpose="assessment",
            reserved_at=now,
        )
        assert first_call is not None
        assert second.try_reserve_model_call(
            run_id=run_two,
            daily_limit=1,
            model="gpt-test",
            purpose="assessment",
            reserved_at=now,
        ) is None
        assert first.model_calls_committed_for_day(now.date()) == 1

        first.finalize_model_call(first_call, status="cancelled", finalized_at=now)
        second_call = second.try_reserve_model_call(
            run_id=run_two,
            daily_limit=1,
            model="gpt-test",
            purpose="prevention",
            reserved_at=now,
        )
        assert second_call is not None
        second.finalize_model_call(second_call, status="unknown", finalized_at=now)
        assert first.model_calls_committed_for_day(now.date()) == 1


def test_subscription_assessment_cache_does_not_require_a_dollar_reservation(
    tmp_path: Path,
) -> None:
    created_at = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        state.cache_assessment_result(
            cache_key="d" * 64,
            repository="acme/widgets",
            pr_number=12,
            head_sha="a" * 40,
            base_sha="b" * 40,
            model="gpt-test",
            reasoning_effort="max",
            result_json='{"schema_version":1}',
            created_at=created_at,
        )

        assert state.cached_assessment_result(
            cache_key="d" * 64,
            repository="acme/widgets",
            pr_number=12,
            head_sha="a" * 40,
            base_sha="b" * 40,
            model="gpt-test",
            reasoning_effort="max",
        ) == '{"schema_version":1}'


def test_publication_ledger_is_append_only_recoverable_and_idempotent(
    tmp_path: Path,
) -> None:
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        revision = state.record_feedback_event(_event())
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.APPLY_OWNED_TRANSLATIONS,
        )
        metadata = {
            "run_id": run_id,
            "repository": "acme/widgets",
            "pr_number": 123,
            "original_head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "commit_sha": "c" * 40,
            "event_revision_ids": (revision.revision_id,),
        }
        publication_key = state.record_publication_event(
            **metadata,
            phase="prepared",
        )

        pending = state.pending_publications(repository="acme/widgets")
        assert len(pending) == 1
        assert pending[0].publication_key == publication_key
        assert pending[0].phase == "prepared"
        assert pending[0].event_revision_ids == (revision.revision_id,)
        assert state.record_publication_event(
            **metadata,
            phase="prepared",
        ) == publication_key

        state.record_publication_event(**metadata, phase="published")
        assert state.pending_publications()[0].phase == "published"
        state.record_publication_event(**metadata, phase="replied")
        assert state.pending_publications() == ()

        replied = state.replied_publication_for_head(
            repository="acme/widgets",
            pr_number=123,
            head_sha="c" * 40,
        )
        assert replied is not None
        assert replied.phase == "replied"

        with pytest.raises(sqlite3.IntegrityError, match="publication events"):
            state._connection.execute(  # noqa: SLF001 - DB immutability assertion
                "UPDATE publication_events SET phase = 'abandoned'"
            )


def test_prevention_draft_ledger_recovers_pushes_and_deduplicates_opened_evidence(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        metadata = {
            "run_id": run_id,
            "source_repository": "acme/widgets",
            "target_repository": "guardian/pipeline",
            "target_base_branch": "main",
            "target_base_sha": "a" * 40,
            "push_repository": "guardian/pipeline",
            "branch": "guardian/prevention-" + "b" * 64,
            "candidate_sha": "c" * 40,
            "evidence_hash": "b" * 64,
            "title": "Prevent recurrence: placeholder parity",
            "body": "Validated prevention body\n",
            "occurred_at": now,
        }
        draft_key = state.record_prevention_draft_event(
            **metadata,
            phase="validated",
        )
        assert state.record_prevention_draft_event(
            **metadata,
            phase="validated",
        ) == draft_key
        assert state.pending_prevention_drafts()[0].phase == "validated"

        state.record_prevention_draft_event(**metadata, phase="pushed")
        pending = state.pending_prevention_drafts(source_repository="acme/widgets")
        assert len(pending) == 1
        assert pending[0].phase == "pushed"
        assert pending[0].candidate_sha == "c" * 40

        state.record_prevention_draft_event(
            **metadata,
            phase="draft_opened",
            draft_number=17,
            draft_url="https://github.test/guardian/pipeline/pull/17",
        )
        assert state.pending_prevention_drafts() == ()
        assert state.opened_prevention_evidence_hashes(
            source_repository="acme/widgets",
            target_repository="guardian/pipeline",
        ) == frozenset({"b" * 64})

        with pytest.raises(sqlite3.IntegrityError, match="prevention draft events"):
            state._connection.execute(  # noqa: SLF001 - immutability assertion
                "UPDATE prevention_draft_events SET phase = 'abandoned'"
            )
