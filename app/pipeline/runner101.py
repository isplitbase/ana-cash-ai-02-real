import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import google.auth
import google.auth.transport.requests
from google.cloud import storage as gcs_storage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COLAB_SCRIPT = PROJECT_ROOT / "app" / "pipeline" / "originals" / "colab101.py"
OUTPUT_JSON = "output.json"
OUTPUT_UPDATED_JSON = "output_updated.json"
HTML_FILE = "report.html"
PRESIGNED_EXPIRES_SECONDS = 7 * 24 * 60 * 60


def _extract_port(payload: Any) -> str | None:
    try:
        if isinstance(payload, dict) and payload.get("port") is not None:
            v = str(payload["port"]).strip()
            return v or None
    except Exception:
        pass
    return None


def _run(cmd, cwd: Path, env: dict):
    p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"cmd={cmd}\n"
            f"returncode={p.returncode}\n"
            f"stdout:\n{p.stdout}\n"
            f"stderr:\n{p.stderr}\n"
        )
    return p


def _build_request_data_script_tag(payload: Any) -> str:
    data = []
    if isinstance(payload, dict):
        raw_data = payload.get("data")
        if isinstance(raw_data, list):
            data = raw_data

    json_for_html = json.dumps({"data": data}, ensure_ascii=False)
    json_for_html = (
        json_for_html
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    return "<script>\nwindow.__CASH_AI_SAVE_DATA__ = " + json_for_html + ";\n</script>"


def _patch_report_html_for_cloudrun(html_path: Path, port_value: str | None = None, request_payload: Any = None) -> None:
    html = html_path.read_text(encoding="utf-8")

    script_tag = _build_request_data_script_tag(request_payload)
    if "window.__CASH_AI_SAVE_DATA__" not in html:
        if "</head>" in html:
            html = html.replace("</head>", script_tag + "\n</head>", 1)
        elif "<body>" in html:
            html = html.replace("<body>", "<body>\n" + script_tag, 1)
        else:
            html = script_tag + "\n" + html

    if port_value is not None:
        js_port_literal = json.dumps(str(port_value))
        html, n_payload = re.subn(
            r"var\s+payload\s*=\s*\{\s*data:\s*window\.reportData\s*\|\|\s*\[\]\s*,\s*period_numbers:\s*window\._periodNumbers\s*\|\|\s*\{\}\s*\}\s*;",
            lambda _m: f"var payload = {{ data: window.reportData || [], period_numbers: window._periodNumbers || {{}}, port: {js_port_literal} }};",
            html,
            count=1,
        )
        if n_payload == 0:
            html, _ = re.subn(
                r"(var\s+payload\s*=\s*\{[^\}]*period_numbers\s*:\s*window\._periodNumbers\s*\|\|\s*\{\}\s*)(\}\s*;)",
                lambda _m: _m.group(1) + f",\n                port: {js_port_literal}\n            " + _m.group(2),
                html,
                count=1,
                flags=re.DOTALL,
            )

    pattern = re.compile(
        r"function\s+safeInvokeSave\s*\(\s*payload\s*\)\s*\{.*?\n\}\n\nfunction\s+showSimpleModal",
        re.DOTALL,
    )

    replacement = '''function safeInvokeSave(payload){
      try{
        const port = (payload && typeof payload === "object" && payload.port !== undefined && payload.port !== null && String(payload.port).trim() !== "")
          ? String(payload.port).trim()
          : ((window.CASH_AI_PORT !== undefined && window.CASH_AI_PORT !== null && String(window.CASH_AI_PORT).trim() !== "")
              ? String(window.CASH_AI_PORT).trim()
              : "[port]");
        if (port.indexOf("[port]") !== -1){
          showSimpleModal("保存先ポートが未設定です（CASH_AI_PORT を設定してください）");
          return Promise.resolve(null);
        }

        const url = "https://z-lite.aitask.biz:" + port + "/sapis/cash_ai_03.php";
        const dataToSend = (payload && typeof payload === "object" && Array.isArray(payload.data))
          ? payload.data
          : payload;

        return fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(dataToSend)
        }).then(async (r) => {
          let data = null;
          let text = "";
          try {
            data = await r.json();
          } catch (_e) {
            try { text = await r.text(); } catch (__e) {}
          }

          if (!r.ok){
            showSimpleModal("保存できませんでした");
            return { ok: false, status: r.status, data, text };
          }

          showSimpleModal("保存できました");
          return data || { ok: true, text };
        }).catch(err => {
          console.error(err);
          showSimpleModal("保存できませんでした");
          return null;
        });

      }catch(e){
        console.error(e);
        showSimpleModal("保存できませんでした");
        return Promise.resolve(null);
      }
    }

    function showSimpleModal'''

    new_html, n = pattern.subn(replacement, html, count=1)
    if n == 0:
        override = '''
<script>
(function(){
  window.safeInvokeSave = function(payload){
    try{
      const port = (payload && typeof payload === "object" && payload.port !== undefined && payload.port !== null && String(payload.port).trim() !== "")
        ? String(payload.port).trim()
        : ((window.CASH_AI_PORT !== undefined && window.CASH_AI_PORT !== null && String(window.CASH_AI_PORT).trim() !== "")
            ? String(window.CASH_AI_PORT).trim()
            : "[port]");
      if (port.indexOf("[port]") !== -1){
        window.showSimpleModal && window.showSimpleModal("保存先ポートが未設定です（CASH_AI_PORT を設定してください）");
        return Promise.resolve(null);
      }
      const url = "https://z-lite.aitask.biz:" + port + "/sapis/cash_ai_03.php";
      const dataToSend = (payload && typeof payload === "object" && Array.isArray(payload.data))
        ? payload.data
        : payload;
      return fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(dataToSend)
      }).then(r => r.json().catch(() => r.text().then(t => ({text: t}))))
        .catch(err => {
          console.error(err);
          window.showSimpleModal && window.showSimpleModal("保存できませんでした");
          return null;
        });
    }catch(e){
      console.error(e);
      window.showSimpleModal && window.showSimpleModal("保存できませんでした");
      return Promise.resolve(null);
    }
  };
})();
</script>
'''
        new_html = html.replace("</body>", override + "\n</body>") if "</body>" in html else html + "\n" + override

    html_path.write_text(new_html, encoding="utf-8")


def _upload_html_and_presign(html_path: Path) -> str:
    bucket = os.getenv("GCS_BUCKET")
    if not bucket:
        raise RuntimeError("GCS_BUCKET が未設定です（例: zlite）")

    prefix = os.getenv("GCS_PREFIX", "cash-ai-02/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{prefix}{ts}_{uuid.uuid4().hex}.html"

    # ADC で認証情報を取得し、アクセストークンを更新する
    # Cloud Run では Compute Engine credentials が返る（秘密鍵なし）
    # generate_signed_url には service_account_email + access_token を渡すことで
    # IAM SignBlob API 経由で署名させる
    credentials, project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)

    client = gcs_storage.Client(credentials=credentials, project=project)
    blob = client.bucket(bucket).blob(key)
    blob.upload_from_filename(
        str(html_path),
        content_type="text/html; charset=utf-8",
    )

    # service_account_email + access_token を使って IAM 経由で署名付きURL生成
    signed_url = blob.generate_signed_url(
        expiration=timedelta(seconds=PRESIGNED_EXPIRES_SECONDS),
        method="GET",
        version="v4",
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )
    return signed_url


def run_colab101(payload: Any) -> Dict[str, Any]:
    run_dir = Path(tempfile.mkdtemp(prefix="cashai02_", dir="/tmp"))

    try:
        (run_dir / OUTPUT_JSON).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["NO_HTML"] = "0"
        env["DISABLE_EXCEL"] = "1"
        env["HTML_OUTPUT_PATH"] = HTML_FILE

        _run(["python3", str(COLAB_SCRIPT)], cwd=run_dir, env=env)

        out_path = run_dir / OUTPUT_UPDATED_JSON
        if not out_path.exists():
            raise RuntimeError("output_updated.json が生成されませんでした。")
        data = json.loads(out_path.read_text(encoding="utf-8"))

        html_path = run_dir / HTML_FILE
        if not html_path.exists():
            raise RuntimeError("report.html が生成されませんでした。")

        _patch_report_html_for_cloudrun(html_path, port_value=_extract_port(payload), request_payload=payload)
        html_url = _upload_html_and_presign(html_path)

        return {"html": html_url, "data": data}

    finally:
        if os.getenv("DEBUG_KEEP_TMP", "0") != "1":
            shutil.rmtree(run_dir, ignore_errors=True)
