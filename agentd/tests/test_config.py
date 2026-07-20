import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gauss_agentd.config import load_config, resolve_cline_path, cmd_hash

def test_load_default_config():
    config = load_config()
    assert config.ipc.transport == "tcp"
    assert config.ipc.tcp_port == 19876
    assert config.cline.timeout_seconds == 600
    assert config.cline.max_retries == 1

def test_resolve_cline_path_auto():
    path = resolve_cline_path("auto")
    assert path
    assert os.path.exists(path)

def test_cmd_hash():
    h1 = cmd_hash(["cline", "-p", "-c", "test"])
    h2 = cmd_hash(["cline", "-p", "-c", "test"])
    h3 = cmd_hash(["cline", "-p", "-c", "other"])
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64
    print("config tests passed")
