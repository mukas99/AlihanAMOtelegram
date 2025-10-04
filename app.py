# app.py
import os, html, logging, requests, re
from flask import Flask, request, jsonify, abort
from dotenv import load_dotenv
import json
from typing import Dict, Any, List

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

WEBHOOK_SECRET = os.getenv("AMO_WEBHOOK_SECRET", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage" if TELEGRAM_BOT_TOKEN else None

# amoCRM API
AMO_BASE_URL = (os.getenv("AMO_BASE_URL") or "").rstrip("/")
AMO_ACCESS_TOKEN = os.getenv("AMO_ACCESS_TOKEN")
AMO_H = {"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"} if AMO_ACCESS_TOKEN else {}

# ID кастомного поля «День обучений»
CF_TRAINING_DAY_ID = os.getenv("CF_TRAINING_DAY_ID", "1057359")

# Читать карты полей из .env
def _load_json_env(name: str, default: dict):
    try:
        raw = os.getenv(name)
        return json.loads(raw) if raw else default
    except Exception:
        return default

CF_FIELDS: Dict[str, str] = _load_json_env("CF_FIELDS_JSON", {"training_day": "1057359"})
CONTACT_CF_FIELDS: Dict[str, str] = _load_json_env("CONTACT_CF_FIELDS_JSON", {"phone": "PHONE", "email": "EMAIL"})

def _norm(v):
    if v is None: return None
    if isinstance(v, (list, tuple)) and v: v = v[0]
    v = str(v).strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1]
    return v

def tg_send(text: str, parse_mode: str = "HTML"):
    if not (TG_API and TELEGRAM_CHAT_ID):
        app.logger.warning("Telegram env vars not set; skip sending")
        return
    limit = 4096
    for i in range(0, len(text), limit):
        chunk = text[i:i+limit]
        resp = requests.post(
            TG_API,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": parse_mode, "disable_web_page_preview": True},
            timeout=20
        )
        if resp.status_code >= 300:
            app.logger.error("Telegram send error: %s %s", resp.status_code, resp.text[:500])

def amo_get(path, params=None):
    # (если у вас уже есть эта функция — оставьте свою)
    if not (AMO_BASE_URL and AMO_ACCESS_TOKEN):
        return None
    url = path if path.startswith("http") else f"{AMO_BASE_URL}{path}"
    r = requests.get(url, headers={"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"}, params=params or {}, timeout=30)
    if r.status_code == 200:
        return r.json()
    app.logger.warning("amoGET %s -> %s %s", url, r.status_code, r.text[:200])
    return None

def parse_payload_from_request(req):
    """Собираем всё: JSON + form + query (нормализуем строки)."""
    payload = {}
    if req.is_json:
        j = req.get_json(silent=True) or {}
        if isinstance(j, dict): payload.update(j)
    if req.form:
        payload.update(req.form.to_dict(flat=True))
    if req.args:
        for k, v in req.args.to_dict(flat=True).items():
            payload.setdefault(k, v)
    return {k: _norm(v) for k, v in payload.items()}


def extract_lead_ids(payload: dict):
    ids = set()
    # формат leads[status][0][id]
    for k, v in payload.items():
        if re.match(r"^leads\[status\]\[\d+\]\[id\]$", k) and v:
            ids.add(str(v))
    # запасные варианты
    for key in ("lead_id", "id", "leadId"):
        if payload.get(key):
            ids.add(str(payload[key]))
    return list(ids)

def get_cf_value(entity: dict, field_id: str):
    """Достаёт значение кастомного поля по field_id из сущности (lead/contact)."""
    for cf in (entity.get("custom_fields_values") or []):
        if str(cf.get("field_id")) == str(field_id):
            vals = []
            for v in (cf.get("values") or []):
                if "value" in v and v["value"] is not None:
                    vals.append(v["value"])
                elif "enum_id" in v:  # на всякий случай
                    vals.append(v["enum_id"])
            if not vals:
                return None
            return vals[0] if len(vals) == 1 else vals
    return None

def fetch_contact_details(ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Подтягиваем контакты и вытягиваем нужные поля по CONTACT_CF_FIELDS."""
    if not ids:
        return {}
    data = amo_get("/api/v4/contacts", params={"filter[id]": ",".join(map(str, ids))}) or {}
    out = {}
    for c in (data.get("_embedded", {}) or {}).get("contacts", []):
        info = {"id": c.get("id"), "name": c.get("name")}
        # PHONE / EMAIL по field_code
        phones, emails = [], []
        for cf in (c.get("custom_fields_values") or []):
            code = cf.get("field_code")
            for v in (cf.get("values") or []):
                val = (v.get("value") or "").strip() if isinstance(v.get("value"), str) else v.get("value")
                if code == "PHONE" and val: phones.append(val)
                if code == "EMAIL" and val: emails.append(val)
        # кастомные поля контакта по id
        for key, fid in CONTACT_CF_FIELDS.items():
            if fid == "PHONE":
                info[key] = phones
            elif fid == "EMAIL":
                info[key] = emails
            else:
                info[key] = get_cf_value(c, fid)
        out[c["id"]] = info
    return out

@app.route("/webhooks/amocrm/stage", methods=["GET", "POST"])
def amocrm_stage_webhook():
    # секрет
    secret = request.args.get("secret") or request.headers.get("X-Webhook-Secret") or ""
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        abort(401, "bad secret")

    payload = {}  # как у вас было: собрать из JSON/form/query
    if request.is_json:
        payload.update(request.get_json(silent=True) or {})
    if request.form:
        payload.update(request.form.to_dict(flat=True))
    if request.args:
        for k, v in request.args.to_dict(flat=True).items():
            payload.setdefault(k, v)
    payload = {k: _norm(v) for k, v in payload.items()}

    lead_ids = extract_lead_ids(payload)

    enriched = []
    for lid in lead_ids:
        # берём сделку + контакты/компании
        lead = amo_get(f"/api/v4/leads/{lid}", params={"with": "contacts,companies"}) or {}
        if not lead:
            continue

        # 1) кастомные поля сделки по списку CF_FIELDS
        lead_cf = {}
        for key, fid in CF_FIELDS.items():
            lead_cf[key] = get_cf_value(lead, fid)

        # 2) контакты (основной + до 2-х следующих)
        contact_ids = [c.get("id") for c in (lead.get("_embedded", {}).get("contacts") or []) if c.get("id")]
        contacts_map = fetch_contact_details(contact_ids[:3]) if contact_ids else {}
        contacts_out = list(contacts_map.values())

        item = {
            "id": lid,
            "name": lead.get("name"),
            "price": lead.get("price"),
            "pipeline_id": lead.get("pipeline_id"),
            "status_id": lead.get("status_id"),
            "custom_fields": lead_cf,
            "contacts": contacts_out,
            "link": f"{AMO_BASE_URL}/leads/detail/{lid}" if AMO_BASE_URL else None
        }
        enriched.append(item)

        # === Telegram карточка ===
        lines = [
            f"✅ <b>Сделка</b> <code>{html.escape(str(lid))}</code>",
            f"<b>{html.escape(item['name'] or 'Без названия')}</b>",
            # f"Сумма: <b>{html.escape(str(item['price']))}</b>",
            # f"Pipeline: <code>{html.escape(str(item['pipeline_id']))}</code> | Status: <code>{html.escape(str(item['status_id']))}</code>",
        ]
        # добавим выбранные CF красиво
        for k, v in lead_cf.items():
            if isinstance(v, list):
                v = ", ".join(map(str, v))
            lines.append(f"{html.escape(k)}: <b>{html.escape(str(v or '—'))}</b>")
        # контакт (кратко)
        # if contacts_out:
        #     c = contacts_out[0]
        #     phones = ", ".join(c.get("phone", []) or c.get("phones", []) or [])
        #     emails = ", ".join(c.get("email", []) or c.get("emails", []) or [])
        #     lines.append(f"Контакт: <b>{html.escape(c.get('name') or '')}</b>"
        #                  f"{' | 📞 ' + html.escape(phones) if phones else ''}"
        #                  f"{' | ✉️ ' + html.escape(emails) if emails else ''}")
        if item["link"]:
            lines.append(html.escape(item["link"]))

        tg_send("\n".join(lines))
    # итоговый JSON (для отладки)
    final = {"ok": True, "webhook_minimal": payload, "leads_full": enriched}
    # tg_send(f"<b>JSON:</b>\n<pre>{html.escape(json.dumps(final, ensure_ascii=False, indent=2))}</pre>")
    return jsonify(final), 200


@app.get("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
