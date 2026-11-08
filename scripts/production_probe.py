"""Off-host, read-only product probes. No app/provider credentials or inference."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def notify(message: str) -> None:
    if source := os.environ.get("MONITOR_SOURCE"):
        message = f"[{source}]\n{message}"
    base = os.environ["TELEGRAM_API_BASE_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("Telegram alert transport requires HTTPS")
    request = urllib.request.Request(
        f"{base}/bot{os.environ['MONITOR_BOT_TOKEN']}/sendMessage",
        data=json.dumps({"chat_id": os.environ["MONITOR_CHAT_ID"], "text": message,
                         "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if json.loads(response.read(4096)).get("ok") is not True:
                raise RuntimeError("Telegram rejected alert")
    except Exception:
        raise RuntimeError("Telegram alert transport failed") from None


def report(incident: str) -> None:
    path = Path(os.environ.get("MONITOR_STATE_PATH", "/tmp/openlearn-monitor-state.json"))
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    previous = state.get("incident", "")
    now = time.time()
    changed = False
    if incident and (incident != previous or now - float(state.get("sent_at", 0)) >= 3600):
        notify("OpenLearn: аварийная проверка\n" + incident +
               "\nПроверьте Deploy и release_drain; неизвестные расходы не сбрасывать.")
        changed = True
    elif previous and not incident:
        notify("OpenLearn: внешние проверки процесса, БД, допуска и шлюза снова проходят. "
               "Результат релиза проверяйте отдельно в Deploy.")
        changed = True
    if changed:
        state = {"incident": incident, "sent_at": now}
    state["checked_at"] = now
    path.write_text(json.dumps(state))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as stream:
            stream.write(f"state_changed={str(changed).lower()}\n")


def check(base: str) -> list[str]:
    errors = []
    for path, status in (("/healthz", "ok"), ("/readyz", "ready"), ("/operationalz", "operational")):
        try:
            request = urllib.request.Request(base.rstrip("/") + path,
                                             headers={"User-Agent": "OpenLearn-production-monitor"})
            with urllib.request.urlopen(request, timeout=12) as response:
                if response.status != 200 or json.loads(response.read(4096)).get("status") != status:
                    errors.append(f"{path}: unexpected response")
        except Exception as exc:
            # URLs/response bodies can include secrets; diagnostics need only the failure type.
            errors.append(f"{path}: {type(exc).__name__}")
    return errors


def main() -> int:
    if not all(os.environ.get(key) for key in ("TELEGRAM_API_BASE_URL", "MONITOR_BOT_TOKEN", "MONITOR_CHAT_ID")):
        print("Telegram monitor configuration is incomplete")
        return 1
    if os.environ.get("MONITOR_TEST_ALERT") == "true":
        notify("OpenLearn: тест внешнего монитора из GitHub Actions. "
               "Доставка идёт напрямую в Telegram, без VPS и gw.open-learn.ai.")
    base = os.environ.get("PRODUCTION_MONITOR_BASE_URL", "https://open-learn.ai")
    errors = []
    for attempt in range(3):
        errors = check(base)
        if not errors:
            break
        print("; ".join(errors), flush=True)
        if attempt < 2:
            time.sleep(30)
    failed_deploy = os.environ.get("FAILED_DEPLOY_URL", "")
    if failed_deploy:
        print(f"Deploy failed: {failed_deploy}. Check admission and unresolved operations before resuming.")
    try:
        report("\n".join(errors + ([f"Deploy: {failed_deploy}"] if failed_deploy else [])))
    except Exception as exc:
        print(f"Alert delivery/state failed: {type(exc).__name__}")
        return 1
    failed = bool(errors or failed_deploy)
    print("Production incident detected" if failed else "Production probes passed", flush=True)
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
