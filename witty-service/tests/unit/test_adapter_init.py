def test_adapter_exports():
    from src.adapter import (
        WebSocketClient,
        WebSocketClientPool,
        AdaptorEndpoint,
        AdaptorConnectionError,
        AdaptorConnectionTimeout,
        AdaptorSendFailed,
        AdaptorReceiveError,
    )
    assert WebSocketClient is not None
    assert WebSocketClientPool is not None
    assert AdaptorEndpoint is not None
    assert AdaptorConnectionError is not None
    assert AdaptorConnectionTimeout is not None
    assert AdaptorSendFailed is not None
    assert AdaptorReceiveError is not None
