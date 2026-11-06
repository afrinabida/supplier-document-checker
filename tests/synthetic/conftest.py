"""Synthetic-only tests; block network, allowing stdlib socket-pair IPC only."""
import socket
import threading
import pytest


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    local=threading.local()
    connect=socket.socket.connect
    pair=socket.socketpair
    def guarded_connect(sock,address):
        if getattr(local,'pair',False) and isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):
            return connect(sock,address)
        raise RuntimeError('EXTERNAL_AND_UNSCOPED_NETWORK_BLOCKED')
    def guarded_pair(*args,**kwargs):
        local.pair=True
        try: return pair(*args,**kwargs)
        finally: local.pair=False
    def blocked(*args,**kwargs):
        raise RuntimeError('EXTERNAL_AND_UNSCOPED_NETWORK_BLOCKED')
    monkeypatch.setattr(socket.socket,'connect',guarded_connect)
    monkeypatch.setattr(socket.socket,'connect_ex',blocked)
    monkeypatch.setattr(socket,'socketpair',guarded_pair)
    for key in ('GEMINI_API_KEY','GOOGLE_API_KEY'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('LANGSMITH_TRACING','false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2','false')
