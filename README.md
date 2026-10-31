# Qlib-Futu Research Bridge

Read-only research integration between the validated Futu desktop market-data
bridge and Qlib. The project exports adjusted OHLCV data into Qlib's CSV/bin
workflow, preserves source provenance, materializes option-chain snapshots, and
supports DeepSeek JSON extraction for news, earnings transcripts, and report
PDF pages.

The Futu desktop application itself is not part of this repository. Run the
bridge against a locally installed, signed-in Futu desktop session.

## Install

```powershell
python -m venv .venv-qlib
.\.venv-qlib\Scripts\python.exe -m pip install -e ".[qlib]"
```

## Export prices

```powershell
python -m futu_bridge.qlib_export `
  --symbol US.AAPL --symbol HK.00700 `
  --timeframe 1d --start 2020-01-01 --end 2026-10-23 `
  --adjustment forward --output-dir output\qlib_source --overwrite
```

Use the matching Qlib source checkout's `scripts/dump_bin.py` to convert the
CSV files to Qlib binary data. See `futu_bridge/README.md` for the full
workflow and point-in-time limitations.

## DeepSeek extraction

Set `DEEPSEEK_API_KEY` in the process environment. Never commit the key.

```powershell
$env:DEEPSEEK_API_KEY = "<your key>"
futu-deepseek-extract `
  --input-jsonl output\research\events.jsonl `
  --output-jsonl output\research\claims.jsonl
```

DeepSeek results retain source IDs, source hashes, evidence quotes, model
metadata, and extraction time. Model extraction time is not treated as a
historical availability timestamp.

## Tests

```powershell
python -m unittest discover -s futu_bridge/tests
```
