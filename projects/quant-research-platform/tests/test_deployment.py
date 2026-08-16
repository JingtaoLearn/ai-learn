from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_context_is_allowlisted_and_build_uses_hash_locked_dependencies():
    dockerignore = (ROOT / ".dockerignore").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerignore.splitlines()[0] == "*"
    assert "!.env" not in dockerignore
    assert "COPY ." not in dockerfile
    assert "@sha256:" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "requirements.lock" in dockerfile


def test_jupyter_is_loopback_only_authenticated_and_non_root():
    compose = (ROOT / "compose.yaml").read_text()
    assert '127.0.0.1:8888:8888' in compose
    assert "JUPYTER_TOKEN" in compose
    assert "ServerApp.token=" not in compose
    assert 'user: "1000:1000"' in compose