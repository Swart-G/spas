"""Run against the deployed HTTP pipeline: python -m apps.mvp.demo."""

import argparse
import json
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    args = parser.parse_args()

    def post(path, body):
        request = urllib.request.Request(
            args.url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    incident = post("/incidents", {})
    result = post(f"/incidents/{incident['incident_id']}/process", {})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "resolved" or not result["verification"]["resolved"]:
        raise SystemExit(1)
    if [item["stage"] for item in result["history"]] != [
        "triage",
        "diagnosis",
        "remediation",
        "verification",
    ]:
        raise SystemExit("Incomplete pipeline")


if __name__ == "__main__":
    main()
