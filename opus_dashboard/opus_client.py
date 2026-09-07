from __future__ import annotations

import base64
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterator

if sys.platform == "win32":
    # OPUS is accessed from managed Windows devices whose trusted issuer chain
    # can include certificates installed in the Windows certificate store.
    # Keep full TLS verification enabled while making that system trust store
    # available to requests/urllib3.
    import truststore

    truststore.inject_into_ssl()

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class OpusApiError(RuntimeError):
    """Raised when OPUS returns an unexpected response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpusAuthenticationError(OpusApiError):
    """Raised when OPUS rejects a login or authenticated request."""


@dataclass(frozen=True, slots=True)
class OpusCredentials:
    email: str
    password: str


def _request_template(
    date_from: date,
    date_to: date,
    *,
    start: int = 0,
    count: int = 100,
    search: str = "ORDBULK",
    job_statuses: tuple[int, ...] = (),
) -> dict[str, Any]:
    return {
        "Start": start,
        "Count": count,
        "DateFrom": date_from.isoformat(),
        "DateTo": date_to.isoformat(),
        "JobStatus": list(job_statuses),
        "SiteJobStatus": [],
        "AlertTaskStatus": [],
        "ChecklistStatus": [],
        "DocumentStatus": [],
        "ChecklistFilterType": 0,
        "JobsCalendarFilterType": 0,
        "TeamID": "",
        "UserID": "",
        "SiteID": "",
        "EvaluationTagID": "",
        "TagID": [],
        "TagsFilterType": 0,
        "SiteTagID": [],
        "SiteTagsFilterType": 0,
        "TagType": 1,
        "JobID": "",
        "ProjectGroupID": "",
        "ProjectID": "",
        "ChecklistPriority": 0,
        "EvaluationID": "",
        "AnsEvaluationID": "",
        "FileFormat": "",
        "Search": search,
        "SearchFieldType": 1,
        "Value": 1,
        "View": 0,
        "ID": "",
        "DeletedJobs": False,
        "Overdue": False,
        "Recurring": False,
        "JobsWithNoOperator": False,
        "JobsWithNoProject": False,
        "JobsWithNoSite": False,
        "JobsWithNoLogs": False,
        "JobsWithNoExpectedStartDate": False,
        "JobsInPendingState": False,
        "JobsDownloadedToApp": False,
        "JobsNotDownloadedToApp": False,
        "MyJobs": False,
        "ReportingDateFrom": date_from.isoformat(),
        "ReportingDateTo": date_to.isoformat(),
        "InheritedView": False,
    }


class OpusClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 45,
        read_attempts: int = 3,
        retry_backoff_seconds: int = 2,
        on_retry: Callable[[dict[str, Any]], None] | None = None,
        section_workers: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.read_attempts = max(read_attempts, 1)
        self.retry_backoff_seconds = max(retry_backoff_seconds, 0)
        self.on_retry = on_retry
        self.section_workers = max(section_workers, 1)
        self.session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            # Do not repeat a POST after a response read timeout. OPUS uses POST
            # for read operations, and retrying it can make a slow endpoint look
            # frozen for several minutes.
            read=0,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=utf-8",
                "Origin": "https://app.opus4business.com",
                "Referer": "https://app.opus4business.com/",
                "User-Agent": "ConnectLogisticsOperationsDashboard/0.2",
            }
        )
        self._auth_token: dict[str, Any] | None = None

    def close(self) -> None:
        self.session.close()

    def authenticate(self, credentials: OpusCredentials) -> dict[str, Any]:
        email = credentials.email.strip()
        if not email or not credentials.password:
            raise OpusAuthenticationError("OPUS email and password are required.")
        response = self.session.post(
            self.base_url + "Security/WebLogin",
            json={"Email": email, "Password": credentials.password},
            timeout=self.timeout,
        )
        payload = self._decode(response, "OPUS login")
        if not isinstance(payload, dict) or not payload.get("PublicKey"):
            message = (
                str(payload.get("Message") or "OPUS rejected the supplied credentials.")
                if isinstance(payload, dict)
                else "OPUS returned an invalid login response."
            )
            raise OpusAuthenticationError(message)
        encoded = base64.b64encode(
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).decode("ascii")
        self.session.headers["AuthToken"] = encoded
        self._auth_token = payload
        return {
            "email": payload.get("Email") or email,
            "company_id": payload.get("CompanyID") or payload.get("CompanyId"),
            "user_id": payload.get("UserID") or payload.get("UserId"),
            "base_version": payload.get("BaseVersion"),
        }

    def clone_authenticated(self) -> "OpusClient":
        if self._auth_token is None:
            raise OpusAuthenticationError("The OPUS client is not authenticated.")
        clone = OpusClient(
            self.base_url,
            self.timeout,
            self.read_attempts,
            self.retry_backoff_seconds,
            self.on_retry,
            self.section_workers,
        )
        clone._auth_token = self._auth_token
        clone.session.headers["AuthToken"] = self.session.headers["AuthToken"]
        return clone

    def checklist_definitions(self) -> list[dict[str, Any]]:
        payload = self._post(
            "Evaluations/GetEvaluations",
            "",
            timeout=min(self.timeout, 15),
            attempts=1,
        )
        return _rows_from_payload(
            payload,
            "Data",
            "Items",
            "Evaluations",
            "Checklists",
            "Result",
        )

    def iter_jobs(
        self,
        date_from: date,
        date_to: date,
        *,
        page_size: int = 100,
        search: str = "ORDBULK",
        job_statuses: tuple[int, ...] = (),
    ) -> Iterator[list[dict[str, Any]]]:
        start = 0
        while True:
            request = _request_template(
                date_from,
                date_to,
                start=start,
                count=page_size,
                search=search,
                job_statuses=job_statuses,
            )
            payload = self._post("Jobs/GetJobs/false/true/true", request)
            rows = _rows_from_payload(
                payload,
                "Data",
                "Items",
                "Jobs",
                "Records",
                "Result",
            )
            if not isinstance(payload, (dict, list)):
                raise OpusApiError("OPUS returned an invalid jobs response.")
            can_paginate_value = (
                _first_value(payload, "CanPaginate", "HasMore", "MoreRecords")
                if isinstance(payload, dict)
                else None
            )
            can_paginate = (
                bool(can_paginate_value)
                if can_paginate_value is not None
                else len(rows) >= page_size
            )
            if not rows:
                break
            yield rows
            start += len(rows)
            if not can_paginate or len(rows) < page_size:
                break

    def job_bundle(
        self,
        job_id: str,
        date_from: date,
        date_to: date,
        checklist_instance_id: str | None = None,
    ) -> dict[str, Any]:
        request = _request_template(date_from, date_to, count=100)
        detail = self._post(f"Jobs/GetJob/{job_id}/false", None)
        if checklist_instance_id:
            checklists = [{"ID": checklist_instance_id}]
        else:
            checklists_payload = self._post(
                f"Evaluations/GetChecklistsForJob/{job_id}",
                request,
            )
            checklists = _rows_from_payload(
                checklists_payload,
                "Data",
                "Items",
                "Jobs",
                "Checklists",
                "Result",
            )
        checklist_details: list[dict[str, Any]] = []
        for checklist in checklists if isinstance(checklists, list) else []:
            if not isinstance(checklist, dict):
                continue
            checklist_id = _first_value(
                checklist,
                "ChecklistID",
                "ChecklistId",
                "ID",
                "Id",
            )
            if not checklist_id:
                checklist_details.append({"summary": checklist, "detail": checklist})
                continue
            full = _single_record(
                self._get(f"Evaluations/GetChecklist/{checklist_id}")
            )
            sections: list[dict[str, Any]] = []
            detail_errors: list[dict[str, Any]] = []
            if isinstance(full, dict):
                section_rows = [
                    section
                    for section in (full.get("Sections") or full.get("sections") or [])
                    if isinstance(section, dict)
                    and _first_value(section, "ID", "Id", "SectionID")
                ]

                def fetch_section(
                    section: dict[str, Any],
                ) -> tuple[dict[str, Any], dict[str, Any] | None]:
                    section_id = _first_value(section, "ID", "Id", "SectionID")
                    section_client = self
                    owns_client = False
                    if len(section_rows) > 1:
                        section_client = self.clone_authenticated()
                        owns_client = True
                    try:
                        section_detail = _single_record(
                            section_client._get(
                                "Evaluations/GetChecklistSectionDetailByID/"
                                f"{checklist_id}/{section_id}"
                            )
                        )
                        return (
                            section,
                            section_detail if isinstance(section_detail, dict) else None,
                        )
                    except OpusApiError as exc:
                        if exc.status_code != 404:
                            raise
                        return (
                            section,
                            {
                                "section_id": str(section_id),
                                "error": str(exc),
                            },
                        )
                    finally:
                        if owns_client:
                            section_client.close()

                if len(section_rows) <= 1:
                    results = [fetch_section(section) for section in section_rows]
                else:
                    worker_count = min(
                        max(self.section_workers, 1),
                        len(section_rows),
                    )
                    with ThreadPoolExecutor(
                        max_workers=worker_count,
                        thread_name_prefix="opus-sections",
                    ) as pool:
                        futures = [
                            pool.submit(fetch_section, section)
                            for section in section_rows
                        ]
                        results = [future.result() for future in as_completed(futures)]
                    order = {
                        str(_first_value(section, "ID", "Id", "SectionID")): index
                        for index, section in enumerate(section_rows)
                    }
                    results.sort(
                        key=lambda result: order[
                            str(_first_value(result[0], "ID", "Id", "SectionID"))
                        ]
                    )

                for _section, result in results:
                    if result is None:
                        continue
                    if "error" in result and "section_id" in result:
                        detail_errors.append(result)
                    else:
                        sections.append(result)
            checklist_details.append(
                {
                    "summary": checklist,
                    "detail": full,
                    "sections": sections,
                    "detail_errors": detail_errors,
                }
            )
        return {
            "job": detail,
            "checklists": checklist_details,
        }

    def _get(self, path: str) -> Any:
        return self._request(
            "GET",
            path,
            None,
            timeout=self.timeout,
        )

    def _post(
        self,
        path: str,
        payload: Any,
        *,
        timeout: int | None = None,
        attempts: int | None = None,
    ) -> Any:
        return self._request(
            "POST",
            path,
            payload,
            timeout=timeout or self.timeout,
            attempts=attempts,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: Any,
        *,
        timeout: int,
        attempts: int | None = None,
    ) -> Any:
        operation = path.lstrip("/")
        attempt_limit = max(attempts or self.read_attempts, 1)
        for attempt in range(1, attempt_limit + 1):
            try:
                if method == "GET":
                    response = self.session.get(
                        self.base_url + operation,
                        timeout=timeout,
                    )
                else:
                    response = self.session.post(
                        self.base_url + operation,
                        json=payload,
                        timeout=timeout,
                    )
                return self._decode(response, operation)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= attempt_limit:
                    raise OpusApiError(
                        f"OPUS request {operation} timed out after "
                        f"{attempt_limit} attempts."
                    ) from exc
                if self.on_retry:
                    self.on_retry(
                        {
                            "operation": operation,
                            "attempt": attempt + 1,
                            "attempt_limit": attempt_limit,
                            "error_type": type(exc).__name__,
                        }
                    )
                time.sleep(self.retry_backoff_seconds * attempt)
        raise AssertionError("OPUS request retry loop did not return or raise.")

    @staticmethod
    def _decode(response: requests.Response, operation: str) -> Any:
        if response.status_code in {401, 403}:
            raise OpusAuthenticationError(
                f"OPUS authorization failed while requesting {operation}.",
                status_code=response.status_code,
            )
        if not response.ok:
            raise OpusApiError(
                f"OPUS request {operation} failed with HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise OpusApiError(
                f"OPUS request {operation} returned non-JSON data."
            ) from exc


def _first_value(data: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).casefold(): value for key, value in data.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def _rows_from_payload(payload: Any, *names: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = _first_value(payload, *names)
        if isinstance(rows, dict):
            rows = _first_value(rows, *names)
    else:
        rows = None
    return [row for row in rows or [] if isinstance(row, dict)]


def _single_record(payload: Any) -> Any:
    if isinstance(payload, list):
        return next((row for row in payload if isinstance(row, dict)), payload)
    if isinstance(payload, dict) and payload:
        indexed_rows = [
            value
            for key, value in payload.items()
            if str(key).isdigit() and isinstance(value, dict)
        ]
        if indexed_rows:
            return indexed_rows[0]
    return payload
