import json
import pandas as pd

from gold_research.data import DataDownloadError, save_dataset


def test_synthetic_data_writes_csv_parquet_and_manifest(tmp_path):
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=3), "Close": [1.0, 2.0, 3.0]})
    manifest = save_dataset(frame, "GLD", "https://example.invalid/chart", "2010-01-01", tmp_path)
    assert (tmp_path / "GLD.csv").exists()
    assert (tmp_path / "GLD.parquet").exists()
    assert (tmp_path / "data_manifest.json").exists()
    assert manifest["rows"] == 3
    assert len(manifest["sha256"]) == 64
    assert len(manifest["csv_sha256"]) == 64
    assert len(manifest["parquet_sha256"]) == 64
    assert json.loads((tmp_path / "data_manifest.json").read_text())["GLD"]["symbol"] == "GLD"


def test_download_error_is_clear():
    err = DataDownloadError("GLD", "network down")
    assert "GLD" in str(err) and "network down" in str(err)
