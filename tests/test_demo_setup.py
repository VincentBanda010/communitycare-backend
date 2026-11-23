import base64
from app.setup_demo import build_metadata, event_payload, save_env, report_errors
from dotenv import dotenv_values


def test_setup_metadata_and_event_references_are_consistent():
    metadata, mapping = build_metadata("test1234", "Abcdef12345", "Bbcdef12345")
    assert metadata["programs"][0]["programType"] == "WITHOUT_REGISTRATION"
    assert metadata["programs"][0]["id"] == mapping["DHIS2_PROGRAM"]
    assert metadata["programStages"][0]["program"]["id"] == mapping["DHIS2_PROGRAM"]
    assert metadata["dataElements"][0]["id"] == mapping["DHIS2_TRIAGE_ELEMENT"]
    payload = event_payload(mapping, "REFERRAL")
    assert payload["events"][0]["programStage"] == mapping["DHIS2_STAGE"]
    assert payload["events"][0]["dataValues"][0]["dataElement"] == mapping["DHIS2_TRIAGE_ELEMENT"]


def test_env_update_preserves_database_and_encryption_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# operator settings\nDATABASE_PASSWORD=keep-me\nSYNC_ENCRYPTION_KEY=keep-key\nDHIS2_TOKEN=old\n")
    save_env(path, {"DHIS2_TOKEN": "new", "DHIS2_URL": "https://example.com"})
    config = dotenv_values(path)
    assert config["DATABASE_PASSWORD"] == "keep-me"
    assert config["SYNC_ENCRYPTION_KEY"] == "keep-key"
    assert config["DHIS2_TOKEN"] == "new"


def test_new_env_generates_key_without_inventing_missing_services(tmp_path):
    path = tmp_path / ".env"
    save_env(path, {"DHIS2_URL": "https://example.com", "DHIS2_TOKEN": "test"})
    config = dotenv_values(path)
    assert len(base64.b64decode(config["SYNC_ENCRYPTION_KEY"])) == 32
    assert config["DATABASE_PASSWORD"] == ""
    assert config["OIDC_ISSUER"] == ""
    assert not path.with_name(path.name + ".tmp").exists()


def test_validation_errors_are_detected():
    assert report_errors({"validationReport": {"errorReports": [{"errorCode": "E1000"}]}})
    assert not report_errors({"validationReport": {"errorReports": []}})
    assert report_errors([])
