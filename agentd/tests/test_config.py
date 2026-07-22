import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_hub.config as config_module
from agent_hub.config import load_config, resolve_cline_path, cmd_hash
import agent_hub.codex_executor as codex_module

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

def test_resolve_cline_next_to_python_when_path_is_minimal(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    cline = bin_dir / "cline"
    python.write_text("")
    cline.write_text("")
    monkeypatch.setattr(config_module.sys, "executable", str(python))
    monkeypatch.setattr(config_module.shutil, "which", lambda _: None)
    monkeypatch.delenv("CLINE_PATH", raising=False)
    assert resolve_cline_path("auto") == str(cline)

def test_resolve_codex_from_vscode_extension(tmp_path, monkeypatch):
    extension_root = tmp_path / ".vscode-server" / "extensions"
    codex = extension_root / "openai.chatgpt-test" / "bin" / "linux-x86_64" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("")
    monkeypatch.setattr(codex_module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(codex_module.shutil, "which", lambda _: None)
    assert codex_module.resolve_codex_path() == str(codex)

def test_cmd_hash():
    h1 = cmd_hash(["cline", "-p", "-c", "test"])
    h2 = cmd_hash(["cline", "-p", "-c", "test"])
    h3 = cmd_hash(["cline", "-p", "-c", "other"])
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64
    print("config tests passed")
