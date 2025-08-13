# storage.py
import json, os, threading

PATH = "data/locations.json"
os.makedirs("data", exist_ok=True)
_lock = threading.Lock()

def _load():
    if not os.path.exists(PATH):
        return {}
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save(data):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def save_location(user_id: str, name: str, entry: dict):
    with _lock:
        data = _load()
        user = data.get(user_id, {})
        user[name] = entry
        data[user_id] = user
        _save(data)

def get_location(user_id: str, name: str = "home"):
    data = _load()
    return data.get(user_id, {}).get(name)

def list_locations(user_id: str):
    return list(_load().get(user_id, {}).keys())

def delete_location(user_id: str, name: str):
    with _lock:
        data = _load()
        if user_id in data and name in data[user_id]:
            del data[user_id][name]
            _save(data)
            return True
    return False
