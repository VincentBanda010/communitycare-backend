import logging
import threading
import httpx

log = logging.getLogger(__name__)


def outcome(status, report):
    if not isinstance(report, dict):
        return None
    validation = report.get("validationReport") or {}
    if status in (200, 201, 400, 409) and isinstance(validation, dict) and validation.get("errorReports"):
        return "REJECTED"
    if not 200 <= status < 300 or report.get("status") not in ("OK", "WARNING"):
        return None
    try:
        stats = report["bundleReport"]["typeReportMap"]["EVENT"]["stats"]
        if int(stats.get("created", 0)) + int(stats.get("updated", 0)) == 1:
            return "FORWARDED"
    except (KeyError, TypeError, ValueError):
        pass
    return None


class DeliveryWorker:
    def __init__(self, queue, settings, client=None):
        self.queue, self.settings = queue, settings
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
        )
        self.stop_event = threading.Event()
        self.thread = None

    def deliver(self, job):
        try:
            timestamp, triage = self.queue.read(job)
            event = {
                "event": job["event_uid"], "program": self.settings.program,
                "programStage": self.settings.stage, "orgUnit": job["org_unit"],
                "occurredAt": timestamp, "status": "COMPLETED",
                "dataValues": [{"dataElement": self.settings.triage_element, "value": triage}],
            }
            response = self.client.post(
                self.settings.dhis_url + "/api/tracker",
                params={"async": "false", "importStrategy": "CREATE_AND_UPDATE", "reportMode": "FULL"},
                headers={"Authorization": "ApiToken " + self.settings.dhis_token},
                json={"events": [event]},
            )
            try:
                report = response.json()
            except ValueError:
                report = None
            state = outcome(response.status_code, report)
        except Exception:
            # Never log tokens, clinical payloads or remote response bodies.
            log.warning("Encounter delivery failed; scheduling retry")
            state = None
        if state:
            self.queue.finish(job, state)
        else:
            self.queue.retry(job)

    def run(self):
        while not self.stop_event.is_set():
            try:
                for _ in range(20):
                    if self.stop_event.is_set():
                        break
                    job = self.queue.claim()
                    if not job:
                        break
                    self.deliver(job)
            except Exception:
                log.warning("Delivery queue unavailable; retrying on next poll")
            self.stop_event.wait(1)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="dhis-delivery", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        self.client.close()
