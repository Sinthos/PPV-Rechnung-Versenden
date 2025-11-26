import sys
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import AppSettings
from app.config import get_settings

# Mock environment variable
os.environ["TENANT_ID"] = "your-tenant-id-here"

# Setup in-memory DB
engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
db = SessionLocal()

def test_logic():
    print("--- Testing AppSettings Logic ---")
    
    # 1. Test get_microsoft_settings with default env (placeholder)
    # Reload settings to pick up env var
    from app import config
    config.reload_settings()
    
    ms_settings = AppSettings.get_microsoft_settings(db)
    print(f"Initial MS Settings (should be empty): {ms_settings}")
    
    if ms_settings['tenant_id'] == "":
        print("PASS: Placeholder correctly filtered out.")
    else:
        print(f"FAIL: Placeholder NOT filtered out. Got: '{ms_settings['tenant_id']}'")

    # 2. Test saving settings to DB
    print("\n--- Saving to DB ---")
    new_tenant_id = "12345-actual-tenant-id"
    AppSettings.set(db, AppSettings.KEY_TENANT_ID, new_tenant_id)
    db.commit()
    
    # 3. Test retrieving settings after save
    ms_settings_after = AppSettings.get_microsoft_settings(db)
    print(f"MS Settings after save: {ms_settings_after}")
    
    if ms_settings_after['tenant_id'] == new_tenant_id:
        print("PASS: DB value retrieved correctly.")
    else:
        print(f"FAIL: DB value NOT retrieved. Got: '{ms_settings_after['tenant_id']}'")

    # 4. Test logic with NO DB value and NO placeholder (simulating clean env)
    print("\n--- Testing with clean env and no DB ---")
    # Clear DB
    db.query(AppSettings).delete()
    db.commit()
    # Set env to valid value
    os.environ["TENANT_ID"] = "env-tenant-id"
    config.reload_settings()
    
    ms_settings_clean = AppSettings.get_microsoft_settings(db)
    print(f"MS Settings from Env: {ms_settings_clean}")
    
    if ms_settings_clean['tenant_id'] == "env-tenant-id":
        print("PASS: Env value retrieved correctly.")
    else:
         print(f"FAIL: Env value NOT retrieved. Got: '{ms_settings_clean['tenant_id']}'")


if __name__ == "__main__":
    test_logic()
