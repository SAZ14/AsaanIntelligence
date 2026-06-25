from pathlib import Path
from app.api.deps import menu_path
from app.ingest.loader import load_menu
from app.community.store import upsert_menu_item

def main():
    menu = load_menu(menu_path())
    count = 0
    for item in menu.values():
        upsert_menu_item(None, item)  # None forces Supabase persistence
        count += 1
    print(f"Successfully seeded {count} menu items to Supabase.")

if __name__ == "__main__":
    main()
