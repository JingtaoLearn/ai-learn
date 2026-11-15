#!/usr/bin/env python3
"""Hermes no-agent cron script. Hermes passes no arguments, so the job comes from the
installed file name: quant_daily_cn.py, quant_daily_us.py, quant_alerts.py.

Calls the serving app through the ailearn->zhlearn tunnel and prints the Feishu text;
empty stdout means Hermes stays silent.
"""
import json
import pathlib
import sys
import urllib.request

ENV = pathlib.Path.home() / ".config/quant-signal/env"
JOBS = {"quant_daily_cn": ("daily", "cn,sge"), "quant_daily_us": ("daily", "us"), "quant_alerts": ("alerts", "")}


def main() -> int:
    cfg = dict(line.split("=", 1) for line in ENV.read_text().split() if "=" in line)
    job, markets = JOBS[pathlib.Path(sys.argv[0]).stem]
    url = f"{cfg['QUANT_URL']}/api/jobs/{job}" + (f"?markets={markets}" if markets else "")
    req = urllib.request.Request(url, method="POST",
                                 headers={"Authorization": f"Bearer {cfg['QUANT_API_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            text = json.load(r)["text"]
    except Exception as exc:  # alerts run every few minutes; only daily failures are worth a message
        if job == "daily":
            print(f"❗趋势信号任务失败：{exc}")
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
