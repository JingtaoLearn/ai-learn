import csv
import importlib.util
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_strategy_lab.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_strategy_lab", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_prices(path: Path, offset: float):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Date", "Open", "Close"])
        writer.writeheader()
        for index in range(320):
            writer.writerow({"Date": (date(2024, 1, 1) + timedelta(days=index)).isoformat(), "Open": 100 + offset + index, "Close": 100.5 + offset + index})
        writer.writerow({"Date": "</script><script>alert(1)</script>", "Open": 1, "Close": 1})
        writer.writerow({"Date": "2025-01-01", "Open": float("inf"), "Close": 1})


def test_build_strategy_lab_embeds_data_and_all_source_assets(tmp_path):
    builder = load_builder()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_prices(data_dir / "GC_F.csv", 0)
    write_prices(data_dir / "GLD.csv", 10)
    output = tmp_path / "lab.html"
    data = builder.load_sources(data_dir, refresh=False)
    builder.build(ROOT, output, data)
    content = output.read_text()
    assert content.startswith("<!doctype html>")
    assert "黄金策略实验室" in content
    assert "window.GOLD_LAB_DATA=" in content
    assert "GC=F" in content and "GLD" in content
    assert "/*__DATA__*/" not in content
    assert "function generatePositions" in content
    assert "2024-11-15" in content
    assert "alert(1)" not in content
    assert '"open":Infinity' not in content
