from src.sandbox.base import AdapterEndpoint

def test_ws_url_for_http():
    endpoint = AdapterEndpoint(base_url="http://localhost:8080")
    assert endpoint.ws_url == "ws://localhost:8080/agent/sessions/{session_id}/ws"

def test_ws_url_for_https():
    endpoint = AdapterEndpoint(base_url="https://localhost:8080")
    assert endpoint.ws_url == "wss://localhost:8080/agent/sessions/{session_id}/ws"

def test_ws_endpoint():
    endpoint = AdapterEndpoint(base_url="http://localhost:8080")
    assert endpoint.ws_endpoint("session-123") == "ws://localhost:8080/agent/sessions/session-123/ws"
