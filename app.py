import json
import os
import re
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify
from stock_data import analyze_stock
from market_data  import get_market_overview
from options_data import get_options_stats, get_options_watchlist, get_dynamic_watchlist

TW_TZ = ZoneInfo("Asia/Taipei")

# ── Simple in-memory cache ────────────────────────────────────────────────────

class SimpleCache:
    """Thread-safe TTL cache. Keys are strings; values are any JSON-serialisable object."""

    def __init__(self):
        self._store = {}
        self._lock  = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry and time.time() - entry["ts"] < entry["ttl"]:
                return entry["data"]
            return None

    def set(self, key: str, data, ttl: int = 60):
        with self._lock:
            self._store[key] = {"data": data, "ts": time.time(), "ttl": ttl}

    def delete(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def get_or_fetch(self, key: str, fetch_fn, ttl: int = 60):
        cached = self.get(key)
        if cached is not None:
            return cached
        data = fetch_fn()
        self.set(key, data, ttl)
        return data

    def purge_expired(self):
        with self._lock:
            now = time.time()
            expired = [k for k, v in self._store.items() if now - v["ts"] >= v["ttl"]]
            for k in expired:
                del self._store[k]
        return len(expired)

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            live    = sum(1 for v in self._store.values() if now - v["ts"] < v["ttl"])
            expired = len(self._store) - live
            return {"live": live, "expired": expired, "total": len(self._store)}

_cache = SimpleCache()

# TTL constants (seconds)
TTL_US_STOCK   = 60    # Alpaca price ~real-time; fundamentals stable for 1 min
TTL_TW_STOCK   = 120   # Yahoo already 15-min delayed; 2 min cache adds little
TTL_MARKET_US  = 60    # Sectors/gainers refresh every minute
TTL_MARKET_TW  = 120   # TW batch download is slow; 2 min is fine
TTL_OPTIONS    = 60    # Options chain changes fast; cap at 1 min

_TW_SYM = re.compile(r'^\d{4,6}[A-Za-z]{0,2}(\.TW[O]?)?$', re.IGNORECASE)

def sort_results(results: list) -> list:
    """Taiwan stocks sorted numerically, US stocks sorted alphabetically.
       Errored rows are kept at the end in original order."""
    tw, us, errors = [], [], []
    for r in results:
        if r.get("error"):
            errors.append(r)
        elif r.get("is_taiwan") or _TW_SYM.match(r.get("display_symbol") or r.get("symbol", "")):
            tw.append(r)
        else:
            us.append(r)

    def tw_key(r):
        sym = r.get("display_symbol") or r.get("symbol", "")
        m = re.match(r"(\d+)", sym)
        return int(m.group(1)) if m else 0

    tw.sort(key=tw_key)
    us.sort(key=lambda r: (r.get("display_symbol") or r.get("symbol", "")).upper())
    return tw + us + errors

_here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            static_folder=os.path.join(_here, "static"),
            template_folder=os.path.join(_here, "templates"))

# DATA_DIR can be overridden via env var to point at a persistent volume on cloud
_data_dir = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
PROFILES_FILE = os.path.join(_data_dir, "profiles.json")


def load_profiles() -> dict:
    if not os.path.exists(PROFILES_FILE):
        return {}
    with open(PROFILES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_profiles(profiles: dict) -> None:
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)


# ── pages ────────────────────────────────────────────────────────────────────

def _is_mobile():
    ua = request.user_agent.string.lower()
    return any(k in ua for k in ["android", "iphone", "ipad", "mobile", "tablet"])

@app.route("/")
def index():
    if _is_mobile():
        return render_template("mobile.html")
    return render_template("index.html")

@app.route("/m")
def mobile():
    return render_template("mobile.html")

@app.route("/desktop")
def desktop():
    return render_template("index.html")

@app.route("/market")
def market_page():
    if _is_mobile():
        return render_template("market_mobile.html")
    return render_template("market.html")

@app.route("/options")
def options_page():
    if _is_mobile():
        return render_template("options_mobile.html")
    return render_template("options.html")

@app.route("/options/data")
def options_data_api():
    raw     = request.args.get("symbols", "").strip()
    symbols = [s.strip().upper() for s in raw.replace(",", " ").split() if s.strip()]
    if not symbols:
        symbols = get_dynamic_watchlist(30)
    symbols = symbols[:30]
    _cache.purge_expired()   # lazy cleanup on each options request
    results = [
        _cache.get_or_fetch(f"opts:{s}", lambda s=s: get_options_stats(s), TTL_OPTIONS)
        for s in symbols
    ]
    return jsonify({"results": results, "count": len(results)})

@app.route("/options/single")
def options_single():
    sym = request.args.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    result = _cache.get_or_fetch(f"opts:{sym}", lambda: get_options_stats(sym), TTL_OPTIONS)
    return jsonify(result)

@app.route("/market/data")
def market_data_api():
    market = request.args.get("market", "US").upper()
    if market not in ("US", "TW"):
        return jsonify({"error": "market must be US or TW"}), 400
    ttl = TTL_MARKET_US if market == "US" else TTL_MARKET_TW
    data = _cache.get_or_fetch(
        f"market:{market}",
        lambda: get_market_overview(market),
        ttl
    )
    return jsonify(data)


# ── ad-hoc stock lookup ───────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    symbols_raw = data.get("symbols", "")
    symbols = [s.strip() for s in symbols_raw.replace(",", " ").split() if s.strip()]
    if not symbols:
        return jsonify({"error": "請輸入至少一個股票代碼。"})

    def _fetch(sym):
        is_tw = bool(_TW_SYM.match(sym))
        ttl   = TTL_TW_STOCK if is_tw else TTL_US_STOCK
        return _cache.get_or_fetch(f"stock:{sym}", lambda: analyze_stock(sym), ttl)

    results = sort_results([_fetch(s) for s in symbols[:10]])
    return jsonify({"results": results})


# ── profiles CRUD ─────────────────────────────────────────────────────────────

@app.route("/profiles", methods=["GET"])
def list_profiles():
    profiles = load_profiles()
    summary = {
        name: {
            "stocks": p.get("stocks", []),
            "last_sync": p.get("last_sync"),
        }
        for name, p in profiles.items()
    }
    return jsonify(summary)


@app.route("/profiles", methods=["POST"])
def create_profile():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    stocks = [s.strip().upper() for s in data.get("stocks", []) if s.strip()]
    if not name:
        return jsonify({"error": "請輸入投資組合名稱"}), 400
    profiles = load_profiles()
    if name in profiles:
        return jsonify({"error": f"已存在名為「{name}」的投資組合"}), 400
    profiles[name] = {"stocks": stocks, "last_sync": None, "cache": []}
    save_profiles(profiles)
    return jsonify({"ok": True, "name": name})


@app.route("/profiles/<name>", methods=["GET"])
def get_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404
    p = profiles[name]
    return jsonify({
        "name": name,
        "stocks": p.get("stocks", []),
        "last_sync": p.get("last_sync"),
        "cache": p.get("cache", []),
    })


@app.route("/profiles/<name>", methods=["PUT"])
def update_profile(name):
    data = request.get_json()
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404

    new_name = (data.get("new_name") or "").strip()
    stocks   = data.get("stocks")

    if new_name and new_name != name:
        if new_name in profiles:
            return jsonify({"error": f"已存在名為「{new_name}」的投資組合"}), 400
        profiles[new_name] = profiles.pop(name)
        name = new_name

    if stocks is not None:
        profiles[name]["stocks"] = [s.strip().upper() for s in stocks if s.strip()]

    save_profiles(profiles)
    return jsonify({"ok": True, "name": name})


@app.route("/profiles/<name>", methods=["DELETE"])
def delete_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404
    del profiles[name]
    save_profiles(profiles)
    return jsonify({"ok": True})


@app.route("/profiles/<name>/sync", methods=["POST"])
def sync_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404
    stocks = profiles[name].get("stocks", [])
    if not stocks:
        return jsonify({"error": "此投資組合沒有股票，請先加入股票"}), 400
    # Bust stock cache for each symbol so fresh data is fetched
    for s in stocks:
        _cache.delete(f"stock:{s.upper()}")
    results = sort_results([analyze_stock(s) for s in stocks])
    profiles[name]["last_sync"] = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    profiles[name]["cache"] = results
    save_profiles(profiles)
    return jsonify({"results": results, "last_sync": profiles[name]["last_sync"]})


@app.route("/cache/stats")
def cache_stats():
    purged = _cache.purge_expired()
    return jsonify({**_cache.stats(), "just_purged": purged})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
