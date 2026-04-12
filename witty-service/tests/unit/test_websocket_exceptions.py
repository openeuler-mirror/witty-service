import pytest
from src.adapter.exceptions import (
    AdaptorConnectionError,
    AdaptorConnectionTimeout,
    AdaptorSendFailed,
    AdaptorReceiveError,
)

def test_adaptor_connection_error_code():
    exc = AdaptorConnectionError(message="failed", details={"host": "localhost"})
    assert exc.code == "ADAPTOR_CONNECTION_ERROR"
    assert exc.message == "failed"
    assert exc.details["host"] == "localhost"

def test_adaptor_connection_timeout_code():
    exc = AdaptorConnectionTimeout(message="timeout", details={})
    assert exc.code == "ADAPTOR_CONNECTION_TIMEOUT"

def test_adaptor_send_failed_code():
    exc = AdaptorSendFailed(message="send failed", details={})
    assert exc.code == "ADAPTOR_SEND_FAILED"

def test_adaptor_receive_error_code():
    exc = AdaptorReceiveError(message="receive failed", details={})
    assert exc.code == "ADAPTOR_RECEIVE_ERROR"