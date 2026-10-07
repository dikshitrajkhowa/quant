"""
Megabull paper-trading API client  (https://megabull.in/paper-trading-api-india.html)

- Base URL https://api.megabull.in, auth header `api-key: <key>`.
- The key lives in app1/.env as MEGABULL_API_KEY=... (keys expire after 1 month; regenerate
  from Profile at trade.megabull.in and update .env - the Trade page can reload it without a restart).
- All orders are virtual money.

Megabull publishes the request schemas only in its OpenAPI spec, so the client downloads the spec
(with your key), caches it in app1/data/, and maps our order fields (side, quantity, order type,
product, instrument...) onto the spec's field names and enum values. The mapping is shown on the
Trade page; anything it gets wrong can be overridden in app1/megabull_mapping.json, e.g.
    {"fields": {"side": "buySell"}, "values": {"side": {"BUY": "B", "SELL": "S"}}}

CLI:
    python megabull.py --inspect          # download spec, print order schema + resolved mapping
    python megabull.py --profile          # check the key works
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("megabull")

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
DATA_DIR = HERE / "data"
SPEC_CACHE = DATA_DIR / "megabull-openapi.json"
MAPPING_FILE = HERE / "megabull_mapping.json"
SPEC_URLS = ["https://api.megabull.in/api/megabull-openapi.json", "https://megabull.in/api/megabull-openapi.json"]

ORDER_PATH = "/api/order/buysell"


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #
def load_env(path: Path = ENV_FILE, override: bool = True) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE, # comments, optional quotes). No extra dependency."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        values[k] = v
        if override or k not in os.environ:
            os.environ[k] = v
    return values


def ensure_env_file() -> None:
    """Create app1/.env with an empty key placeholder if it doesn't exist (never overwrites)."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text(
            "# Megabull paper-trading API key - generate it at https://trade.megabull.in (Profile -> API key).\n"
            "# Keys expire after 1 month. This file is git-ignored; never commit it.\n"
            "MEGABULL_API_KEY=\n"
            "# Optional: MEGABULL_BASE_URL=https://api.megabull.in\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class MegabullError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: Any = None):
        super().__init__(msg)
        self.status, self.body = status, body


class MegabullAuthError(MegabullError):
    pass


# --------------------------------------------------------------------------- #
# Helpers to read loosely-specified JSON
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def pick(d: Any, candidates: list[str], default: Any = None) -> Any:
    """First matching key (case/underscore-insensitive) in a dict."""
    if not isinstance(d, dict):
        return default
    nk = {_norm(k): k for k in d}
    for c in candidates:
        if _norm(c) in nk and d[nk[_norm(c)]] not in (None, ""):
            return d[nk[_norm(c)]]
    return default


def unwrap_list(payload: Any) -> list:
    """Responses may be a list or {data: [...]} / {result: {...: [...]}} - find the list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "result", "results", "items", "orders", "positions", "holdings", "instruments", "list"):
            if k in payload:
                inner = unwrap_list(payload[k])
                if inner:
                    return inner
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def find_key(payload: Any, candidates: list[str]) -> Any:
    """Depth-first search for the first key matching any candidate."""
    if isinstance(payload, dict):
        v = pick(payload, candidates)
        if v is not None:
            return v
        for val in payload.values():
            r = find_key(val, candidates)
            if r is not None:
                return r
    elif isinstance(payload, list):
        for val in payload:
            r = find_key(val, candidates)
            if r is not None:
                return r
    return None


# --------------------------------------------------------------------------- #
# Order field mapping (derived from the OpenAPI spec)
# --------------------------------------------------------------------------- #
FIELD_CANDIDATES: dict[str, list[str]] = {
    "instrument_id": ["instrumentId", "instrument_id", "instrumentToken", "instrument_token", "token", "securityId",
                      "security_id", "scripCode", "scripId", "symbolToken", "symbolId", "instrument", "marketWatchId"],
    "symbol": ["tradingSymbol", "tradingsymbol", "symbol", "scripName", "stockName", "stock", "name"],
    "exchange": ["exchange", "exchangeSegment", "exch", "segment", "market"],
    "side": ["transactionType", "transaction_type", "side", "buySell", "buy_sell", "orderSide", "action", "tradeType"],
    "quantity": ["quantity", "qty", "orderQty", "orderQuantity", "lots", "noOfShares"],
    "order_type": ["orderType", "order_type", "priceType", "price_type", "type"],
    "price": ["price", "limitPrice", "orderPrice", "rate"],
    "trigger_price": ["triggerPrice", "trigger_price", "stopPrice", "slPrice", "trigger"],
    "product": ["productType", "product", "product_type", "orderProduct", "productCode", "tradeMode"],
    "validity": ["validity", "timeInForce", "duration", "orderValidity"],
}
VALUE_CANDIDATES: dict[str, dict[str, list[str]]] = {
    "side": {"BUY": ["BUY", "B", "LONG", "1"], "SELL": ["SELL", "S", "SHORT", "-1", "2"]},
    "order_type": {"MARKET": ["MARKET", "MKT", "M"], "LIMIT": ["LIMIT", "LMT", "L"],
                   "SL": ["SL", "STOPLOSS", "STOP_LOSS", "SL_LIMIT", "SLL", "STOPLIMIT"],
                   "SLM": ["SLM", "SL-M", "SL_M", "STOPLOSSMARKET", "SL_MARKET", "STOPMARKET", "STOP"]},
    "product": {"MIS": ["MIS", "INTRADAY", "I", "INTRA"], "CNC": ["CNC", "DELIVERY", "D", "NRML"]},
    "validity": {"DAY": ["DAY", "D"]},
    "exchange": {"NSE": ["NSE", "NSE_EQ", "NSEEQ", "NSECM", "N"], "BSE": ["BSE", "BSE_EQ", "BSEEQ", "BSECM", "B"]},
}


def _resolve(spec: dict, node: Any, depth: int = 0) -> Any:
    if depth > 20 or not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"].lstrip("#/").split("/")
        target: Any = spec
        for part in ref:
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _resolve(spec, target, depth + 1)
    if "allOf" in node:
        merged: dict = {"type": "object", "properties": {}, "required": []}
        for sub in node["allOf"]:
            s = _resolve(spec, sub, depth + 1)
            merged["properties"].update(s.get("properties", {}))
            merged["required"] += s.get("required", [])
        return merged
    for key in ("oneOf", "anyOf"):
        if key in node and node[key]:
            return _resolve(spec, node[key][0], depth + 1)
    return node


def order_schema(spec: dict) -> dict:
    """The JSON body schema of POST /api/order/buysell."""
    op = spec.get("paths", {}).get(ORDER_PATH, {}).get("post", {})
    content = _resolve(spec, op.get("requestBody", {})).get("content", {})
    media = content.get("application/json") or next(iter(content.values()), {})
    return _resolve(spec, media.get("schema", {}))


@dataclass
class OrderMapping:
    fields: dict[str, str] = field(default_factory=dict)           # canonical -> API field name
    values: dict[str, dict[str, Any]] = field(default_factory=dict)  # canonical -> {our value: API value}
    required: list[str] = field(default_factory=list)
    unmapped_required: list[str] = field(default_factory=list)
    properties: dict[str, dict] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)   # constant fields always sent (from megabull_mapping.json)
    source: str = "defaults"

    def report(self) -> dict:
        return {"source": self.source, "fields": self.fields, "values": self.values,
                "required": self.required, "unmapped_required": self.unmapped_required,
                "api_fields": {k: {kk: vv for kk, vv in v.items() if kk in ("type", "enum", "format", "description")}
                               for k, v in self.properties.items()}}


def _match_enum(enum: list, candidates: list[str]) -> Any:
    norm = {_norm(e): e for e in enum}
    for c in candidates:
        if _norm(c) in norm:
            return norm[_norm(c)]
    return None


def build_mapping(spec: dict | None) -> OrderMapping:
    m = OrderMapping()
    schema = order_schema(spec) if spec else {}
    props = {k: _resolve(spec, v) for k, v in schema.get("properties", {}).items()} if spec else {}
    if props:
        m.source, m.properties, m.required = "openapi", props, list(schema.get("required", []))
        used: set[str] = set()
        for canon, cands in FIELD_CANDIDATES.items():
            for c in cands:
                hit = next((p for p in props if _norm(p) == _norm(c) and p not in used), None)
                if hit:
                    # "type" is ambiguous: only accept it for order_type if its enum looks like order types
                    if _norm(hit) == "type" and canon == "order_type":
                        enum = props[hit].get("enum") or []
                        if enum and not _match_enum(enum, VALUE_CANDIDATES["order_type"]["MARKET"]):
                            continue
                    m.fields[canon] = hit
                    used.add(hit)
                    break
        for canon, wanted in VALUE_CANDIDATES.items():
            fname = m.fields.get(canon)
            enum = props.get(fname, {}).get("enum") if fname else None
            if enum:
                m.values[canon] = {ours: _match_enum(enum, cands) for ours, cands in wanted.items()
                                   if _match_enum(enum, cands) is not None}
        m.unmapped_required = [r for r in m.required if r not in m.fields.values()]
    else:  # no spec available: conventional Indian-broker field names
        m.fields = {"symbol": "tradingSymbol", "exchange": "exchange", "side": "transactionType",
                    "quantity": "quantity", "order_type": "orderType", "price": "price",
                    "trigger_price": "triggerPrice", "product": "productType", "validity": "validity"}
    # manual overrides
    if MAPPING_FILE.exists():
        try:
            ov = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
            m.fields.update(ov.get("fields", {}))
            for k, v in ov.get("values", {}).items():
                m.values.setdefault(k, {}).update(v)
            m.extra = ov.get("extra", {})  # constant fields to always send
            m.source += " + megabull_mapping.json"
        except Exception as exc:
            log.warning("Ignoring invalid %s: %s", MAPPING_FILE.name, exc)
    return m


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
@dataclass
class Order:
    ticker: str                 # RELIANCE.NS
    side: str                   # BUY | SELL
    quantity: int
    order_type: str = "MARKET"  # MARKET | LIMIT | SL | SLM
    price: float | None = None
    trigger_price: float | None = None
    product: str = "MIS"        # MIS (intraday) | CNC


class MegabullClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 15.0):
        self.api_key = api_key if api_key is not None else os.getenv("MEGABULL_API_KEY", "")
        self.base_url = (base_url or os.getenv("MEGABULL_BASE_URL") or "https://api.megabull.in").rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._spec: dict | None = None
        self._mapping: OrderMapping | None = None
        self._instruments: tuple[float, list[dict]] | None = None
        self.last_exchange: dict = {}   # last raw request/response, shown on the Trade page

    # ---- plumbing ----
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        if not self.api_key:
            raise MegabullAuthError("No Megabull API key. Put MEGABULL_API_KEY=... in app1/.env")
        url = self.base_url + path
        try:
            r = self.session.request(method, url, json=body, timeout=self.timeout,
                                     headers={"api-key": self.api_key, "Accept": "application/json"})
        except requests.RequestException as exc:
            raise MegabullError(f"Network error calling Megabull: {exc}") from exc
        try:
            data = r.json()
        except ValueError:
            data = r.text
        self.last_exchange = {"method": method, "path": path, "request": body, "status": r.status_code,
                              "response": data, "time": time.strftime("%H:%M:%S")}
        if r.status_code in (401, 403):
            raise MegabullAuthError("Megabull rejected the API key (expired or invalid). Keys last 1 month - "
                                    "generate a new one under Profile at trade.megabull.in and update app1/.env.",
                                    r.status_code, data)
        if r.status_code >= 400:
            msg = find_key(data, ["message", "error", "detail", "msg"]) or r.reason
            raise MegabullError(f"Megabull {method} {path} failed ({r.status_code}): {msg}", r.status_code, data)
        return data

    # ---- schema ----
    def spec(self, refresh: bool = False) -> dict | None:
        if self._spec is not None and not refresh:
            return self._spec
        if SPEC_CACHE.exists() and not refresh:
            try:
                self._spec = json.loads(SPEC_CACHE.read_text(encoding="utf-8"))
                return self._spec
            except ValueError:
                pass
        for url in [self.base_url + "/api/megabull-openapi.json"] + [u for u in SPEC_URLS if not u.startswith(self.base_url)]:
            try:
                r = self.session.get(url, timeout=self.timeout, headers={"api-key": self.api_key} if self.api_key else {})
                if r.ok:
                    self._spec = r.json()
                    DATA_DIR.mkdir(exist_ok=True)
                    SPEC_CACHE.write_text(json.dumps(self._spec, indent=1), encoding="utf-8")
                    return self._spec
            except Exception as exc:
                log.debug("spec fetch failed from %s: %s", url, exc)
        return None

    def mapping(self, refresh: bool = False) -> OrderMapping:
        if self._mapping is None or refresh:
            self._mapping = build_mapping(self.spec(refresh=refresh))
        return self._mapping

    # ---- account ----
    def profile(self) -> Any:
        return self._req("GET", "/api/user/my")

    def orders(self) -> list:
        return unwrap_list(self._req("GET", "/api/order/my"))

    def positions(self) -> list:
        return unwrap_list(self._req("GET", "/api/position/my"))

    def holdings(self) -> list:
        return unwrap_list(self._req("GET", "/api/holding/my"))

    # ---- instruments ----
    def instruments(self, refresh: bool = False) -> list[dict]:
        if self._instruments and not refresh and time.time() - self._instruments[0] < 6 * 3600:
            return self._instruments[1]
        rows = unwrap_list(self._req("GET", "/api/marketwatch/instruments"))
        self._instruments = (time.time(), rows)
        return rows

    def find_instrument(self, ticker: str) -> dict | None:
        base, _, suffix = ticker.upper().rpartition(".")
        base = base or ticker.upper()
        exch = {"NS": "NSE", "BO": "BSE"}.get(suffix, "NSE")
        try:
            rows = self.instruments()
        except MegabullError:
            return None
        best = None
        for r in rows:
            sym = str(pick(r, FIELD_CANDIDATES["symbol"], "")).upper()
            if sym not in (base, f"{base}-EQ", f"{base}.NS", f"{base}.BO"):
                continue
            ex = str(pick(r, FIELD_CANDIDATES["exchange"], "")).upper()
            if ex and exch not in ex:
                continue
            seg = _norm(pick(r, ["segment", "instrumentType", "series"], ""))
            if seg and any(x in seg for x in ("fut", "opt", "ce", "pe")):
                continue
            best = r
            if sym in (base, f"{base}-EQ"):
                break
        return best

    # ---- orders ----
    def build_payload(self, o: Order) -> dict:
        m = self.mapping()
        base, _, suffix = o.ticker.upper().rpartition(".")
        base = base or o.ticker.upper()
        exch = {"NS": "NSE", "BO": "BSE"}.get(suffix, "NSE")
        canon: dict[str, Any] = {
            "symbol": base, "exchange": exch, "side": o.side, "quantity": int(o.quantity),
            "order_type": o.order_type, "product": o.product, "validity": "DAY",
            "price": o.price if o.price is not None else (0 if o.order_type == "MARKET" else None),
            "trigger_price": o.trigger_price,
        }
        if "instrument_id" in m.fields:
            inst = self.find_instrument(o.ticker)
            if inst is None:
                raise MegabullError(f"{base} not found in Megabull's instrument list")
            fname = m.fields["instrument_id"]
            canon["instrument_id"] = pick(inst, [fname] + FIELD_CANDIDATES["instrument_id"] + ["id", "_id"])
            # some APIs want the exact symbol string from their master
            canon["symbol"] = pick(inst, FIELD_CANDIDATES["symbol"], base)
        payload: dict[str, Any] = {}
        for canon_key, api_field in m.fields.items():
            v = canon.get(canon_key)
            if v is None:
                continue
            v = m.values.get(canon_key, {}).get(v, v)
            prop = m.properties.get(api_field, {})
            if prop.get("type") in ("number", "integer") and isinstance(v, str):
                try:
                    v = float(v) if prop["type"] == "number" else int(v)
                except ValueError:
                    pass
            payload[api_field] = v
        payload.update(getattr(m, "extra", {}) or {})
        return payload

    def place_order(self, o: Order) -> dict:
        payload = self.build_payload(o)
        with self._lock:
            resp = self._req("POST", ORDER_PATH, payload)
        oid = find_key(resp, ["orderId", "order_id", "orderNo", "orderNumber", "id", "_id"])
        status = find_key(resp, ["status", "orderStatus"])
        return {"order_id": oid, "status": status, "request": payload, "response": resp}

    def cancel_orders(self, order_ids: list) -> Any:
        """Body shape comes from the spec: a bare array, or an object with one array property."""
        body: Any = {"orderIds": order_ids}
        spec = self.spec()
        if spec:
            op = spec.get("paths", {}).get("/api/order/bulk/cancel", {}).get("put", {})
            content = _resolve(spec, op.get("requestBody", {})).get("content", {})
            sch = _resolve(spec, (content.get("application/json") or next(iter(content.values()), {})).get("schema", {}))
            if sch.get("type") == "array":
                body = order_ids
            else:
                arr = [k for k, v in sch.get("properties", {}).items() if _resolve(spec, v).get("type") == "array"]
                if arr:
                    body = {arr[0]: order_ids}
        return self._req("PUT", "/api/order/bulk/cancel", body)

    def order_fill(self, order_id: Any) -> dict | None:
        """Look up an order in the order book; returns {status, avg_price, qty} when found."""
        if order_id is None:
            return None
        for o in self.orders():
            if str(pick(o, ["orderId", "order_id", "orderNo", "id", "_id"])) == str(order_id):
                return {"status": str(pick(o, ["status", "orderStatus"], "")).upper(),
                        "avg_price": pick(o, ["averagePrice", "avgPrice", "avg_price", "tradedPrice",
                                              "executedPrice", "fillPrice", "price"]),
                        "qty": pick(o, ["filledQuantity", "filledQty", "tradedQty", "quantity", "qty"]),
                        "raw": o}
        return None

    def ltp_map(self) -> dict[str, float]:
        """Last traded prices from open positions, keyed by base symbol (e.g. RELIANCE)."""
        out = {}
        for p in self.positions():
            sym = str(pick(p, FIELD_CANDIDATES["symbol"], "")).upper().replace("-EQ", "")
            ltp = pick(p, ["ltp", "lastPrice", "last_price", "lastTradedPrice", "currentPrice", "cmp", "marketPrice"])
            if sym and ltp is not None:
                try:
                    out[sym] = float(ltp)
                except (TypeError, ValueError):
                    pass
        return out


def summarize_profile(p: Any) -> dict:
    return {"name": find_key(p, ["name", "fullName", "userName", "username", "firstName", "email"]),
            "balance": find_key(p, ["virtualMoney", "virtual_money", "balance", "availableBalance", "funds",
                                    "availableMargin", "cash", "virtualBalance"]),
            "raw": p}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Megabull paper-trading API helper")
    ap.add_argument("--inspect", action="store_true", help="download the OpenAPI spec and show the order mapping")
    ap.add_argument("--profile", action="store_true", help="call /api/user/my to check the key")
    ap.add_argument("--payload", metavar="TICKER", help="show the order body that would be sent (nothing is placed)")
    a = ap.parse_args()
    ensure_env_file()
    load_env()
    c = MegabullClient()
    if not c.configured:
        raise SystemExit(f"Set MEGABULL_API_KEY in {ENV_FILE}")
    if a.profile:
        print(json.dumps(summarize_profile(c.profile()), indent=2, default=str))
    if a.inspect:
        spec = c.spec(refresh=True)
        print("spec:", "downloaded -> " + str(SPEC_CACHE) if spec else "NOT available (using default field names)")
        if spec:
            print("order body schema:", json.dumps(order_schema(spec), indent=2)[:4000])
        print("resolved mapping:", json.dumps(c.mapping(refresh=True).report(), indent=2, default=str)[:4000])
    if a.payload:
        print(json.dumps(c.build_payload(Order(a.payload if "." in a.payload else a.payload + ".NS", "BUY", 1)), indent=2))
