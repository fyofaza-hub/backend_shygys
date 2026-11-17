from fastapi.testclient import TestClient
from openapi_spec_validator import validate

from app.main import app


def test_openapi_and_cors():
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    validate(spec)
    props = spec["components"]["schemas"]["PublicCreator"]["properties"]
    assert {"legal_status", "residency", "moderation_status", "publication_status"} <= props.keys()
    assert {"kk", "ru", "en"} == spec["components"]["schemas"]["Localized"]["properties"].keys()
    catalog = spec["paths"]["/creators"]["get"]
    markers = spec["paths"]["/map/creators"]["get"]
    assert {p["name"] for p in catalog["parameters"]} | {"bbox", "zoom"} == {
        p["name"] for p in markers["parameters"]
    }
    cors = client.options(
        "/me",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert cors.headers["access-control-allow-credentials"] == "true"
    assert cors.headers["access-control-allow-origin"] == "http://localhost:3000"
