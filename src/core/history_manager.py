import json
import os
import logging
import time
from src.utils.paths import get_base_path

class HistoryManager:
    def __init__(self, filename="smart_history.json"):
        self.filename = os.path.join(get_base_path(), filename)
        self.history = self._load_history()
        
    def _load_history(self):
        if not os.path.exists(self.filename):
            return {}
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load history: {e}")
            return {}
            
    def save_history(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.error(f"Failed to save history: {e}")

    def update_disk_history(self, serial, attributes):
        """
        Update the history for a specific disk.
        attributes: list of SmartAttribute objects
        """
        if not serial:
            return
            
        now = time.time()
        raw_attrs = {str(attr.id): attr.raw for attr in attributes}
        
        # Check existing
        existing = self.history.get(serial, {})
        
        if not existing:
            # First time seeing this disk
            new_entry = {
                "first_seen": now,
                "last_check": now,
                "attributes": raw_attrs
            }
        else:
            # Update existing
            new_entry = existing
            new_entry["last_check"] = now
            new_entry["attributes"] = raw_attrs # Update to latest for next comparison
            if "first_seen" not in new_entry:
                 new_entry["first_seen"] = now

        self.history[serial] = new_entry
        self.save_history()
        
    def get_disk_history(self, serial):
        return self.history.get(serial)

    def get_attribute_history(self, serial, attr_id):
        disk_hist = self.history.get(serial)
        if disk_hist:
            return disk_hist.get("attributes", {}).get(str(attr_id))
        return None
