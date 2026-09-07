from __future__ import annotations

import base64
import json
import unittest
from datetime import date
from typing import Any

import requests

from opus_dashboard.opus_client import (
    OpusClient,
    OpusCredentials,
    _request_template,
)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, Any]] = []

    def post(self, url: str, *, json: Any, timeout: int) -> FakeResponse:
        self.calls.append(("POST", url, json))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, *, timeout: int) -> FakeResponse:
        self.calls.append(("GET", url, None))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass


class OpusClientTests(unittest.TestCase):
    def test_authentication_uses_opus_auth_token_header(self) -> None:
        token = {
            "PublicKey": "public-key",
            "Email": "ops@example.com",
            "CompanyID": "company-1",
        }
        client = OpusClient("https://example.test/api/")
        client.session = FakeSession([FakeResponse(token)])

        identity = client.authenticate(
            OpusCredentials("ops@example.com", "secret-password")
        )

        encoded = client.session.headers["AuthToken"]
        self.assertEqual(
            json.loads(base64.b64decode(encoded).decode("utf-8")),
            token,
        )
        self.assertEqual(identity["email"], "ops@example.com")
        self.assertEqual(
            client.session.calls[0][2],
            {"Email": "ops@example.com", "Password": "secret-password"},
        )

    def test_job_pages_accept_items_envelope(self) -> None:
        client = OpusClient("https://example.test/api/")
        client.session = FakeSession(
            [
                FakeResponse(
                    {
                        "Items": [
                            {
                                "ID": "11111111-1111-4111-8111-111111111111",
                                "Reference": "ORDBULK-1",
                            }
                        ],
                        "CanPaginate": False,
                    }
                )
            ]
        )

        pages = list(
            client.iter_jobs(date(2026, 1, 1), date(2026, 7, 23))
        )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][0]["Reference"], "ORDBULK-1")
        self.assertTrue(
            client.session.calls[0][1].endswith(
                "Jobs/GetJobs/false/true/true"
            )
        )

    def test_job_request_searches_reference_field(self) -> None:
        request = _request_template(
            date(2026, 1, 1),
            date(2026, 7, 23),
            start=200,
            count=100,
        )

        self.assertEqual(request["Search"], "ORDBULK")
        self.assertEqual(request["SearchFieldType"], 1)
        self.assertEqual(request["Start"], 200)
        self.assertEqual(request["Count"], 100)
        self.assertEqual(request["DateFrom"], "2026-01-01")

    def test_job_request_can_filter_active_statuses(self) -> None:
        request = _request_template(
            date(2026, 1, 1),
            date(2026, 7, 23),
            job_statuses=(1, 2, 3, 6),
        )

        self.assertEqual(request["JobStatus"], [1, 2, 3, 6])

    def test_iter_jobs_accepts_targeted_search(self) -> None:
        client = OpusClient("https://example.test/api/")
        client.session = FakeSession([FakeResponse([])])

        list(
            client.iter_jobs(
                date(2026, 6, 29),
                date(2026, 8, 5),
                search="KFTS26-10M",
            )
        )

        self.assertEqual(
            client.session.calls[0][2]["Search"],
            "KFTS26-10M",
        )

    def test_job_bundle_unwraps_checklist_and_section_records(self) -> None:
        checklist_id = "22222222-2222-4222-8222-222222222222"
        section_id = "33333333-3333-4333-8333-333333333333"
        client = OpusClient("https://example.test/api/")
        client.session = FakeSession(
            [
                FakeResponse({"ID": "11111111-1111-4111-8111-111111111111"}),
                FakeResponse([{"ID": checklist_id, "Name": "Transport Allocation"}]),
                FakeResponse(
                    {
                        "0": {
                            "ID": checklist_id,
                            "Name": "Transport Allocation",
                            "Sections": [{"ID": section_id, "Name": "Section A"}],
                        }
                    }
                ),
                FakeResponse(
                    {
                        "0": {
                            "ID": section_id,
                            "SubSections": [
                                {
                                    "AnswerSubsections": [
                                        {
                                            "Answers": [
                                                {
                                                    "Question": "Truck Registration",
                                                    "Answer": "ABC 123 GP",
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                ),
            ]
        )

        bundle = client.job_bundle(
            "11111111-1111-4111-8111-111111111111",
            date(2026, 7, 1),
            date(2026, 7, 30),
        )

        checklist = bundle["checklists"][0]
        self.assertEqual(checklist["detail"]["Name"], "Transport Allocation")
        answer = checklist["sections"][0]["SubSections"][0][
            "AnswerSubsections"
        ][0]["Answers"][0]
        self.assertEqual(answer["Question"], "Truck Registration")

    def test_timed_out_read_is_retried_with_telemetry(self) -> None:
        retry_events: list[dict[str, Any]] = []
        client = OpusClient(
            "https://example.test/api/",
            read_attempts=2,
            retry_backoff_seconds=0,
            on_retry=retry_events.append,
        )
        client.session = FakeSession(
            [
                requests.Timeout("slow OPUS response"),
                FakeResponse(
                    {
                        "Items": [
                            {
                                "ID": "11111111-1111-4111-8111-111111111111",
                                "Reference": "ORDBULK-1",
                            }
                        ],
                        "CanPaginate": False,
                    }
                ),
            ]
        )

        pages = list(client.iter_jobs(date(2026, 7, 1), date(2026, 7, 30)))

        self.assertEqual(len(pages), 1)
        self.assertEqual(len(client.session.calls), 2)
        self.assertEqual(retry_events[0]["attempt"], 2)
        self.assertEqual(retry_events[0]["attempt_limit"], 2)


if __name__ == "__main__":
    unittest.main()
