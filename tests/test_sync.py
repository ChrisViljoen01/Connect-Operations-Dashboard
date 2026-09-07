from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

from opus_dashboard.sync import (
    OpusSyncEngine,
    _answer_pairs,
    _business_fields,
    _checklist_answer_rows,
    _date_chunks,
    _is_terminal_status,
    _job_checklist_name,
    _live_scan_start,
    _qualify_transport_workflows,
    _select_bounded_candidates,
    _should_load_detail,
    _source_job_row,
    _stage_config,
    _stored_job_row,
    _timestamp,
    _uuid_text,
)


class SyncHelperTests(unittest.TestCase):
    @staticmethod
    def _job(
        job_id: str,
        reference: str,
        checklist: str,
        created_at: str,
    ) -> dict[str, str]:
        return {
            "ID": job_id,
            "Reference": reference,
            "ChecklistName": checklist,
            "CreateDate": created_at,
            "StatusDescription": "Operator Signed Off",
        }

    def test_submitted_answer_takes_precedence_over_question_detail(self) -> None:
        pairs = _answer_pairs(
            {
                "Question": "Any question with supporting detail",
                "Text": "Instructional text explaining what to capture.",
                "ReportFormattedAnswer": (
                    "Instructional text explaining what to capture."
                ),
                "Answer": "Submitted value",
                "UnformattedAnswer": "Submitted value",
            }
        )

        self.assertEqual(
            pairs,
            [("Any question with supporting detail", "Submitted value")],
        )

    def test_month_chunks_cover_range_without_overlap(self) -> None:
        chunks = list(
            _date_chunks(date(2026, 1, 1), date(2026, 3, 5), days=31)
        )

        self.assertEqual(
            chunks,
            [
                (date(2026, 1, 1), date(2026, 1, 31)),
                (date(2026, 2, 1), date(2026, 3, 3)),
                (date(2026, 3, 4), date(2026, 3, 5)),
            ],
        )

    def test_live_scan_uses_bounded_recent_window(self) -> None:
        self.assertEqual(
            _live_scan_start(
                date(2026, 6, 29),
                date(2026, 8, 18),
                7,
            ),
            date(2026, 8, 12),
        )
        self.assertEqual(
            _live_scan_start(
                date(2026, 8, 15),
                date(2026, 8, 18),
                7,
            ),
            date(2026, 8, 15),
        )

    def test_bounded_candidates_reserve_both_audit_types(self) -> None:
        entries = [
            ((1, float(index)), {"ID": f"live-{index}"})
            for index in range(60)
        ]
        entries.extend(
            (
                (4, float(index)),
                {"ID": f"active-{index}", "_audit_kind": "active"},
            )
            for index in range(10)
        )
        entries.extend(
            (
                (5, float(index)),
                {"ID": f"history-{index}", "_audit_kind": "historical"},
            )
            for index in range(10)
        )

        selected = _select_bounded_candidates(entries, 50, full=False)
        selected_ids = [str(row["ID"]) for row in selected]

        self.assertEqual(len(selected), 50)
        self.assertEqual(
            sum(value.startswith("active-") for value in selected_ids),
            3,
        )
        self.assertEqual(
            sum(value.startswith("history-") for value in selected_ids),
            2,
        )

    def test_raw_record_uses_engine_schema_capability_check(self) -> None:
        engine = object.__new__(OpusSyncEngine)
        engine._check_source_hash_column = Mock(return_value=False)
        cursor = Mock()
        cursor.connection = Mock()
        cursor.fetchone.return_value = None

        changed, inserted = engine._raw_record(
            cursor,
            1,
            "opus",
            "job-1",
            "job",
            {"ID": "job-1"},
        )

        self.assertTrue(changed)
        self.assertTrue(inserted)
        engine._check_source_hash_column.assert_called_once_with(cursor.connection)

    def test_checkpoint_is_due_when_timestamp_is_missing_or_expired(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value

        for row in (
            None,
            {"last_at": None},
            {
                "last_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=61)
                ).isoformat()
            },
        ):
            with self.subTest(row=row):
                cursor.fetchone.return_value = row
                self.assertTrue(
                    OpusSyncEngine._checkpoint_due(
                        connection,
                        "last_audit_at",
                        60,
                    )
                )

    def test_checkpoint_is_not_due_within_interval(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "last_at": (
                datetime.now(timezone.utc) - timedelta(minutes=3)
            ).isoformat()
        }

        self.assertFalse(
            OpusSyncEngine._checkpoint_due(
                connection,
                "last_active_sweep_at",
                4,
            )
        )

    def test_june_root_with_only_july_children_is_excluded(self) -> None:
        selection = _qualify_transport_workflows(
            [
                self._job(
                    "11111111-1111-4111-8111-111111111111",
                    "ORDBULK-1",
                    "Transport Allocation",
                    "2026-06-30T23:00:00+02:00",
                ),
                self._job(
                    "22222222-2222-4222-8222-222222222222",
                    "ORDBULK-1",
                    "Vehicle Inspection",
                    "2026-07-01T08:00:00+02:00",
                ),
            ],
            date(2026, 7, 1),
            date(2026, 8, 4),
        )

        self.assertEqual(selection.rows, [])
        self.assertEqual(selection.excluded_out_of_window_root_references, 1)

    def test_root_qualification_includes_all_chronological_downstream_jobs(self) -> None:
        selection = _qualify_transport_workflows(
            [
                self._job(
                    "44444444-4444-4444-8444-444444444444",
                    "ORDBULK-2",
                    "Offloading and Exit",
                    "2026-07-03T12:00:00+02:00",
                ),
                self._job(
                    "11111111-1111-4111-8111-111111111111",
                    "ORDBULK-2",
                    "Bulk Import for Minerals Transport Allocation",
                    "2026-07-01T06:00:00+02:00",
                ),
                self._job(
                    "22222222-2222-4222-8222-222222222222",
                    "ORDBULK-2",
                    "Transport Allocation",
                    "2026-07-01T07:00:00+02:00",
                ),
                self._job(
                    "33333333-3333-4333-8333-333333333333",
                    "ORDBULK-2",
                    "New Downstream Checklist",
                    "2026-07-02T09:00:00+02:00",
                ),
            ],
            date(2026, 7, 1),
            date(2026, 8, 4),
        )

        self.assertEqual(selection.eligible_references, 1)
        self.assertEqual(selection.bulk_import_jobs_excluded, 1)
        self.assertEqual(
            [row["ChecklistName"] for row in selection.rows],
            [
                "Transport Allocation",
                "New Downstream Checklist",
                "Offloading and Exit",
            ],
        )
        self.assertTrue(
            all(
                row["_qualified_transport_root_created_at"]
                == "2026-07-01T07:00:00+02:00"
                for row in selection.rows
            )
        )

    def test_qualification_excludes_duplicate_roots_and_pre_root_jobs(self) -> None:
        selection = _qualify_transport_workflows(
            [
                self._job(
                    "11111111-1111-4111-8111-111111111111",
                    "ORDBULK-3",
                    "Vehicle Inspection",
                    "2026-07-01T06:00:00+02:00",
                ),
                self._job(
                    "22222222-2222-4222-8222-222222222222",
                    "ORDBULK-3",
                    "Transport Allocation",
                    "2026-07-01T07:00:00+02:00",
                ),
                self._job(
                    "33333333-3333-4333-8333-333333333333",
                    "ORDBULK-3",
                    "Transport Allocation",
                    "2026-07-01T08:00:00+02:00",
                ),
            ],
            date(2026, 7, 1),
            date(2026, 8, 4),
        )

        self.assertEqual(len(selection.rows), 1)
        self.assertEqual(selection.duplicate_root_jobs_excluded, 1)
        self.assertEqual(selection.pre_root_jobs_excluded, 1)

    def test_source_job_row_removes_internal_qualification_metadata(self) -> None:
        source = _source_job_row(
            {
                "ID": "11111111-1111-4111-8111-111111111111",
                "_qualified_transport_root_created_at": "2026-07-01T07:00:00+02:00",
            }
        )

        self.assertEqual(
            source,
            {"ID": "11111111-1111-4111-8111-111111111111"},
        )

    def test_business_fields_are_derived_from_checklist_answers(self) -> None:
        bundle = {
            "checklists": [
                {
                    "detail": {
                        "Questions": [
                            {
                                "QuestionText": "1.4 Loading Point",
                                "AnswerText": "Horizon Mine",
                            },
                            {
                                "QuestionText": "1.5 Offloading Point",
                                "AnswerText": "CCIS",
                            },
                            {
                                "QuestionText": "Truck Registration",
                                "AnswerText": "ABC 123 GP",
                            },
                        ]
                    }
                }
            ]
        }

        fields = _business_fields(bundle, {})

        self.assertEqual(fields["loading_point"], "Horizon Mine")
        self.assertEqual(fields["offloading_point"], "CCIS")
        self.assertEqual(fields["truck_registration"], "ABC 123 GP")

    def test_order_number_does_not_match_verify_order_reference(self) -> None:
        bundle = {
            "checklists": [
                {
                    "sections": [
                        {
                            "SubSections": [
                                {
                                    "AnswerSubsections": [
                                        {
                                            "Answers": [
                                                {
                                                    "Question": "Verify Order Reference",
                                                    "Text": "Does the order reference match?",
                                                    "Answer": "Yes",
                                                },
                                                {
                                                    "Question": "1.2 Order Number",
                                                    "Answer": "ORD-2026-001",
                                                },
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        fields = _business_fields(bundle, {})

        self.assertEqual(fields["order_reference"], "ORD-2026-001")

    def test_known_stage_names_are_canonical(self) -> None:
        self.assertEqual(
            _stage_config("Loading and Exit"),
            ("loading_exit", 2.20, "conditional"),
        )

    def test_job_uses_its_own_checklist_name_not_parent_name(self) -> None:
        self.assertEqual(
            _job_checklist_name(
                {
                    "ChecklistName": "Loading and Exit",
                    "CreatedFromChecklistName": "Transport Allocation",
                }
            ),
            "Loading and Exit",
        )

    def test_initial_full_import_resumes_only_missing_or_changed_detail(self) -> None:
        self.assertFalse(
            _should_load_detail(
                force_reload_all=False,
                detail_exists=True,
                changed=False,
                status="Operator Signed Off",
            )
        )
        self.assertTrue(
            _should_load_detail(
                force_reload_all=False,
                detail_exists=False,
                changed=False,
                status="Operator Signed Off",
            )
        )
        self.assertTrue(
            _should_load_detail(
                force_reload_all=False,
                detail_exists=True,
                changed=True,
                status="Operator Signed Off",
            )
        )

    def test_completed_full_audit_reloads_existing_detail(self) -> None:
        self.assertTrue(
            _should_load_detail(
                force_reload_all=True,
                detail_exists=True,
                changed=False,
                status="Operator Signed Off",
            )
        )

    def test_unchanged_active_detail_waits_for_bounded_audit(self) -> None:
        self.assertFalse(
            _should_load_detail(
                force_reload_all=False,
                detail_exists=True,
                changed=False,
                status="Operator In Progress",
            )
        )
        self.assertFalse(
            _should_load_detail(
                force_reload_all=False,
                detail_exists=True,
                changed=False,
                status="Operator Not Started",
            )
        )

    def test_stored_audit_row_preserves_source_fields(self) -> None:
        row = _stored_job_row(
            {
                "job_id": "11111111-1111-4111-8111-111111111111",
                "job_reference": "ORDBULK-1",
                "checklist_name": "Vehicle Inspection",
                "status": "Operator In Progress",
                "raw_attributes": {
                    "list": {
                        "AnswerEvaluationID": (
                            "22222222-2222-4222-8222-222222222222"
                        )
                    }
                },
            },
            audit_requested=True,
        )

        self.assertEqual(row["Reference"], "ORDBULK-1")
        self.assertEqual(row["ChecklistName"], "Vehicle Inspection")
        self.assertEqual(
            row["AnswerEvaluationID"],
            "22222222-2222-4222-8222-222222222222",
        )
        self.assertTrue(row["_audit_requested"])

    def test_previously_errored_jobs_are_retried(self) -> None:
        self.assertTrue(
            _should_load_detail(
                force_reload_all=False,
                detail_exists=True,
                changed=False,
                status="Operator Signed Off",
                previously_errored=True,
            )
        )

    def test_terminal_statuses_do_not_force_unchanged_detail_reload(self) -> None:
        self.assertTrue(_is_terminal_status("Operator Signed Off"))
        self.assertTrue(_is_terminal_status("Job Closed"))
        self.assertTrue(_is_terminal_status("Completed"))
        self.assertFalse(_is_terminal_status("Operator In Progress"))
        self.assertFalse(_is_terminal_status("Unknown"))

    def test_opus_timestamp_is_timezone_aware(self) -> None:
        parsed = _timestamp("2026-07-23T09:00:00+02:00")
        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)

    def test_opus_year_one_timestamp_is_treated_as_unset(self) -> None:
        self.assertIsNone(_timestamp("0001-01-01T00:00:00+00:00"))

    def test_opus_nil_uuid_is_treated_as_unset(self) -> None:
        self.assertIsNone(
            _uuid_text("00000000-0000-0000-0000-000000000000")
        )

    def test_checklist_answers_keep_section_and_subsection_context(self) -> None:
        item = {
            "sections": [
                {
                    "ID": "11111111-1111-4111-8111-111111111111",
                    "Name": "Section A",
                    "SubSections": [
                        {
                            "Name": "Transport Allocation",
                            "AnswerSubsections": [
                                {
                                    "AnswersubsectionID": (
                                        "22222222-2222-4222-8222-222222222222"
                                    ),
                                    "Answers": [
                                        {
                                            "AnswerID": (
                                                "33333333-3333-4333-8333-333333333333"
                                            ),
                                            "Question": "Loading Point",
                                            "Answer": "Horizon Mine",
                                            "AnswerItems": [{"Code": "SITE-1"}],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        rows = _checklist_answer_rows(item)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["section_sequence"], 1)
        self.assertEqual(rows[0]["section"]["Name"], "Section A")
        self.assertEqual(rows[0]["subsection"]["Name"], "Transport Allocation")
        self.assertEqual(rows[0]["answer"]["Question"], "Loading Point")
        self.assertEqual(rows[0]["answer"]["AnswerItems"][0]["Code"], "SITE-1")

    def test_checklist_answer_fallback_keeps_full_question_payload(self) -> None:
        rows = _checklist_answer_rows(
            {
                "detail": {
                    "Name": "Transport Allocation",
                    "Questions": [
                        {
                            "QuestionText": "1.4 Loading Point",
                            "AnswerText": "Horizon Mine",
                            "Comments": "Confirmed by operator",
                            "QuestionSummary": "Dispatch origin",
                            "AnswerImages": [{"ID": "image-1"}],
                            "AnswerItems": [{"Code": "SITE-1"}],
                            "ChildChecklistAnswers": [{"ID": "child-1"}],
                            "Tabledata": {"rows": [["A", "B"]]},
                        }
                    ],
                }
            }
        )

        self.assertEqual(len(rows), 1)
        answer = rows[0]["answer"]
        self.assertEqual(answer["Question"], "1.4 Loading Point")
        self.assertEqual(answer["Answer"], "Horizon Mine")
        self.assertEqual(answer["Comments"], "Confirmed by operator")
        self.assertEqual(answer["QuestionSummary"], "Dispatch origin")
        self.assertEqual(answer["AnswerImages"][0]["ID"], "image-1")
        self.assertEqual(answer["AnswerItems"][0]["Code"], "SITE-1")
        self.assertEqual(answer["ChildChecklistAnswers"][0]["ID"], "child-1")
        self.assertEqual(answer["Tabledata"]["rows"][0], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
